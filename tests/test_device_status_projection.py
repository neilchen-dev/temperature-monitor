from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from domain.models import (
    AlarmLifecycleState,
    AlarmState,
    ControlType,
    DataQualityStatus,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
)
from domain.operation import OperationAction, OperationObservation
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import (
    FeishuDeviceStatusWriteFieldMap,
    FeishuDeviceStatusWriter,
    FeishuWriteError,
)
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.runtime_state import (
    SQLiteAlarmStateRepository,
    SQLiteDeviceStatusProjectionRepository,
    SQLiteLatestSampleRepository,
    SQLiteOperationRepository,
)
from services.device_status_projection import DeviceStatusProjector
from domain.standard_resolver import StaticStandardResolver


NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)


class _StatusWriter:
    def __init__(self, records: tuple[FeishuRawRecord, ...]) -> None:
        self.fields = FeishuDeviceStatusWriteFieldMap()
        self.records = {record.record_id: record for record in records}
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.fail_updates = 0

    def read_device_record(self, device_id: str) -> FeishuRawRecord:
        normalized = device_id.upper()
        for record in self.records.values():
            if str(record.fields.get("设备编号", "")).upper() == normalized:
                return record
        raise AssertionError(f"missing record for {normalized}")

    def update(self, *, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if self.fail_updates:
            self.fail_updates -= 1
            raise TimeoutError("simulated Feishu timeout")
        self.updates.append((record_id, dict(fields)))
        current = dict(self.records[record_id].fields)
        current.update(fields)
        self.records[record_id] = FeishuRawRecord(record_id, current)
        return {"record_id": record_id, "fields": fields}


def _standard(
    device_id: str,
    area: str = "精密装配间",
    control_type: ControlType = ControlType.ALL_DAY,
) -> EnvironmentStandard:
    return EnvironmentStandard(
        standard_id=f"ENV-{device_id}",
        revision="R1",
        area=area,
        device_id=device_id,
        operation_type=None,
        control_type=control_type,
        temperature_min=20,
        temperature_max=26,
        humidity_min=40,
        humidity_max=60,
        effective_from=NOW - timedelta(days=1),
        effective_to=None,
        source_document="SOP",
        clause="1",
        priority=1,
    )


class DeviceStatusProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_write_enabled = config.FEISHU_WRITE_ENABLED
        self.original_cutover_ack = config.ACTIVE_CUTOVER_ACK
        self.original_projection_enabled = config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED
        config.FEISHU_WRITE_ENABLED = True
        config.ACTIVE_CUTOVER_ACK = config.ACTIVE_CUTOVER_ACK_EXPECTED
        config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED = True
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.task_repository = SQLiteAutomationTaskRepository(self.connection)
        self.alarm_repository = SQLiteAlarmStateRepository(self.connection)
        self.sample_repository = SQLiteLatestSampleRepository(self.connection)
        self.operation_repository = SQLiteOperationRepository(self.connection)
        self.state_repository = SQLiteDeviceStatusProjectionRepository(self.connection)
        self.task_repository.set_runtime_context(mode="active", now=NOW)
        self.devices = {
            "TH-01": DeviceContext("TH-01", "对拖测试区"),
            "TH-03": DeviceContext("TH-03", "精密装配间"),
        }
        self.sample_repository.save(
            MonitorSample(
                device_id="TH-03",
                sample_time=NOW,
                temperature=24,
                humidity=50,
                online_status="online",
                data_quality=DataQualityStatus.GOOD,
            )
        )
        self.sample_repository.save(
            MonitorSample(
                device_id="TH-01",
                sample_time=NOW,
                temperature=24,
                humidity=50,
                online_status="online",
                data_quality=DataQualityStatus.GOOD,
            )
        )
        self.writer = _StatusWriter(
            (
                FeishuRawRecord(
                    "rec-01",
                    {"设备编号": "TH-01", "默认异常责任人": [{"id": "ou-01"}]},
                ),
                FeishuRawRecord(
                    "rec-03",
                    {"设备编号": "TH-03", "默认异常责任人": [{"id": "ou-03"}]},
                ),
            )
        )
        self.projector = DeviceStatusProjector(
            devices=self.devices,
            standard_resolver=StaticStandardResolver(
                (_standard("TH-01", "对拖测试区"), _standard("TH-03"))
            ),
            operation_state_provider=self.operation_repository,
            alarm_state_repository=self.alarm_repository,
            latest_sample_repository=self.sample_repository,
            task_repository=self.task_repository,
            state_repository=self.state_repository,
            writer=self.writer,
            active_device_ids=("TH-01", "TH-03"),
            standards_ready_provider=lambda: True,
            default_owner_by_device={"TH-03": [{"id": "ou-03"}]},
            now_provider=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.connection.close()
        config.FEISHU_WRITE_ENABLED = self.original_write_enabled
        config.ACTIVE_CUTOVER_ACK = self.original_cutover_ack
        config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED = self.original_projection_enabled

    def _start_operation(self) -> None:
        self.operation_repository.save_current(
            OperationObservation(
                device_id="TH-03",
                area_id="精密装配间",
                action=OperationAction.START,
                operation_type="工艺A",
                work_order="WO-1",
                source_record_id="operation-start",
                source_created_at=NOW - timedelta(minutes=2),
                observed_at=NOW,
            )
        )

    def _make_task(self, device_id: str = "TH-03"):
        return self.projector.request(device_id, now=NOW, trigger="test")

    def _seed_remote_with_desired(self, device_id: str) -> None:
        desired = self.projector.build_desired(device_id, now=NOW)
        record = self.writer.read_device_record(device_id)
        fields = dict(record.fields)
        fields.update(desired.fields)
        self.writer.records[record.record_id] = FeishuRawRecord(
            record.record_id,
            fields,
        )

    def test_standard_and_operation_fields_are_projected(self) -> None:
        self._start_operation()
        desired = self.projector.build_desired("TH-03", now=NOW)
        self.assertEqual(desired.fields["当前适用温度下限（°C）"], 20.0)
        self.assertEqual(desired.fields["当前适用温度上限（°C）"], 26.0)
        self.assertEqual(desired.fields["控制类型"], "全天控制")
        self.assertEqual(desired.fields["当前作业状态"], "作业中")
        self.assertEqual(desired.fields["当前工艺"], "工艺A")
        self.assertEqual(
            desired.fields["作业开始时间"],
            int((NOW - timedelta(minutes=2)).timestamp() * 1000),
        )
        self.assertEqual(desired.fields["默认异常责任人"], [{"id": "ou-03"}])

    def test_end_clears_operation_start_and_type(self) -> None:
        self._start_operation()
        end = NOW + timedelta(minutes=1)
        self.operation_repository.save_current(
            OperationObservation(
                device_id="TH-03",
                area_id="精密装配间",
                action=OperationAction.END,
                operation_type=None,
                work_order=None,
                source_record_id="operation-end",
                source_created_at=end,
                observed_at=end,
            )
        )
        desired = self.projector.build_desired("TH-03", now=end)
        self.assertEqual(desired.fields["当前作业状态"], "无作业")
        self.assertEqual(desired.fields["当前工艺"], "N/A")
        self.assertIsNone(desired.fields["作业开始时间"])

    def test_alarm_lifecycle_and_prewarning_labels_are_projected(self) -> None:
        expected = {
            AlarmLifecycleState.NORMAL: "未触发",
            AlarmLifecycleState.PENDING: "计时中",
            AlarmLifecycleState.ALARM: "已发警报",
            AlarmLifecycleState.RECOVERY: "恢复中",
        }
        for lifecycle, label in expected.items():
            self.alarm_repository.save(AlarmState("TH-01", lifecycle))
            desired = self.projector.build_desired("TH-01", now=NOW)
            self.assertEqual(desired.fields["警报状态"], label)

        self.alarm_repository.save(
            AlarmState(
                "TH-01",
                AlarmLifecycleState.NORMAL,
                prewarning_active=True,
                prewarning_episode_id="episode-1",
            )
        )
        desired = self.projector.build_desired("TH-01", now=NOW)
        self.assertEqual(desired.fields["警报状态"], "预警")

    def test_operation_period_without_operation_projects_not_applicable(self) -> None:
        self.projector.standard_resolver = StaticStandardResolver(
            (
                _standard(
                    "TH-01",
                    "对拖测试区",
                    control_type=ControlType.OPERATION_PERIOD,
                ),
                _standard("TH-03"),
            )
        )
        desired = self.projector.build_desired("TH-01", now=NOW)
        self.assertEqual(desired.fields["控制类型"], "作业期间控制")
        self.assertEqual(desired.fields["当前作业状态"], "N/A")
        self.assertEqual(desired.fields["警报状态"], "N/A")

    def test_request_is_deduplicated_after_success(self) -> None:
        first = self._make_task()
        self.assertIsNotNone(first)
        second = self._make_task()
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'PROJECT_DEVICE_STATUS'"
            ).fetchone()[0],
            1,
        )
        claimed = self.task_repository.claim_due(now=NOW, worker_id="scheduler")
        self.projector.handle_task(claimed[0], now=NOW)
        self.task_repository.mark_succeeded(
            claimed[0].task_id,
            finished_at=NOW,
            worker_id="scheduler",
        )
        self.assertIsNone(self._make_task())
        self.assertEqual(self.state_repository.summary()["mismatched_devices"], [])

    def test_only_changed_fields_are_sent(self) -> None:
        self._seed_remote_with_desired("TH-01")
        task = self._make_task("TH-01")
        claimed = self.task_repository.claim_due(now=NOW, worker_id="scheduler")
        self.projector.handle_task(claimed[0], now=NOW)
        self.task_repository.mark_succeeded(
            claimed[0].task_id,
            finished_at=NOW,
            worker_id="scheduler",
        )
        self.assertEqual(self.writer.updates, [])
        self.assertEqual(self.state_repository.summary()["mismatched_devices"], [])

        changed = dict(self.writer.records["rec-01"].fields)
        changed["当前适用湿度上限（%RH）"] = 55
        self.writer.records["rec-01"] = FeishuRawRecord("rec-01", changed)
        task = self.projector.request("TH-01", now=NOW, force=True, trigger="drift")
        claimed = self.task_repository.claim_due(now=NOW, worker_id="scheduler-2")
        current = next(item for item in claimed if item.task_id == task.task_id)
        self.projector.handle_task(current, now=NOW)
        self.assertEqual(self.writer.updates[-1][1], {"当前适用湿度上限（%RH）": 60.0})

    def test_failure_is_rescheduled_with_same_task_id(self) -> None:
        task = self._make_task()
        claimed = self.task_repository.claim_due(now=NOW, worker_id="scheduler")
        self.writer.fail_updates = 1
        with self.assertRaises(TimeoutError):
            self.projector.handle_task(claimed[0], now=NOW)
        pending = self.task_repository.get(task.task_id)
        self.assertEqual(pending.status.value, "PENDING")
        self.assertEqual(pending.task_id, task.task_id)
        self.assertTrue(self.state_repository.get("TH-03")["pending"])

        retry_time = pending.due_at + timedelta(seconds=1)
        retry_claimed = self.task_repository.claim_due(
            now=retry_time,
            worker_id="scheduler",
        )
        self.projector.handle_task(retry_claimed[0], now=retry_time)
        self.assertFalse(self.state_repository.get("TH-03")["pending"])
        self.assertEqual(self.state_repository.get("TH-03")["failed"], False)

    def test_shadow_projection_is_audited_without_remote_write(self) -> None:
        self.task_repository.set_runtime_context(mode="shadow", now=NOW)
        task = self._make_task("TH-01")
        self.assertIsNotNone(task)
        self.assertEqual(task.payload["external_effect_policy"], "SHADOW_ONLY")
        self.assertEqual(
            self.state_repository.get("TH-01")["status"],
            "SHADOW_ONLY",
        )
        self.assertEqual(self.writer.updates, [])
        self.assertIsNone(
            self.projector.request("TH-01", now=NOW, trigger="same_state")
        )

    def test_independent_gate_reads_and_audits_drift_without_remote_write(self) -> None:
        config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED = False
        task = self._make_task("TH-01")
        claimed = self.task_repository.claim_due(now=NOW, worker_id="scheduler")
        self.projector.handle_task(claimed[0], now=NOW)
        self.task_repository.mark_succeeded(
            task.task_id,
            finished_at=NOW,
            worker_id="scheduler",
        )

        state = self.state_repository.get("TH-01")
        self.assertEqual(state["status"], "SHADOW_ONLY")
        self.assertEqual(state["record_id"], "rec-01")
        self.assertIn("当前适用温度下限（°C）", state["changed_fields"])
        self.assertEqual(self.writer.updates, [])
        projection = self.projector.summary()
        self.assertFalse(projection["enabled"])
        self.assertEqual(projection["gated_count"], 1)
        self.assertEqual(projection["planned_count"], 1)
        self.assertEqual(projection["mismatched_devices"], ["TH-01"])
        self.assertEqual(
            projection["devices"][0]["changed_fields"],
            state["changed_fields"],
        )

        # Re-enabling the independent gate must requeue the same desired hash
        # instead of treating the read-only SHADOW_ONLY observation as success.
        config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED = True
        retry_task = self.projector.request("TH-01", now=NOW)
        self.assertIsNotNone(retry_task)
        retry_claimed = self.task_repository.claim_due(now=NOW, worker_id="scheduler-2")
        current = next(item for item in retry_claimed if item.task_id == retry_task.task_id)
        self.projector.handle_task(current, now=NOW)
        self.task_repository.mark_succeeded(
            current.task_id,
            finished_at=NOW,
            worker_id="scheduler-2",
        )
        self.assertEqual(self.writer.updates, [("rec-01", {
            "当前适用温度下限（°C）": 20.0,
            "当前适用温度上限（°C）": 26.0,
            "当前适用湿度下限（%RH）": 40.0,
            "当前适用湿度上限（%RH）": 60.0,
            "控制类型": "全天控制",
            "当前作业状态": "N/A",
            "当前工艺": "N/A",
            "警报状态": "未触发",
        })])
        self.assertEqual(self.projector.summary()["mismatched_devices"], [])

    def test_device_status_schema_validation_is_read_only_and_explicit(self) -> None:
        field_map = FeishuDeviceStatusWriteFieldMap()
        required = {
            field_map.device_id,
            field_map.temperature_min,
            field_map.temperature_max,
            field_map.humidity_min,
            field_map.humidity_max,
            field_map.control_type,
            field_map.operation_state,
            field_map.operation_type,
            field_map.default_owner,
            field_map.alarm_status,
            field_map.operation_started_at,
        }

        class _SchemaSource:
            def read_field_names(self, _table_id: str):
                return tuple(required)

            def read_records(self, _table_id: str):
                return ()

        writer = FeishuDeviceStatusWriter(
            writer=object(),
            source=_SchemaSource(),
            device_table_id="device-table",
        )
        self.assertEqual(writer.validate_schema()["status"], "valid")
        self.assertEqual(writer.schema_status()["field_count"], len(required))

        class _IncompleteSchema(_SchemaSource):
            def read_field_names(self, _table_id: str):
                return tuple(field for field in required if field != field_map.default_owner)

        incomplete = FeishuDeviceStatusWriter(
            writer=object(),
            source=_IncompleteSchema(),
            device_table_id="device-table",
        )
        with self.assertRaises(FeishuWriteError):
            incomplete.validate_schema()
        self.assertEqual(incomplete.schema_status()["status"], "invalid")

    def test_existing_projection_table_gets_additive_changed_fields_migration(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            """
            CREATE TABLE device_status_projection (
                device_id TEXT PRIMARY KEY,
                record_id TEXT,
                desired_hash TEXT,
                desired_fields_json TEXT NOT NULL DEFAULT '{}',
                observed_fields_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'PENDING',
                pending INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                last_attempt_at TEXT,
                last_error TEXT,
                last_task_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        SQLiteDeviceStatusProjectionRepository(connection)
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(device_status_projection)")
        }
        self.assertIn("changed_fields_json", columns)
        SQLiteDeviceStatusProjectionRepository(connection)
        self.assertIn(
            "changed_fields_json",
            {
                row[1]
                for row in connection.execute("PRAGMA table_info(device_status_projection)")
            },
        )
        connection.close()

    def test_empty_active_allowlist_fails_closed(self) -> None:
        self.projector.active_device_ids = ()
        task = self._make_task("TH-01")
        self.assertIsNone(task)
        self.assertEqual(
            self.state_repository.get("TH-01")["status"],
            "SHADOW_ONLY",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'PROJECT_DEVICE_STATUS'"
            ).fetchone()[0],
            0,
        )

    def test_devices_keep_independent_records(self) -> None:
        self._seed_remote_with_desired("TH-01")
        self._seed_remote_with_desired("TH-03")
        first = self.projector.request("TH-01", now=NOW)
        second = self.projector.request("TH-03", now=NOW)
        claimed = self.task_repository.claim_due(now=NOW, limit=10, worker_id="scheduler")
        for task in claimed:
            self.projector.handle_task(task, now=NOW)
            self.task_repository.mark_succeeded(
                task.task_id,
                finished_at=NOW,
                worker_id="scheduler",
            )
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual({record_id for record_id, _ in self.writer.updates}, set())
        self.assertEqual(self.state_repository.get("TH-01")["record_id"], "rec-01")
        self.assertEqual(self.state_repository.get("TH-03")["record_id"], "rec-03")
        self.assertEqual(self.state_repository.summary()["mismatched_devices"], [])


if __name__ == "__main__":
    unittest.main()
