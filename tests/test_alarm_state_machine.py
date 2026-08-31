from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmActionType,
    AlarmLifecycleState,
    AlarmState,
    MonitorResult,
    OverallStatus,
    TemperatureStatus,
)


class AlarmStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 8, 28, 13, 0)
        self.machine = AlarmStateMachine()
        self.alarm_result = self._result(OverallStatus.VIOLATION)
        self.normal_result = self._result(OverallStatus.NORMAL)
        self.unknown_result = self._result(OverallStatus.UNKNOWN)

    def _result(self, status: OverallStatus) -> MonitorResult:
        return MonitorResult(
            device_id="TH-03",
            sample_time=self.start,
            temperature=27.0 if status is OverallStatus.VIOLATION else 24.0,
            humidity=50.0,
            temperature_status=(
                TemperatureStatus.HIGH
                if status is OverallStatus.VIOLATION
                else TemperatureStatus.NORMAL
            ),
            humidity_status=TemperatureStatus.NORMAL,
            overall_status=status,
            standard_id="ENV-002",
            standard_revision="Rev.B",
            reasons=(),
        )

    def test_first_violation_creates_pending_task(self) -> None:
        transition = self.machine.apply(
            result=self.alarm_result,
            current_state=AlarmState.normal("TH-03"),
            now=self.start,
        )
        self.assertEqual(transition.next.state, AlarmLifecycleState.PENDING)
        self.assertEqual(transition.actions[0].action_type, AlarmActionType.CREATE_VERIFY_TASK)
        self.assertEqual(transition.actions[0].run_at, self.start + timedelta(minutes=5))

    def test_persisted_string_state_is_accepted(self) -> None:
        persisted_state = AlarmState("TH-03", "NORMAL")
        transition = self.machine.apply(
            result=self.alarm_result,
            current_state=persisted_state,
            now=self.start,
        )
        self.assertEqual(transition.next.state, AlarmLifecycleState.PENDING)

    def test_pending_violation_becomes_alarm_after_five_minutes(self) -> None:
        pending = AlarmState(
            device_id="TH-03",
            state=AlarmLifecycleState.PENDING,
            violation_started_at=self.start,
            pending_task_id="task-1",
        )
        transition = self.machine.apply(
            result=self.alarm_result,
            current_state=pending,
            now=self.start + timedelta(minutes=5),
        )
        self.assertEqual(transition.next.state, AlarmLifecycleState.ALARM)
        self.assertEqual(
            [action.action_type for action in transition.actions],
            [AlarmActionType.COMPLETE_VERIFY_TASK, AlarmActionType.CREATE_ALARM_EVENT],
        )

    def test_pending_recovery_cancels_task(self) -> None:
        pending = AlarmState(
            device_id="TH-03",
            state=AlarmLifecycleState.PENDING,
            violation_started_at=self.start,
            pending_task_id="task-1",
        )
        transition = self.machine.apply(
            result=self.normal_result,
            current_state=pending,
            now=self.start + timedelta(minutes=1),
        )
        self.assertEqual(transition.next.state, AlarmLifecycleState.NORMAL)
        self.assertEqual(transition.actions[0].action_type, AlarmActionType.CANCEL_VERIFY_TASK)

    def test_unknown_does_not_clear_pending_or_alarm(self) -> None:
        for state in (
            AlarmState(
                "TH-03",
                AlarmLifecycleState.PENDING,
                violation_started_at=self.start,
            ),
            AlarmState("TH-03", AlarmLifecycleState.ALARM, active_alarm_id="event-1"),
        ):
            transition = self.machine.apply(
                result=self.unknown_result,
                current_state=state,
                now=self.start + timedelta(minutes=1),
            )
            self.assertEqual(transition.next, state)
            self.assertEqual(transition.actions, ())

    def test_alarm_requires_one_minute_recovery_confirmation_by_default(self) -> None:
        alarm = AlarmState("TH-03", AlarmLifecycleState.ALARM, active_alarm_id="event-1")
        first = self.machine.apply(
            result=self.normal_result,
            current_state=alarm,
            now=self.start + timedelta(minutes=6),
        )
        self.assertEqual(first.next.state, AlarmLifecycleState.RECOVERY)
        self.assertEqual(first.actions[0].action_type, AlarmActionType.START_RECOVERY)
        second = self.machine.apply(
            result=self.normal_result,
            current_state=first.next,
            now=self.start + timedelta(minutes=7),
        )
        self.assertEqual(second.next.state, AlarmLifecycleState.NORMAL)
        self.assertEqual(second.actions[0].action_type, AlarmActionType.CLOSE_ALARM_EVENT)

    def test_recovery_window_can_be_enabled(self) -> None:
        machine = AlarmStateMachine(recovery_after=timedelta(minutes=2))
        alarm = AlarmState("TH-03", AlarmLifecycleState.ALARM, active_alarm_id="event-1")
        first = machine.apply(
            result=self.normal_result,
            current_state=alarm,
            now=self.start,
        )
        self.assertEqual(first.next.state, AlarmLifecycleState.RECOVERY)
        second = machine.apply(
            result=self.normal_result,
            current_state=first.next,
            now=self.start + timedelta(minutes=2),
        )
        self.assertEqual(second.next.state, AlarmLifecycleState.NORMAL)
        self.assertEqual(second.actions[0].action_type, AlarmActionType.CLOSE_ALARM_EVENT)


if __name__ == "__main__":
    unittest.main()
