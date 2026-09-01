from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import config
from domain.models import MonitorSample, OperationStatus
from domain.operation import OperationAction, OperationObservation
from integrations.feishu_records import FeishuRawRecord
from application.operation_sync import OperationObservationService
from repositories.runtime_state import SQLiteOperationRepository
from runtime.bootstrap import build_runtime
from services import devices


class _ReadOnlySource:
    def __init__(self) -> None:
        business_timezone = ZoneInfo(config.HISTORY_TIMEZONE)
        self.records = {
            config.FEISHU_STANDARD_TABLE_ID: (
                FeishuRawRecord(
                    record_id="standard-10",
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
                        "生效时间": "2026-01-01T00:00:00",
                        "失效时间": None,
                        "优先级": 1,
                        "是否启用": True,
                        "来源文件": "SOP-001",
                        "条款": "5.2.3",
                    },
                    created_at=datetime(2026, 1, 1, tzinfo=business_timezone),
                    updated_at=datetime(2026, 1, 1, tzinfo=business_timezone),
                ),
            ),
            config.FEISHU_OPERATION_TABLE_ID: (),
            config.FEISHU_EVENT_TABLE_ID: (),
            "device-table": (
                FeishuRawRecord(
                    record_id="device-10",
                    fields={
                        "设备编号": "TH-10",
                        "警报状态": "未触发",
                        "当前作业状态": "N/A",
                        "当前判定状态": "超限",
                        "温度判定": "高于上限",
                        "湿度判定": "正常",
                        "在线状态": "在线",
                    },
                    updated_at=datetime(
                        2026, 8, 31, 12, 0, tzinfo=business_timezone
                    ),
                ),
            ),
            "event-table": (),
        }

    def read_records(self, table_id: str):
        return self.records.get(table_id, ())


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
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
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = ("TH-10",)
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True

    def tearDown(self) -> None:
        for name, value in self.original.items():
            setattr(config, name, value)

    def test_shadow_bootstrap_is_available_and_not_active(self) -> None:
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_ReadOnlySource(),
        )
        self.assertEqual(components.status()["mode"], "shadow")
        self.assertTrue(components.status()["available"])
        self.assertFalse(components.runtime.monitor_service.action_executor.mode.value == "active")
        components.start()
        self.assertTrue(components.status()["scheduler_running"])
        components.stop()

    def test_missing_credentials_is_explicitly_degraded(self) -> None:
        config.APP_ID = ""
        config.APP_SECRET = ""
        config.APP_TOKEN = ""
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_ReadOnlySource(),
        )
        status = components.status()
        self.assertFalse(status["available"])
        self.assertTrue(status["degraded"])
        self.assertIn("FEISHU_APP_ID", status["reason"])
        self.assertFalse(components.status()["scheduler_running"])
        components.stop()

    def test_active_mode_requires_explicit_feishu_write_enable(self) -> None:
        config.AUTOMATION_MODE = "active"
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_ReadOnlySource(),
        )
        self.assertFalse(components.status()["available"])
        self.assertEqual(components.runtime.monitor_service.action_executor.mode.value, "disabled")
        self.assertIn("FEISHU_WRITE_ENABLED=true", components.status()["reason"])
        components.start()
        self.assertFalse(components.status()["scheduler_running"])
        components.stop()

    def test_whitelist_routes_sample_and_creates_only_local_tasks(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_ReadOnlySource(),
            now_provider=lambda: now,
        )
        components.runtime.handle_standard_sync(object())
        components.runtime._accepting_samples = True
        handled = components.handle_sample(
            MonitorSample(" th-10 ", now, 28.0, 50.0, online_status="online")
        )
        self.assertIsNotNone(handled)
        self.assertEqual(handled.operation_state.state, OperationStatus.NOT_APPLICABLE)
        self.assertIsNone(
            components.handle_sample(
                MonitorSample("TH-03", now, 28.0, 50.0, online_status="online")
            )
        )
        task_types = {
            row[0]
            for row in components.connection.execute(
                "SELECT task_type FROM automation_tasks WHERE task_type = 'VERIFY_ALARM'"
            )
        }
        self.assertEqual(task_types, {"VERIFY_ALARM"})
        self.assertEqual(
            components.connection.execute(
                "SELECT COUNT(*) FROM environment_events"
            ).fetchone()[0],
            0,
        )
        components.stop()

    def test_due_verification_persists_alarm_and_local_projected_event(self) -> None:
        current_time = [
            datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        ]
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_ReadOnlySource(),
            now_provider=lambda: current_time[0],
        )
        components.runtime.handle_standard_sync(object())
        components.runtime._accepting_samples = True
        components.handle_sample(
            MonitorSample("TH-10", current_time[0], 28.0, 50.0, online_status="online")
        )
        current_time[0] += timedelta(minutes=5)
        claimed = components.task_repository.claim_due(
            now=current_time[0], worker_id=components.runtime.worker_id
        )
        verify = next(task for task in claimed if task.task_type == "VERIFY_ALARM")
        components.runtime.handle_verify_alarm(verify)
        self.assertEqual(
            components.connection.execute(
                "SELECT state FROM alarm_states WHERE device_id = 'TH-10'"
            ).fetchone()[0],
            "ALARM",
        )
        self.assertEqual(
            components.connection.execute(
                "SELECT COUNT(*) FROM environment_events"
            ).fetchone()[0],
            1,
        )
        components.stop()

    def test_started_runtime_listener_schedules_compare_from_naive_standard(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_ReadOnlySource(),
            now_provider=lambda: now,
        )
        components.runtime.handle_standard_sync(object())
        synced_standard = components.standard_repository.list_all()[0]
        self.assertIsNotNone(synced_standard.effective_from.tzinfo)

        try:
            with patch.object(
                components.runtime,
                "_run_scheduler",
                side_effect=lambda stop_event: stop_event.wait(),
            ):
                components.start()
                with (
                    patch(
                        "services.devices.db.fetch_previous_device_sample",
                        return_value=None,
                    ),
                    patch("services.devices.db.save_device_sample"),
                ):
                    devices.record_sample(
                        device="TH-10",
                        source=devices.SOURCE_HOME_ASSISTANT,
                        temperature=24.0,
                        humidity=50.0,
                        status="在线",
                        sample_time_ms=int(now.timestamp() * 1000),
                    )

            row = components.connection.execute(
                "SELECT task_type, entity_id FROM automation_tasks "
                "WHERE task_type = 'SHADOW_COMPARE'"
            ).fetchone()
            self.assertEqual(tuple(row), ("SHADOW_COMPARE", "TH-10"))
        finally:
            components.stop()

    def test_scheduler_executes_compare_and_records_shadow_run(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        source = _ReadOnlySource()
        source.records[config.FEISHU_EVENT_TABLE_ID] = (
            FeishuRawRecord(
                record_id="event-10",
                fields={"监测点": "TH-10", "处理状态": "处理中"},
                updated_at=now,
            ),
        )
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=source,
            now_provider=lambda: now,
        )
        try:
            components.runtime.handle_standard_sync(object())
            components.runtime._accepting_samples = True
            components.handle_sample(
                MonitorSample("TH-10", now, 24.0, 50.0, online_status="online")
            )

            report = components.runtime.scheduler.run_once(now=now)

            self.assertEqual(report.succeeded, 1)
            row = components.connection.execute(
                "SELECT action_type, matched FROM automation_runs "
                "WHERE action_type = 'SHADOW_COMPARE'"
            ).fetchone()
            self.assertEqual(row[0], "SHADOW_COMPARE")
            self.assertIn(row[1], (0, 1))
        finally:
            components.stop()

    def test_operation_observations_are_applied_by_source_creation_order(self) -> None:
        connection = sqlite3.connect(":memory:")
        repository = SQLiteOperationRepository(connection)
        service = OperationObservationService(store=repository)
        newer = OperationObservation(
            device_id="TH-03",
            area_id="精密装配间",
            action=OperationAction.START,
            operation_type="工艺B",
            work_order="WO-2",
            source_record_id="operation-newer",
            source_created_at=datetime(2026, 8, 31, 12, 5),
            observed_at=datetime(2026, 8, 31, 12, 6),
        )
        older = OperationObservation(
            device_id="TH-03",
            area_id="精密装配间",
            action=OperationAction.END,
            operation_type=None,
            work_order=None,
            source_record_id="operation-older",
            source_created_at=datetime(2026, 8, 31, 12, 1),
            observed_at=datetime(2026, 8, 31, 12, 7),
        )

        self.assertTrue(service.apply(newer).accepted)
        self.assertFalse(service.apply(older).accepted)
        self.assertEqual(repository.get_current("TH-03").source_record_id, "operation-newer")
        self.assertEqual(repository.get(self._device("TH-03")).state.value, "OPERATING")
        audit = connection.execute(
            "SELECT accepted FROM operation_observation_audit ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in audit], [1, 0])
        connection.close()

    @staticmethod
    def _device(device_id: str):
        from domain.models import ControlType, DeviceContext

        return DeviceContext(
            device_id=device_id,
            area="精密装配间",
            control_type=ControlType.OPERATION_PERIOD,
        )


if __name__ == "__main__":
    unittest.main()
