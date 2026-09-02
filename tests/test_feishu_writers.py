from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import config
from domain.models import (
    DataQualityStatus,
    MonitorResult,
    MonitorSample,
    OverallStatus,
    TemperatureStatus,
)
from domain.operation import OperationAction, OperationObservation
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import (
    FeishuEnvironmentEventWriter,
    FeishuInspectionRecordWriter,
    FeishuOperationRecordWriter,
    FeishuWriteError,
)
from services.feishu import normalize_client_token


class _RecordingWriter:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any], str | None]] = []
        self.updated: list[tuple[str, str, dict[str, Any]]] = []

    def create(
        self,
        table_id: str,
        fields: dict[str, Any],
        *,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        self.created.append((table_id, fields, client_token))
        return {"record_id": "rec-created", "fields": fields}

    def update(self, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.updated.append((table_id, record_id, fields))
        return {"record_id": record_id, "fields": fields}


class _RecordSource:
    def __init__(self, records_by_table: dict[str, tuple[FeishuRawRecord, ...]]) -> None:
        self.records_by_table = records_by_table

    def read_records(self, table_id: str) -> tuple[FeishuRawRecord, ...]:
        return self.records_by_table.get(table_id, ())


def _sample() -> MonitorSample:
    return MonitorSample(
        device_id="TH-03",
        sample_time=datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc),
        temperature=25.5,
        humidity=55.0,
        online_status="online",
        data_quality=DataQualityStatus.GOOD,
    )


def _result() -> MonitorResult:
    return MonitorResult(
        device_id="TH-03",
        sample_time=_sample().sample_time,
        temperature=25.5,
        humidity=55.0,
        temperature_status=TemperatureStatus.NORMAL,
        humidity_status=TemperatureStatus.HIGH,
        overall_status=OverallStatus.VIOLATION,
        standard_id="STD-1",
        standard_revision="R1",
        reasons=("湿度高于上限",),
    )


class FeishuOperationWriterTests(unittest.TestCase):
    def test_registration_writes_only_confirmed_writable_fields(self) -> None:
        recording = _RecordingWriter()
        writer = FeishuOperationRecordWriter(
            writer=recording,
            operation_table_id="tbl-operation",
            interval_table_id="tbl-interval",
            device_table_id="tbl-device",
        )

        writer.create_registration(
            device_id="th-03",
            area="精密装配间",
            action=OperationAction.START,
            operation_type="未关联工艺文件（TH-03）",
            snapshot=_result(),
            status_recorded_at=_sample().sample_time,
            idempotency_key="operation-1",
        )

        table_id, fields, token = recording.created[-1]
        self.assertEqual(table_id, "tbl-operation")
        self.assertEqual(token, normalize_client_token("operation-1"))
        self.assertEqual(fields["监测点"], "TH-03")
        self.assertEqual(fields["区域"], "精密装配间")
        self.assertEqual(fields["状态变更"], "开始作业")
        self.assertEqual(fields["当前工艺"], "未关联工艺文件（TH-03）")
        self.assertEqual(fields["当时湿度判定"], "高于上限")
        self.assertNotIn("登记编号", fields)
        self.assertNotIn("提交时间", fields)

    def test_interval_uses_actual_status_and_trailing_space_option(self) -> None:
        recording = _RecordingWriter()
        writer = FeishuOperationRecordWriter(
            writer=recording,
            operation_table_id="tbl-operation",
            interval_table_id="tbl-interval",
            device_table_id="tbl-device",
        )
        observation = OperationObservation(
            device_id="TH-03",
            area_id="精密装配间",
            action=OperationAction.START,
            operation_type="未关联工艺文件（TH-03）",
            work_order=None,
            source_record_id="rec-operation",
            source_created_at=_sample().sample_time,
            observed_at=_sample().sample_time,
        )

        writer.create_interval(observation=observation, snapshot=_result())

        _, fields, token = recording.created[-1]
        self.assertEqual(token, normalize_client_token("RUN:rec-operation"))
        self.assertEqual(fields["区间状态"], "作业中")
        self.assertEqual(fields["工艺"], "未关联工艺文件（TH-03） ")
        self.assertNotIn("记录类型", fields)


class FeishuEnvironmentEventWriterTests(unittest.TestCase):
    def _writer(self, recording: _RecordingWriter, events: tuple[FeishuRawRecord, ...] = ()):
        source = _RecordSource(
            {
                "tbl-events": events,
                "tbl-devices": (
                    FeishuRawRecord(
                        record_id="rec-device",
                        fields={
                            "设备编号": "TH-03",
                            "默认异常责任人": [{"id": "ou_owner"}],
                            "要求来源": "执行适用标准",
                        },
                    ),
                ),
            }
        )
        return FeishuEnvironmentEventWriter(
            writer=recording,
            source=source,
            event_table_id="tbl-events",
            device_table_id="tbl-devices",
        )

    def test_create_resolves_owner_and_mirrors_event_type(self) -> None:
        recording = _RecordingWriter()
        writer = self._writer(recording)

        writer.create_event(
            device_id="th-03",
            area="精密装配间",
            start_time=_sample().sample_time,
            temperature=46,
            humidity=55,
            temperature_status="HIGH",
            humidity_status="NORMAL",
            idempotency_key="event-1",
        )

        _, fields, token = recording.created[-1]
        self.assertEqual(token, normalize_client_token("event-1"))
        self.assertEqual(fields["责任人"], [{"id": "ou_owner"}])
        self.assertEqual(fields["异常类型"], "温度高于上限")
        self.assertEqual(fields["处理状态"], "待处理")
        self.assertEqual(fields["控制要求"], "执行适用标准")
        self.assertNotIn("自动异常类型", fields)
        self.assertNotIn("记录类型", fields)

    def test_duplicate_active_event_is_rejected(self) -> None:
        recording = _RecordingWriter()
        active = FeishuRawRecord(
            record_id="rec-active",
            fields={"监测点": "TH-03", "处理状态": "处理中"},
        )
        writer = self._writer(recording, (active,))

        with self.assertRaises(FeishuWriteError):
            writer.create_event(
                device_id="TH-03",
                area="精密装配间",
                start_time=_sample().sample_time,
                temperature=46,
            )
        self.assertEqual(recording.created, [])

    def test_close_requires_manual_closure_fields(self) -> None:
        recording = _RecordingWriter()
        writer = self._writer(recording)
        with self.assertRaises(ValueError):
            writer.close_event(
                record_id="rec-active",
                closed_at=_sample().sample_time,
                cause="",
                measure="已处理",
                product_impact="无",
            )


class FeishuInspectionWriterTests(unittest.TestCase):
    def test_system_fields_are_not_written_and_number_field_stays_numeric(self) -> None:
        recording = _RecordingWriter()
        writer = FeishuInspectionRecordWriter(
            writer=recording,
            inspection_table_id="tbl-inspection",
            device_table_id="tbl-devices",
        )

        writer.create_snapshot(
            area="防爆仓库",
            inspected_at=_sample().sample_time,
            temperature=24.0,
            humidity=50.0,
            online_status="online",
            environment_status=OverallStatus.VIOLATION,
            temperature_status="NORMAL",
            humidity_status="HIGH",
            alarm_status="PENDING",
            monitoring_system_status="OFFLINE",
            site_storage_status="正常",
            abnormal_alarm_number=12,
            parent_record_id="rec-parent",
            idempotency_key="inspection-1",
        )

        _, fields, token = recording.created[-1]
        self.assertEqual(token, normalize_client_token("inspection-1"))
        # 写回时间按业务时区（HISTORY_TIMEZONE）输出，不能用系统本地时区：
        # 容器/CI 的本地时区是 UTC，断言会整体偏移。
        self.assertEqual(
            fields["状态记录时间"],
            _sample()
            .sample_time.astimezone(ZoneInfo(config.HISTORY_TIMEZONE))
            .strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.assertEqual(fields["当时环境判定"], "超限")
        self.assertEqual(fields["监测系统状态"], "离线/数据异常")
        self.assertEqual(fields["现场仓储状态"], "正常，无明显异常")
        self.assertEqual(fields["父记录"], [{"id": "rec-parent"}])
        self.assertNotIn("点检时间", fields)
        self.assertNotIn("点检人", fields)

    def test_inspection_does_not_accept_text_environment_event_number(self) -> None:
        writer = FeishuInspectionRecordWriter(
            writer=_RecordingWriter(),
            inspection_table_id="tbl-inspection",
            device_table_id="tbl-devices",
        )
        with self.assertRaises(ValueError):
            writer.create_snapshot(
                area="仓库",
                inspected_at=_sample().sample_time,
                abnormal_alarm_number="ENV-20260831-001",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
