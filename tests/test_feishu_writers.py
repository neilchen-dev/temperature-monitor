from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from domain.models import (
    AlarmAction,
    AlarmActionType,
    DataQualityStatus,
    MonitorResult,
    MonitorSample,
    OverallStatus,
    TemperatureStatus,
)
from domain.operation import OperationAction, OperationObservation
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import (
    FeishuEventWriteFieldMap,
    FeishuEnvironmentEventWriter,
    FeishuInspectionRecordWriter,
    FeishuOperationRecordWriter,
)
from repositories.environment_events import SQLiteEnvironmentEventRepository
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
        self.assertEqual(
            fields["状态记录时间"], int(_sample().sample_time.timestamp() * 1000)
        )
        self.assertIsInstance(fields["状态记录时间"], int)
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
        self.assertEqual(fields["开始时间"], int(_sample().sample_time.timestamp() * 1000))
        self.assertIsInstance(fields["开始时间"], int)
        self.assertNotIn("记录类型", fields)

    def test_operation_datetime_updates_use_epoch_milliseconds(self) -> None:
        recording = _RecordingWriter()
        writer = FeishuOperationRecordWriter(
            writer=recording,
            operation_table_id="tbl-operation",
            interval_table_id="tbl-interval",
            device_table_id="tbl-device",
        )
        moment = _sample().sample_time
        expected = int(moment.timestamp() * 1000)

        writer.update_registration_snapshot(
            registration_record_id="rec-operation",
            snapshot=_result(),
            status_recorded_at=moment,
        )
        self.assertEqual(recording.updated[-1][2]["状态记录时间"], expected)
        writer.close_interval(interval_record_id="rec-interval", ended_at=moment)
        self.assertEqual(recording.updated[-1][2]["结束时间"], expected)
        writer.update_device_context(
            device_record_id="rec-device",
            state="OPERATING",
            operation_type="未关联工艺文件（TH-03）",
            started_at=moment,
        )
        self.assertEqual(recording.updated[-1][2]["作业开始时间"], expected)


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
        self.assertEqual(fields["开始时间"], int(_sample().sample_time.timestamp() * 1000))
        self.assertIsInstance(fields["开始时间"], int)
        self.assertNotIn("自动异常类型", fields)
        self.assertNotIn("记录类型", fields)

    def test_recovery_and_close_datetimes_use_epoch_milliseconds(self) -> None:
        recording = _RecordingWriter()
        writer = self._writer(recording)
        moment = _sample().sample_time
        expected = int(moment.timestamp() * 1000)

        writer.recover_event(record_id="rec-event", recovered_at=moment)
        self.assertEqual(recording.updated[-1][2]["恢复时间"], expected)
        writer.close_event(
            record_id="rec-event",
            closed_at=moment,
            cause="测试原因",
            measure="测试措施",
            product_impact="无",
            recovered_at=moment,
        )
        self.assertEqual(recording.updated[-1][2]["关闭时间"], expected)
        self.assertEqual(recording.updated[-1][2]["恢复时间"], expected)

    def test_event_generated_idempotency_key_is_stable_for_same_instant(self) -> None:
        recording = _RecordingWriter()
        writer = self._writer(recording)
        utc_start = _sample().sample_time
        business_start = utc_start.astimezone(ZoneInfo("Asia/Shanghai"))

        writer.create_event(
            device_id="TH-03",
            area="精密装配间",
            start_time=utc_start,
            temperature=46,
        )
        writer.create_event(
            device_id="TH-03",
            area="精密装配间",
            start_time=business_start,
            temperature=46,
        )

        self.assertEqual(recording.created[0][2], recording.created[1][2])
        self.assertEqual(
            recording.created[0][1]["开始时间"],
            recording.created[1][1]["开始时间"],
        )

    def test_recovered_unclosed_event_does_not_block_new_alarm_cycle(self) -> None:
        recording = _RecordingWriter()
        historical = FeishuRawRecord(
            record_id="rec-historical",
            fields={
                "监测点": "TH-03",
                "处理状态": "处理中",
                "恢复时间": 1_756_700_000_000,
                "闭环状态": "未关闭",
            },
        )
        writer = self._writer(recording, (historical,))

        writer.create_event(
            device_id="TH-03",
            area="精密装配间",
            start_time=_sample().sample_time,
            temperature=46,
        )
        self.assertEqual(len(recording.created), 1)

    def test_mark_recovered_updates_exact_cycle_without_manual_fields(self) -> None:
        recording = _RecordingWriter()
        event = FeishuRawRecord(
            record_id="rec-current",
            fields={
                "监测点": "TH-03",
                "开始时间": int(_sample().sample_time.timestamp() * 1000),
                "闭环状态": "未关闭",
            },
        )
        writer = self._writer(recording, (event,))
        action = AlarmAction(
            action_type=AlarmActionType.MARK_ALARM_RECOVERED,
            device_id="TH-03",
        )
        recovered_at = _sample().sample_time.replace(minute=1)

        writer.handle_alarm_action(
            action,
            {
                "created_at": recovered_at.isoformat(),
                "sample": {"temperature": 25.5, "humidity": 55.0},
                "python_alarm_transition": {
                    "violation_started_at": _sample().sample_time.isoformat(),
                },
            },
        )

        table_id, record_id, fields = recording.updated[-1]
        self.assertEqual((table_id, record_id), ("tbl-events", "rec-current"))
        self.assertEqual(fields, {"恢复时间": int(recovered_at.timestamp() * 1000)})
        for forbidden in ("闭环状态", "异常原因", "处理措施", "产品影响", "关闭时间"):
            self.assertNotIn(forbidden, fields)

    def test_optional_recovery_measurement_fields_are_written_when_configured(self) -> None:
        recording = _RecordingWriter()
        writer = FeishuEnvironmentEventWriter(
            writer=recording,
            source=_RecordSource({}),
            event_table_id="tbl-events",
            device_table_id="tbl-devices",
            fields=FeishuEventWriteFieldMap(
                recovery_temperature="恢复温度(°C)",
                recovery_humidity="恢复湿度(%RH)",
            ),
        )
        writer.recover_event(
            record_id="rec-current",
            recovered_at=_sample().sample_time,
            temperature=25.5,
            humidity=55.0,
        )
        fields = recording.updated[-1][2]
        self.assertEqual(fields["恢复温度(°C)"], 25.5)
        self.assertEqual(fields["恢复湿度(%RH)"], 55.0)

    def test_update_targets_current_cycle_not_recovered_unclosed_history(self) -> None:
        recording = _RecordingWriter()
        old_start = _sample().sample_time.replace(hour=9)
        current_start = _sample().sample_time
        events = (
            FeishuRawRecord(
                record_id="rec-A",
                fields={
                    "监测点": "TH-03",
                    "开始时间": int(old_start.timestamp() * 1000),
                    "恢复时间": int(old_start.replace(minute=30).timestamp() * 1000),
                    "闭环状态": "未关闭",
                },
            ),
            FeishuRawRecord(
                record_id="rec-B",
                fields={
                    "监测点": "TH-03",
                    "开始时间": int(current_start.timestamp() * 1000),
                    "闭环状态": "未关闭",
                },
            ),
        )
        writer = self._writer(recording, events)
        action = AlarmAction(
            action_type=AlarmActionType.UPDATE_ALARM_EVENT,
            device_id="TH-03",
        )
        writer.handle_alarm_action(
            action,
            {
                "sample": {"temperature": 47.0, "humidity": 56.0},
                "python_monitor_result": {
                    "temperature_status": "HIGH",
                    "humidity_status": "NORMAL",
                },
                "python_alarm_transition": {
                    "violation_started_at": current_start.isoformat(),
                },
                "operation_state": {"area_id": "精密装配间"},
            },
        )
        self.assertEqual(recording.updated[-1][1], "rec-B")

    def test_restart_uses_persisted_local_to_feishu_record_binding(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteEnvironmentEventRepository(connection)
        local = repository.create_or_get_active(
            device_id="TH-03",
            event_key="ENV:TH-03:round-B",
            opened_at=_sample().sample_time,
        )
        repository.bind_external_record(local.event_id, record_id="rec-B")

        recording = _RecordingWriter()
        restarted_repository = SQLiteEnvironmentEventRepository(connection)
        writer = FeishuEnvironmentEventWriter(
            writer=recording,
            source=_RecordSource({}),
            event_table_id="tbl-events",
            device_table_id="tbl-devices",
            event_repository=restarted_repository,
        )
        writer.handle_alarm_action(
            AlarmAction(
                action_type=AlarmActionType.UPDATE_ALARM_EVENT,
                device_id="TH-03",
                alarm_id=local.event_id,
            ),
            {
                "sample": {"temperature": 47.0},
                "python_monitor_result": {"temperature_status": "HIGH"},
                "python_alarm_transition": {},
                "operation_state": {"area_id": "精密装配间"},
            },
        )

        self.assertEqual(recording.updated[-1][1], "rec-B")

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
        self.assertEqual(
            fields["状态记录时间"],
            int(_sample().sample_time.timestamp() * 1000),
        )
        self.assertIsInstance(fields["状态记录时间"], int)
        self.assertEqual(fields["当时环境判定"], "超限")
        self.assertEqual(fields["监测系统状态"], "离线/数据异常")
        self.assertEqual(fields["现场仓储状态"], "正常，无明显异常")
        self.assertEqual(fields["父记录"], [{"id": "rec-parent"}])
        self.assertNotIn("点检时间", fields)
        self.assertNotIn("点检人", fields)

        writer.update_device_recent_inspection(
            device_record_id="rec-device",
            inspected_at=_sample().sample_time,
        )
        self.assertEqual(
            recording.updated[-1][2]["最近仓库点检时间"],
            int(_sample().sample_time.timestamp() * 1000),
        )

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
