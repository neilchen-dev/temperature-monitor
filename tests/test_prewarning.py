from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from application.action_executor import ActionExecutionStatus, ActionExecutor
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmActionType,
    AlarmLifecycleState,
    AlarmState,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationState,
    OperationStatus,
)
from domain.monitor_engine import MonitorEngine
from domain.standard_resolver import StaticStandardResolver
from integrations.feishu_notifications import FeishuNotificationWriter
from integrations.feishu_records import FeishuRawRecord
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.runtime_state import (
    SQLiteAlarmStateRepository,
    SQLitePrewarningEffectRepository,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)


class _OperationProvider:
    def __init__(self, state: OperationStatus = OperationStatus.OPERATING) -> None:
        self.state = state

    def get(self, device: DeviceContext) -> OperationState:
        return OperationState(
            area_id=device.area,
            state=self.state,
            operation_type=None,
            work_order=None,
            started_at=None,
            ended_at=None,
        )


class _Source:
    def read_records(self, table_id: str):
        if table_id == "devices":
            return (
                FeishuRawRecord(
                    "rec-device",
                    {"设备编号": "TH-01", "默认异常责任人": [{"open_id": "ou_owner_1"}]},
                ),
                FeishuRawRecord(
                    "rec-device-2",
                    {"设备编号": "TH-02", "默认异常责任人": [{"open_id": "ou_owner_2"}]},
                ),
            )
        return ()


class _Sender:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_text(self, receive_id, receive_id_type, text, **kwargs):
        self.calls.append(
            {
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "text": text,
                **kwargs,
            }
        )
        return {"code": 0, "data": {"message_id": f"om-pre-{len(self.calls)}"}}


def _standard(
    *,
    standard_id: str = "STD-1",
    revision: str = "R1",
    control_type: ControlType = ControlType.ALL_DAY,
) -> EnvironmentStandard:
    return EnvironmentStandard(
        standard_id=standard_id,
        revision=revision,
        area="仓库",
        operation_type=None,
        temperature_min=20.0,
        temperature_max=26.0,
        humidity_min=40.0,
        humidity_max=60.0,
        effective_from=NOW - timedelta(days=1),
        effective_to=None,
        source_document="SOP",
        clause="5.2",
        control_type=control_type,
    )


class PrewarningDomainTests(unittest.TestCase):
    def test_temperature_margin_hysteresis_and_formal_violation_precedence(self) -> None:
        device = DeviceContext("TH-01", "仓库")
        standard = _standard()
        normal = MonitorEngine.evaluate(
            device=device,
            sample=MonitorSample("TH-01", NOW, 25.0, 50.0),
            standard=standard,
        )
        warning = MonitorEngine.evaluate(
            device=device,
            sample=MonitorSample("TH-01", NOW, 25.9, 50.0),
            standard=standard,
        )
        hysteresis = MonitorEngine.evaluate(
            device=device,
            sample=MonitorSample("TH-01", NOW, 25.7, 50.0),
            standard=standard,
            previous_prewarning_reasons=warning.prewarning_reasons,
        )
        outside = MonitorEngine.evaluate(
            device=device,
            sample=MonitorSample("TH-01", NOW, 26.1, 50.0),
            standard=standard,
            previous_prewarning_reasons=warning.prewarning_reasons,
        )
        self.assertEqual(normal.prewarning_reasons, ())
        self.assertIn("temperature_high_near_limit", warning.prewarning_reasons)
        self.assertIn("temperature_high_near_limit", hysteresis.prewarning_reasons)
        self.assertEqual(outside.overall_status.value, "VIOLATION")
        self.assertEqual(outside.prewarning_reasons, ())

    def test_humidity_lower_and_operation_idle_suppression(self) -> None:
        device = DeviceContext("TH-01", "仓库")
        standard = _standard()
        low_warning = MonitorEngine.evaluate(
            device=device,
            sample=MonitorSample("TH-01", NOW, 23.0, 42.0),
            standard=standard,
        )
        idle = MonitorEngine.evaluate(
            device=DeviceContext("TH-01", "仓库"),
            sample=MonitorSample("TH-01", NOW, 25.9, 50.0),
            standard=_standard(control_type=ControlType.OPERATION_PERIOD),
            operation_state=OperationState(
                "仓库", OperationStatus.IDLE, None, None, None, None
            ),
        )
        self.assertIn("humidity_low_near_limit", low_warning.prewarning_reasons)
        self.assertEqual(idle.applicability.value, "NOT_APPLICABLE")
        self.assertEqual(idle.prewarning_reasons, ())


class PrewarningPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.events = SQLiteEnvironmentEventRepository(self.connection)
        self.tasks = SQLiteAutomationTaskRepository(self.connection)
        self.states = SQLiteAlarmStateRepository(self.connection)
        self.effects = SQLitePrewarningEffectRepository(self.connection)
        self.sender = _Sender()
        self.writer = FeishuNotificationWriter(
            sender=self.sender,
            source=_Source(),
            event_table_id="events",
            device_table_id="devices",
            event_repository=self.events,
            prewarning_effect_repository=self.effects,
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _service(self, *, mode: str = "active", operation: OperationStatus = OperationStatus.OPERATING):
        executor = ActionExecutor(
            mode=mode,
            active_device_ids=("TH-01",),
            standards_ready_provider=lambda: True,
            context_handlers={
                AlarmActionType.NOTIFY_PREWARNING: self.writer.handle_notification_action,
                AlarmActionType.NOTIFY_PREWARNING_RECOVERY: self.writer.handle_notification_action,
            },
            action_enabled_provider=lambda action: True,
        )
        return MonitorApplicationService(
            operation_state_provider=_OperationProvider(operation),
            standard_resolver=StaticStandardResolver((_standard(),)),
            alarm_state_repository=self.states,
            alarm_state_machine=AlarmStateMachine(),
            action_mapper=ApplicationActionMapper(emit_notifications=True),
            action_executor=executor,
            task_repository=self.tasks,
            event_repository=self.events,
        )

    def _sample(self, temperature: float) -> MonitorSample:
        return MonitorSample("TH-01", NOW, temperature, 50.0)

    def test_active_warning_recovery_is_deduped_and_persisted(self) -> None:
        with patch.object(config, "FEISHU_PREWARNING_NOTIFY_ENABLED", True), patch.object(
            config, "FEISHU_PREWARNING_RECOVERY_NOTIFY_ENABLED", True
        ):
            service = self._service()
            first = service.handle_sample(device=DeviceContext("TH-01", "仓库"), sample=self._sample(25.9), now=NOW)
            second = service.handle_sample(
                device=DeviceContext("TH-01", "仓库"),
                sample=MonitorSample("TH-01", NOW + timedelta(seconds=10), 25.7, 50.0),
                now=NOW + timedelta(seconds=10),
            )
            cleared = service.handle_sample(
                device=DeviceContext("TH-01", "仓库"),
                sample=MonitorSample("TH-01", NOW + timedelta(seconds=20), 25.5, 50.0),
                now=NOW + timedelta(seconds=20),
            )
            reentered = service.handle_sample(
                device=DeviceContext("TH-01", "仓库"),
                sample=MonitorSample("TH-01", NOW + timedelta(seconds=30), 25.9, 50.0),
                now=NOW + timedelta(seconds=30),
            )
        self.assertEqual(first.transition.next.state, AlarmLifecycleState.NORMAL)
        self.assertEqual(first.actions[-1].action_type, AlarmActionType.NOTIFY_PREWARNING)
        self.assertEqual(second.actions, ())
        self.assertEqual(cleared.actions[-1].action_type, AlarmActionType.NOTIFY_PREWARNING_RECOVERY)
        self.assertEqual(reentered.actions[-1].action_type, AlarmActionType.NOTIFY_PREWARNING)
        self.assertEqual(len(self.sender.calls), 3)
        self.assertEqual([call["receive_id"] for call in self.sender.calls], [
            "ou_owner_1", "ou_owner_1", "ou_owner_1"
        ])
        state = self.states.get("TH-01")
        assert state is not None
        self.assertTrue(state.prewarning_active)
        self.assertEqual(state.prewarning_message_id, "om-pre-3")
        self.assertTrue(state.prewarning_recovery_message_id == "om-pre-2")
        markers = self.connection.execute(
            "SELECT effect_key, status, message_id FROM prewarning_external_effects ORDER BY rowid"
        ).fetchall()
        self.assertEqual(len(markers), 3)
        self.assertTrue(all(row[1] == "SUCCEEDED" for row in markers))

    def test_shadow_plans_without_sender_and_formal_violation_has_no_prewarning_recovery(self) -> None:
        with patch.object(config, "FEISHU_PREWARNING_NOTIFY_ENABLED", True):
            service = self._service(mode="shadow")
            warning = service.handle_sample(
                device=DeviceContext("TH-01", "仓库"), sample=self._sample(25.9), now=NOW
            )
            violation = service.handle_sample(
                device=DeviceContext("TH-01", "仓库"),
                sample=MonitorSample("TH-01", NOW + timedelta(minutes=1), 26.1, 50.0),
                now=NOW + timedelta(minutes=1),
            )
        self.assertEqual(len(self.sender.calls), 0)
        self.assertEqual(warning.executions[0].status, ActionExecutionStatus.PLANNED)
        self.assertEqual(violation.transition.next.state, AlarmLifecycleState.PENDING)
        self.assertNotIn(
            AlarmActionType.NOTIFY_PREWARNING_RECOVERY,
            [action.action_type for action in violation.actions],
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM environment_events").fetchone()[0],
            0,
        )

    def test_two_devices_have_independent_episode_keys_and_recipients(self) -> None:
        with patch.object(config, "FEISHU_PREWARNING_NOTIFY_ENABLED", True):
            first = self._service()
            first.standard_resolver = StaticStandardResolver((_standard(),))
            first.handle_sample(
                device=DeviceContext("TH-01", "仓库"), sample=self._sample(25.9), now=NOW
            )
            # The second service uses a separate device scope and a source
            # record for its owner; the marker key remains device-specific.
            second = self._service()
            second.action_executor.active_device_ids = ("TH-02",)
            second.standard_resolver = StaticStandardResolver(
                (_standard(standard_id="STD-2"),)
            )
            second.handle_sample(
                device=DeviceContext("TH-02", "仓库"),
                sample=MonitorSample("TH-02", NOW, 25.9, 50.0),
                now=NOW,
            )
        self.assertEqual([call["receive_id"] for call in self.sender.calls], [
            "ou_owner_1", "ou_owner_2"
        ])
        keys = [row[0] for row in self.connection.execute(
            "SELECT effect_key FROM prewarning_external_effects ORDER BY effect_key"
        )]
        self.assertEqual(len(keys), 2)
        self.assertTrue(any(":TH-01:" in key for key in keys))
        self.assertTrue(any(":TH-02:" in key for key in keys))


class PrewarningMigrationTests(unittest.TestCase):
    def test_alarm_state_migration_is_additive_and_idempotent(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE alarm_states ("
            "device_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "violation_started_at TEXT, alarm_started_at TEXT, recovery_started_at TEXT, "
            "active_alarm_id TEXT, pending_task_id TEXT, updated_at TEXT NOT NULL)"
        )
        repo = SQLiteAlarmStateRepository(connection)
        SQLiteAlarmStateRepository(connection)
        repo.save(
            AlarmState(
                "TH-01",
                AlarmLifecycleState.NORMAL,
                prewarning_active=True,
                prewarning_episode_id="episode-1",
                prewarning_reasons=("temperature_high_near_limit",),
            )
        )
        restored = repo.get("TH-01")
        assert restored is not None
        self.assertTrue(restored.prewarning_active)
        self.assertEqual(restored.prewarning_episode_id, "episode-1")
        self.assertEqual(
            connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
