"""真实链路集成测试（P2）。

覆盖：
1. HA 上报 → record_sample → 监听器 → MonitorEngine → SHADOW_COMPARE 任务
   → scheduler.run_once → automation_runs 全链路。
2. OPERATION_PERIOD：无作业 / 作业开始 / 作业结束（现状特征化）/ 作业重叠。
3. offline：在线→离线→在线的期望状态与状态机行为。
4. 容器 TZ=UTC：标准生效时间与采样时间都用 UTC aware 也能正确解析。
5. 飞书 observation 边界：0/1/N 活动事件、待人工闭环、公式字段缺失、
   字段 None / 0 / ""。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import config
import runtime.bootstrap as bootstrap
from domain.models import MonitorSample
from integrations.feishu_observation import (
    FeishuBitableObservationSource,
    FeishuObservationAdapter,
    FeishuObservationFieldMap,
)
from integrations.feishu_records import FeishuRawRecord
from runtime.bootstrap import build_runtime
from services import devices


TZ = ZoneInfo("Asia/Shanghai")


def _std_record(record_id: str, area: str, device: str, temp_max: float) -> FeishuRawRecord:
    return FeishuRawRecord(
        record_id=record_id,
        fields={
            "标准编号": f"ENV-{device}",
            "版本": "Rev.A",
            "适用区域": area,
            "适用设备": device,
            "适用作业类型": None,
            "温度下限（°C）": 20,
            "温度上限（°C）": temp_max,
            "湿度下限（%RH）": 40,
            "湿度上限（%RH）": 60,
            "生效时间": "2026-01-01T00:00:00",
            "失效时间": None,
            "优先级": 1,
            "是否启用": True,
            "来源文件": "SOP-001",
            "条款": "5.2.3",
        },
        created_at=datetime(2026, 1, 1, tzinfo=TZ),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
    )


def _dev_record(record_id: str, device: str, **extra) -> FeishuRawRecord:
    fields = {
        "设备编号": device,
        "警报状态": "未触发",
        "当前作业状态": "N/A",
        "当前判定状态": "正常",
        "温度判定": "正常",
        "湿度判定": "正常",
        "在线状态": "在线",
    }
    fields.update(extra)
    return FeishuRawRecord(
        record_id=record_id,
        fields=fields,
        created_at=datetime(2026, 1, 1, tzinfo=TZ),
        updated_at=datetime(2026, 9, 1, 12, 0, tzinfo=TZ),
    )


def _op_record(record_id: str, device: str, action: str, operation_type, moment: datetime) -> FeishuRawRecord:
    fields = {
        "监测点": device,
        "区域": "精密装配间",
        "状态变更": action,
        "登记组合校验": "有效",
    }
    if operation_type is not None:
        fields["当前工艺"] = operation_type
    return FeishuRawRecord(
        record_id=record_id,
        fields=fields,
        created_at=moment,
        updated_at=moment,
    )


class _ChainSource:
    """两台设备 + 两条标准的可变读源；operation 表可在测试中追加。"""

    def __init__(self) -> None:
        self.records: dict[str, list[FeishuRawRecord]] = {
            config.FEISHU_STANDARD_TABLE_ID: [
                _std_record("std-10", "PE仓库", "TH-10", 26),
                _std_record("std-03", "精密装配间", "TH-03", 26),
            ],
            config.FEISHU_OPERATION_TABLE_ID: [],
            config.FEISHU_EVENT_TABLE_ID: [],
            "device-table": [
                _dev_record("dev-10", "TH-10"),
                _dev_record("dev-03", "TH-03"),
            ],
        }

    def read_records(self, table_id: str):
        return tuple(self.records.get(table_id, ()))

    def add(self, table_id: str, record: FeishuRawRecord) -> None:
        self.records.setdefault(table_id, []).append(record)


class _RuntimeTestBase(unittest.TestCase):
    whitelist: tuple[str, ...] = ("TH-10",)

    def setUp(self) -> None:
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=TZ)
        self.source = _ChainSource()
        self.original = {
            name: getattr(config, name)
            for name in (
                "AUTOMATION_MODE",
                "SHADOW_DEVICE_IDS",
                "APP_ID",
                "APP_SECRET",
                "APP_TOKEN",
                "FEISHU_DEVICE_TABLE_ID",
                "SQLITE_ENABLED",
                "HISTORY_TIMEZONE",
                "AUTOMATION_RUN_RETENTION_DAYS",
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = self.whitelist
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        self.addCleanup(self._restore)
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.connection.close)
        self.components = build_runtime(
            connection=self.connection,
            record_source=self.source,
            now_provider=lambda: self.now,
        )
        self.addCleanup(self._cleanup_runtime)
        self.components.runtime.handle_standard_sync(object())

    def _restore(self) -> None:
        for name, value in self.original.items():
            setattr(config, name, value)
        bootstrap._last_components = None

    def _cleanup_runtime(self) -> None:
        bootstrap._last_components = None
        try:
            self.components.stop()
        except Exception:  # noqa: BLE001 - cleanup must not mask test result
            pass

    def _sample(self, device: str, temp: float, hum: float = 50.0, **kwargs):
        defaults = dict(online_status="online")
        defaults.update(kwargs)
        return MonitorSample(device, self.now, temp, hum, **defaults)

    def _alarm_state(self, device: str) -> str:
        row = self.connection.execute(
            "SELECT state FROM alarm_states WHERE device_id = ?", (device,)
        ).fetchone()
        return row[0] if row else "NORMAL"

    def _task_types(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT task_type FROM automation_tasks ORDER BY created_at, id"
        ).fetchall()
        return [row[0] for row in rows]

    def _pending_verify_task_id(self, device: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT id FROM automation_tasks
            WHERE task_type = 'VERIFY_ALARM' AND entity_id = ? AND status = 'PENDING'
            """,
            (device,),
        ).fetchone()
        return row[0] if row else None


class FullChainTests(_RuntimeTestBase):
    def test_shadow_task_carries_previous_expected_projection(self) -> None:
        runtime = self.components.runtime
        runtime._accepting_samples = True
        self.components.handle_sample(self._sample("TH-10", 24.0))
        self.now += timedelta(seconds=1)
        self.components.handle_sample(self._sample("TH-10", 28.0))

        payloads = [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT payload_json FROM automation_tasks "
                "WHERE task_type = 'SHADOW_COMPARE' ORDER BY created_at, id"
            ).fetchall()
        ]

        self.assertIsNone(payloads[0]["expected"]["previous_state"])
        previous = payloads[1]["expected"]["previous_state"]
        self.assertEqual(previous["temperature_status"], "NORMAL")
        self.assertEqual(previous["overall_status"], "NORMAL")
        self.assertEqual(previous["alarm_state"], "NORMAL")

    def test_temperature_report_to_automation_runs_e2e(self) -> None:
        """/temperature 上报 → 统一模型 → Shadow 域 → 比对结果落 automation_runs。"""
        runtime = self.components.runtime
        with patch.object(
            runtime, "_run_scheduler", side_effect=lambda stop_event: stop_event.wait()
        ):
            runtime.start()
            try:
                now_ms = int(self.now.timestamp() * 1000)
                with (
                    patch.object(
                        devices.db, "fetch_previous_device_sample", return_value=None
                    ),
                    patch.object(devices.db, "save_device_sample"),
                    patch.object(devices.db, "save_device_event"),
                ):
                    transitions = devices.record_sample(
                        "TH-10",
                        devices.SOURCE_HOME_ASSISTANT,
                        24.0,
                        50.0,
                        "在线",
                        now_ms,
                    )
                self.assertEqual(transitions, [])

                # 监听器已把样本送进 Shadow 管道：产生 SHADOW_COMPARE 任务。
                self.assertIn("SHADOW_COMPARE", self._task_types())
                # latest sample 持久化（供 verification task 使用）。
                row = self.connection.execute(
                    "SELECT temperature FROM latest_monitor_samples WHERE device_id = 'TH-10'"
                ).fetchone()
                self.assertIsNotNone(row)

                # 调度器执行比对：结果落 automation_runs。
                # （stop() 会关闭 runtime 连接，因此所有断言必须在 stop 前完成。）
                self.now = self.now + timedelta(seconds=1)
                runtime.scheduler.run_once(now=self.now)
                run = self.connection.execute(
                    """
                    SELECT device_id, matched, difference_type
                    FROM automation_runs WHERE action_type = 'SHADOW_COMPARE'
                    """
                ).fetchone()
                self.assertIsNotNone(run, "SHADOW_COMPARE 必须产生 automation_runs 记录")
                self.assertEqual(run["device_id"], "TH-10")
                self.assertEqual(run["matched"], 1)
                self.assertIsNone(run["difference_type"])
            finally:
                runtime.stop()


class OperationPeriodTests(_RuntimeTestBase):
    whitelist = ("TH-03",)

    def _sync_operations(self) -> None:
        self.components.runtime.handle_operation_sync(object())

    def _handle(self, temp: float, **kwargs):
        self.components.runtime._accepting_samples = True
        return self.components.handle_sample(self._sample("TH-03", temp, **kwargs))

    def test_no_operation_violation_is_not_applicable(self) -> None:
        result = self._handle(28.0)
        # 无作业：OPERATION_PERIOD 设备不判定，状态保持 NORMAL。
        self.assertEqual(result.monitor_result.applicability.value, "NOT_APPLICABLE")
        self.assertEqual(result.monitor_result.overall_status.value, "UNKNOWN")
        self.assertEqual(self._alarm_state("TH-03"), "NORMAL")
        self.assertNotIn("VERIFY_ALARM", self._task_types())

    def test_operation_start_enables_judgement(self) -> None:
        self._handle(24.0)  # baseline
        self.source.add(
            config.FEISHU_OPERATION_TABLE_ID,
            _op_record("op-1", "TH-03", "开始作业", "精密装配", self.now),
        )
        self._sync_operations()
        result = self._handle(28.0)
        self.assertEqual(result.monitor_result.applicability.value, "APPLICABLE")
        self.assertEqual(result.monitor_result.overall_status.value, "VIOLATION")
        self.assertEqual(self._alarm_state("TH-03"), "PENDING")
        self.assertIn("VERIFY_ALARM", self._task_types())

    def test_operation_end_returns_to_not_applicable(self) -> None:
        self._handle(24.0)
        self.source.add(
            config.FEISHU_OPERATION_TABLE_ID,
            _op_record("op-1", "TH-03", "开始作业", "精密装配", self.now),
        )
        self._sync_operations()
        self._handle(28.0)
        self.assertEqual(self._alarm_state("TH-03"), "PENDING")

        # 作业结束：回到 NOT_APPLICABLE，超限不再推进报警（特征化现状：
        # PENDING 不会因 UNKNOWN 直接清零，需要后续 NORMAL 或重新作业）。
        end_at = self.now + timedelta(minutes=1)
        self.source.add(
            config.FEISHU_OPERATION_TABLE_ID,
            _op_record("op-2", "TH-03", "结束作业", None, end_at),
        )
        self._sync_operations()
        result = self._handle(28.0)
        self.assertEqual(result.monitor_result.applicability.value, "NOT_APPLICABLE")
        self.assertEqual(result.monitor_result.overall_status.value, "UNKNOWN")
        self.assertEqual(self._alarm_state("TH-03"), "PENDING")

    def test_overlapping_operations_latest_source_wins(self) -> None:
        earlier = self.now - timedelta(minutes=10)
        self.source.add(
            config.FEISHU_OPERATION_TABLE_ID,
            _op_record("op-1", "TH-03", "开始作业", "精密装配", earlier),
        )
        self._sync_operations()
        self.source.add(
            config.FEISHU_OPERATION_TABLE_ID,
            _op_record("op-2", "TH-03", "工艺切换", "特殊工艺", self.now),
        )
        self._sync_operations()
        result = self._handle(28.0)
        self.assertEqual(result.operation_state.state.value, "OPERATING")
        self.assertEqual(result.operation_state.operation_type, "特殊工艺")


class OfflineTransitionTests(_RuntimeTestBase):
    def test_online_offline_online_expected_state(self) -> None:
        runtime = self.components.runtime
        runtime._accepting_samples = True
        # 在线超限 → PENDING。
        first = runtime.handle_sample(self._sample("TH-10", 28.0))
        self.assertEqual(first.transition.next.state.value, "PENDING")
        # 离线样本 → data_quality=OFFLINE → overall UNKNOWN → 保持 PENDING。
        offline = runtime.handle_sample(
            MonitorSample("TH-10", self.now, None, None, online_status="offline", data_quality="OFFLINE")
        )
        self.assertEqual(offline.monitor_result.data_quality.value, "OFFLINE")
        self.assertEqual(offline.monitor_result.overall_status.value, "UNKNOWN")
        self.assertEqual(self._alarm_state("TH-10"), "PENDING")
        # 回到在线且正常 → NORMAL，VERIFY 任务被取消。
        online = runtime.handle_sample(self._sample("TH-10", 24.0))
        self.assertEqual(online.transition.next.state.value, "NORMAL")
        self.assertEqual(self._alarm_state("TH-10"), "NORMAL")
        task_id = self._pending_verify_task_id("TH-10")
        self.assertIsNone(task_id)
        cancelled = self.connection.execute(
            "SELECT status FROM automation_tasks WHERE task_type = 'VERIFY_ALARM'"
        ).fetchone()
        self.assertEqual(cancelled["status"], "CANCELLED")


class TimezoneUtcTests(_RuntimeTestBase):
    def test_utc_aware_datetimes_flow_through_pipeline(self) -> None:
        """容器 TZ=UTC：标准生效时间与采样时间都用 UTC aware 也能命中标准。"""
        self.source.records[config.FEISHU_STANDARD_TABLE_ID] = [
            FeishuRawRecord(
                record_id="std-utc",
                fields={
                    "标准编号": "ENV-TH-10",
                    "版本": "Rev.A",
                    "适用区域": "PE仓库",
                    "适用设备": "TH-10",
                    "适用作业类型": None,
                    "温度下限（°C）": 20,
                    "温度上限（°C）": 26,
                    "湿度下限（%RH）": 40,
                    "湿度上限（%RH）": 60,
                    "生效时间": "2026-01-01T00:00:00+00:00",
                    "失效时间": None,
                    "优先级": 1,
                    "是否启用": True,
                    "来源文件": "SOP-001",
                    "条款": "5.2.3",
                },
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        ]
        runtime = self.components.runtime
        runtime.handle_standard_sync(object())
        runtime._accepting_samples = True
        utc_now = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)  # = 12:00 +08:00
        result = runtime.handle_sample(
            MonitorSample("TH-10", utc_now, 28.0, 50.0, online_status="online")
        )
        self.assertEqual(result.monitor_result.standard_id, "ENV-TH-10")
        self.assertEqual(result.monitor_result.overall_status.value, "VIOLATION")

    def test_naive_standard_string_is_interpreted_as_business_timezone(self) -> None:
        """飞书表里的 naive 生效时间必须按业务时区解析（+08:00）。"""
        runtime = self.components.runtime
        rows = runtime.standard_repository.list_all()
        self.assertTrue(rows)
        for standard in rows:
            self.assertIsNotNone(standard.effective_from.tzinfo)
            self.assertEqual(
                standard.effective_from.utcoffset(), timedelta(hours=8)
            )


class _MutableSource:
    def __init__(self) -> None:
        self.records: dict[str, list[FeishuRawRecord]] = {}

    def read_records(self, table_id: str):
        return tuple(self.records.get(table_id, ()))

    def add(self, table_id: str, record: FeishuRawRecord) -> None:
        self.records.setdefault(table_id, []).append(record)


class ObservationEdgeCaseTests(unittest.TestCase):
    """飞书观察层边界：0/1/N 活动事件、待闭环、字段缺失/None/0/空串。"""

    def _build(self, device_fields: dict, event_records: list[FeishuRawRecord]):
        source = _MutableSource()
        source.add(
            "dev",
            FeishuRawRecord(
                record_id="dev-x",
                fields=device_fields,
                created_at=datetime(2026, 9, 1, 12, 0, tzinfo=TZ),
                updated_at=datetime(2026, 9, 1, 12, 0, tzinfo=TZ),
            ),
        )
        for record in event_records:
            source.add("evt", record)
        observation_source = FeishuBitableObservationSource(
            source=source, device_table_id="dev", event_table_id="evt"
        )
        adapter = FeishuObservationAdapter(
            source=observation_source,
            fields=FeishuObservationFieldMap(
                alarm_state="警报状态",
                operation_state="当前作业状态",
                event_exists="__event_exists",
                active_event_count="__active_event_count",
                pending_closure_count="__pending_closure_count",
            ),
        )
        return adapter

    def _event(
        self,
        record_id: str,
        status: str,
        recovery_time,
        *,
        start_time=1_756_699_000_000,
    ) -> FeishuRawRecord:
        return FeishuRawRecord(
            record_id=record_id,
            fields={
                "监测点": "TH-01",
                "处理状态": status,
                "恢复时间": recovery_time,
                "开始时间": start_time,
                "闭环状态": "已闭环" if status == "关闭" else "未关闭",
            },
            created_at=datetime(2026, 9, 1, 11, 0, tzinfo=TZ),
            updated_at=datetime(2026, 9, 1, 11, 0, tzinfo=TZ),
        )

    def _device_fields(self, **extra) -> dict:
        fields = {
            "设备编号": "TH-01",
            "警报状态": "未触发",
            "当前作业状态": "N/A",
        }
        fields.update(extra)
        return fields

    def test_zero_open_events(self) -> None:
        adapter = self._build(self._device_fields(), [])
        observed = adapter.observe("TH-01")
        self.assertFalse(observed.event_exists)
        self.assertEqual(observed.active_event_count, 0)
        self.assertEqual(observed.pending_closure_count, 0)

    def test_one_active_event(self) -> None:
        adapter = self._build(
            self._device_fields(),
            [self._event("e1", "处理中", None)],
        )
        observed = adapter.observe("TH-01")
        self.assertTrue(observed.event_exists)
        self.assertEqual(observed.active_event_count, 1)
        self.assertEqual(observed.pending_closure_count, 0)

    def test_pending_closure_is_separated_from_active(self) -> None:
        adapter = self._build(
            self._device_fields(),
            [
                self._event("e1", "处理中", None),  # 真活动
                self._event("e2", "处理中", 1756700000000),  # 已写恢复时间
                self._event("e3", "关闭", 1756700000000),  # 已关闭不计
            ],
        )
        observed = adapter.observe("TH-01")
        self.assertTrue(observed.event_exists)
        self.assertEqual(observed.active_event_count, 1)
        self.assertEqual(observed.pending_closure_count, 1)

    def test_multiple_active_events_counted(self) -> None:
        adapter = self._build(
            self._device_fields(),
            [
                self._event("e1", "处理中", None),
                self._event("e2", "处理中", None),
                self._event("e3", "处理中", ""),
            ],
        )
        observed = adapter.observe("TH-01")
        self.assertEqual(observed.active_event_count, 3)
        self.assertEqual(observed.pending_closure_count, 0)

    def test_distinct_unclosed_alarm_cycles_are_not_counted_as_duplicates(self) -> None:
        adapter = self._build(
            self._device_fields(),
            [
                self._event("e1", "处理中", None, start_time=1_756_699_000_000),
                self._event("e2", "处理中", None, start_time=1_756_700_000_000),
            ],
        )
        observed = adapter.observe("TH-01")
        self.assertEqual(observed.active_event_count, 1)

    def test_missing_formula_fields_do_not_raise(self) -> None:
        """公式字段缺失（表结构变化/权限）不得让 observe 抛异常。"""
        adapter = self._build({"设备编号": "TH-01"}, [])
        observed = adapter.observe("TH-01")
        self.assertEqual(observed.alarm_state, "")
        self.assertEqual(observed.operation_state, "")

    def test_none_zero_empty_field_values_do_not_raise(self) -> None:
        adapter = self._build(
            self._device_fields(
                **{
                    "警报状态": None,
                    "当前作业状态": 0,
                    "当前判定状态": "",
                }
            ),
            [],
        )
        observed = adapter.observe("TH-01")
        # 不抛异常即为底线；值被归一为空文本（0 为 falsy，与 "" 同样归空）。
        self.assertEqual(observed.alarm_state, "")
        self.assertEqual(observed.operation_state, "")


if __name__ == "__main__":
    unittest.main()
