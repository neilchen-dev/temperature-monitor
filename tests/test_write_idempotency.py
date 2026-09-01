"""Active 写回应用层幂等测试（P1）。

覆盖异常事件 / 作业登记 / 点检三类写操作：
1. 第一次 create 成功
2. 同一业务键第二次执行不重复创建（复用 record_id）
3. 模拟 timeout 但远端记录实际存在 → 重试识别既有记录
4. 不同业务键仍正常创建

业务键不依赖本地 SQLite：进程重启、本地事务失败后依旧成立，因为每次
写入前先读飞书表做“查业务键 → 复用/创建”。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from domain.operation import OperationAction
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import (
    FeishuEnvironmentEventWriter,
    FeishuInspectionRecordWriter,
    FeishuOperationRecordWriter,
    FeishuWriteError,
)


TZ = ZoneInfo("Asia/Shanghai")


class _FakeWriter:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict, str | None]] = []
        self.fail_next = False

    def create(self, table_id, fields, *, client_token=None):
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("feishu write timeout")
        self.created.append((table_id, dict(fields), client_token))
        return {"record_id": f"rec-{len(self.created)}", "code": 0}

    def update(self, table_id, record_id, fields):
        return {"record_id": record_id, "code": 0}


class _TableSource:
    """可变飞书读源：模拟“远端记录在写入后可被读回”。"""

    def __init__(self) -> None:
        self.tables: dict[str, list[FeishuRawRecord]] = {}

    def read_records(self, table_id: str):
        return tuple(self.tables.get(table_id, ()))

    def add(self, table_id: str, record: FeishuRawRecord) -> None:
        self.tables.setdefault(table_id, []).append(record)


def _ms(moment: datetime) -> int:
    """飞书 datetime 字段的真实读回形态：毫秒时间戳。"""
    return int(moment.timestamp() * 1000)


def _record(record_id: str, fields: dict) -> FeishuRawRecord:
    moment = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)
    return FeishuRawRecord(
        record_id=record_id,
        fields=fields,
        created_at=moment,
        updated_at=moment,
    )


class EnvironmentEventIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _TableSource()
        self.writer = FeishuEnvironmentEventWriter(
            writer=_FakeWriter(),
            source=self.source,
            event_table_id="evt",
            device_table_id="dev",
            device_id_field="设备编号",
        )
        self.start = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)
        self.source.add(
            "dev",
            _record(
                "dev-1",
                {"设备编号": "TH-01", "默认异常责任人": "张三", "要求来源": "SOP-001"},
            ),
        )

    def _create(self, **overrides):
        kwargs = dict(
            device_id="TH-01",
            area="PE仓库",
            start_time=self.start,
            temperature=30.0,
            humidity=52.0,
            temperature_status="HIGH",
        )
        kwargs.update(overrides)
        return self.writer.create_event(**kwargs)

    def test_first_create_succeeds(self) -> None:
        result = self._create()
        self.assertNotIn("existing", result)
        self.assertEqual(len(self.writer.writer.created), 1)
        self.assertIsNotNone(result.get("record_id"))

    def test_same_business_key_does_not_create_twice(self) -> None:
        self._create()
        # 模拟飞书读回（datetime 为毫秒时间戳）。
        self.source.add(
            "evt",
            _record(
                "rec-remote",
                {
                    "监测点": "TH-01",
                    "开始时间": _ms(self.start),
                    "处理状态": "待处理",
                },
            ),
        )
        result = self._create()
        self.assertTrue(result["existing"])
        self.assertEqual(result["record_id"], "rec-remote")
        # 没有第二次 create。
        self.assertEqual(len(self.writer.writer.created), 1)

    def test_timeout_with_remote_success_reuses_on_retry(self) -> None:
        # 第一次调用：远端还没有记录，写入超时。
        self.writer.writer.fail_next = True
        with self.assertRaises(TimeoutError):
            self._create()
        # 超时的写实际已经落库（远端有记录了）。
        self.source.add(
            "evt",
            _record(
                "rec-late",
                {
                    "监测点": "TH-01",
                    "开始时间": _ms(self.start),
                    "处理状态": "待处理",
                },
            ),
        )
        result = self._create()
        self.assertTrue(result["existing"])
        self.assertEqual(result["record_id"], "rec-late")
        # 重试没有再次调用 create。
        self.assertEqual(self.writer.writer.created, [])

    def test_closed_event_with_same_business_key_still_reuses(self) -> None:
        """已关闭事件的开始时间相同 → 复用而不是新建第二条。"""
        self.source.add(
            "evt",
            _record(
                "rec-closed",
                {
                    "监测点": "TH-01",
                    "开始时间": _ms(self.start),
                    "处理状态": "关闭",
                },
            ),
        )
        result = self._create()
        self.assertTrue(result["existing"])
        self.assertEqual(result["record_id"], "rec-closed")
        self.assertEqual(self.writer.writer.created, [])

    def test_different_business_key_creates_new_event(self) -> None:
        # 既有事件已关闭（历史事件），新逻辑事件（不同开始时间）必须新建。
        self.source.add(
            "evt",
            _record(
                "rec-1",
                {
                    "监测点": "TH-01",
                    "开始时间": _ms(self.start),
                    "处理状态": "关闭",
                },
            ),
        )
        later = self.start.replace(minute=30)
        result = self._create(start_time=later)
        self.assertNotIn("existing", result)
        self.assertEqual(len(self.writer.writer.created), 1)

    def test_two_active_events_still_rejected(self) -> None:
        later = self.start.replace(minute=30)
        for i, moment in enumerate((self.start, later)):
            self.source.add(
                "evt",
                _record(
                    f"rec-{i}",
                    {
                        "监测点": "TH-01",
                        "开始时间": _ms(moment),
                        "处理状态": "待处理",
                    },
                ),
            )
        newest = self.start.replace(minute=45)
        with self.assertRaises(FeishuWriteError):
            self._create(start_time=newest)


class OperationRegistrationIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _TableSource()
        self.writer = FeishuOperationRecordWriter(
            writer=_FakeWriter(),
            operation_table_id="op",
            interval_table_id="iv",
            device_table_id="dev",
            source=self.source,
        )
        self.recorded_at = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)

    def _create(self, **overrides):
        kwargs = dict(
            device_id="TH-03",
            area="精密装配间",
            action=OperationAction.START,
            operation_type="精密装配",
            status_recorded_at=self.recorded_at,
        )
        kwargs.update(overrides)
        return self.writer.create_registration(**kwargs)

    def test_first_create_succeeds(self) -> None:
        result = self._create()
        self.assertNotIn("existing", result)
        self.assertEqual(len(self.writer.writer.created), 1)

    def test_same_business_key_does_not_create_twice(self) -> None:
        self._create()
        self.source.add(
            "op",
            _record(
                "rec-op",
                {
                    "监测点": "TH-03",
                    "状态变更": "开始作业",
                    "状态记录时间": _ms(self.recorded_at),
                },
            ),
        )
        result = self._create()
        self.assertTrue(result["existing"])
        self.assertEqual(result["record_id"], "rec-op")
        self.assertEqual(len(self.writer.writer.created), 1)

    def test_timeout_with_remote_success_reuses_on_retry(self) -> None:
        self.writer.writer.fail_next = True
        with self.assertRaises(TimeoutError):
            self._create()
        self.source.add(
            "op",
            _record(
                "rec-op-late",
                {
                    "监测点": "TH-03",
                    "状态变更": "开始作业",
                    "状态记录时间": _ms(self.recorded_at),
                },
            ),
        )
        result = self._create()
        self.assertTrue(result["existing"])
        self.assertEqual(result["record_id"], "rec-op-late")
        self.assertEqual(self.writer.writer.created, [])

    def test_different_time_or_action_creates_new(self) -> None:
        self.source.add(
            "op",
            _record(
                "rec-op",
                {
                    "监测点": "TH-03",
                    "状态变更": "开始作业",
                    "状态记录时间": _ms(self.recorded_at),
                },
            ),
        )
        later = self.recorded_at.replace(minute=30)
        result = self._create(status_recorded_at=later)
        self.assertNotIn("existing", result)
        result = self._create(action=OperationAction.END)
        self.assertNotIn("existing", result)
        self.assertEqual(len(self.writer.writer.created), 2)


class InspectionSnapshotIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _TableSource()
        self.writer = FeishuInspectionRecordWriter(
            writer=_FakeWriter(),
            inspection_table_id="wh",
            device_table_id="dev",
            source=self.source,
        )
        self.inspected_at = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)

    def _create(self, **overrides):
        kwargs = dict(
            area="PE仓库",
            inspected_at=self.inspected_at,
            temperature=24.0,
            humidity=52.0,
        )
        kwargs.update(overrides)
        return self.writer.create_snapshot(**kwargs)

    def test_first_create_succeeds(self) -> None:
        result = self._create()
        self.assertNotIn("existing", result)
        self.assertEqual(len(self.writer.writer.created), 1)

    def test_same_business_key_does_not_create_twice(self) -> None:
        self._create()
        self.source.add(
            "wh",
            _record(
                "rec-wh",
                {"仓库区域": "PE仓库", "状态记录时间": _ms(self.inspected_at)},
            ),
        )
        result = self._create()
        self.assertTrue(result["existing"])
        self.assertEqual(result["record_id"], "rec-wh")
        self.assertEqual(len(self.writer.writer.created), 1)

    def test_timeout_with_remote_success_reuses_on_retry(self) -> None:
        self.writer.writer.fail_next = True
        with self.assertRaises(TimeoutError):
            self._create()
        self.source.add(
            "wh",
            _record(
                "rec-wh-late",
                {"仓库区域": "PE仓库", "状态记录时间": _ms(self.inspected_at)},
            ),
        )
        result = self._create()
        self.assertTrue(result["existing"])
        self.assertEqual(result["record_id"], "rec-wh-late")
        self.assertEqual(self.writer.writer.created, [])

    def test_different_area_or_time_creates_new(self) -> None:
        self.source.add(
            "wh",
            _record(
                "rec-wh",
                {"仓库区域": "PE仓库", "状态记录时间": _ms(self.inspected_at)},
            ),
        )
        later = self.inspected_at.replace(minute=30)
        result = self._create(inspected_at=later)
        self.assertNotIn("existing", result)
        result = self._create(area="精密装配间")
        self.assertNotIn("existing", result)
        self.assertEqual(len(self.writer.writer.created), 2)


if __name__ == "__main__":
    unittest.main()
