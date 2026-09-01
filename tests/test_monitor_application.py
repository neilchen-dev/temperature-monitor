from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from application.action_executor import ActionExecutor
from application.actions import ApplicationActionKind, ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from application.shadow import expected_state_from
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmState,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationState,
    OperationStatus,
)
from domain.standard_resolver import StaticStandardResolver
from repositories.automation_runs import SQLiteAutomationRunRepository


class _OperationProvider:
    def get(self, device: DeviceContext) -> OperationState:
        return OperationState(
            area_id=device.area,
            state=OperationStatus.OPERATING,
            operation_type=None,
            work_order=None,
            started_at=None,
            ended_at=None,
        )


class _AlarmRepository:
    def __init__(self) -> None:
        self.states: dict[str, AlarmState] = {}

    def get(self, device_id: str) -> AlarmState | None:
        return self.states.get(device_id)

    def save(self, state: AlarmState) -> None:
        self.states[state.device_id] = state


class MonitorApplicationServiceTests(unittest.TestCase):
    def test_handle_sample_runs_domain_then_shadow_executor(self) -> None:
        now = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
        device = DeviceContext("TH-03", "仓库")
        standard = EnvironmentStandard(
            standard_id="ENV-002",
            revision="Rev.B",
            area="仓库",
            operation_type=None,
            temperature_min=20.0,
            temperature_max=26.0,
            humidity_min=40.0,
            humidity_max=60.0,
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            effective_to=None,
            source_document="SOP-001",
            clause="5.2.3",
        )
        connection = sqlite3.connect(":memory:")
        recorder = SQLiteAutomationRunRepository(connection)
        alarm_repository = _AlarmRepository()
        service = MonitorApplicationService(
            operation_state_provider=_OperationProvider(),
            standard_resolver=StaticStandardResolver((standard,)),
            alarm_state_repository=alarm_repository,
            alarm_state_machine=AlarmStateMachine(),
            action_mapper=ApplicationActionMapper(),
            action_executor=ActionExecutor(mode="shadow", recorder=recorder),
        )
        handled = service.handle_sample(
            device=device,
            sample=MonitorSample("TH-03", now, 27.0, 50.0),
            now=now,
        )
        self.assertEqual(handled.transition.next.state.value, "PENDING")
        self.assertEqual(len(handled.actions), 1)
        self.assertEqual(handled.actions[0].kind, ApplicationActionKind.SCHEDULE_TASK)
        self.assertTrue(handled.actions[0].dedupe_key.startswith("VERIFY_ALARM:TH-03:"))
        self.assertEqual(handled.executions[0].status.value, "PLANNED")
        self.assertEqual(alarm_repository.states["TH-03"].state.value, "PENDING")
        connection.close()

    def test_expected_state_is_canonical_and_not_feishu_shaped(self) -> None:
        operation = _OperationProvider().get(DeviceContext("TH-03", "仓库"))
        expected = expected_state_from(
            device_id="TH-03",
            alarm_state="ALARM",
            operation_state=operation,
        )
        self.assertEqual(expected.alarm_state, "ALARM")
        self.assertEqual(expected.operation_state, "OPERATING")
        self.assertTrue(expected.event_exists)


if __name__ == "__main__":
    unittest.main()
