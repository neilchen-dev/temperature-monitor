"""Pure alarm lifecycle state machine."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import (
    AlarmAction,
    AlarmActionType,
    AlarmLifecycleState,
    AlarmState,
    MonitorResult,
    OverallStatus,
    StateTransition,
)


class AlarmStateMachine:
    """Advance alarm state without performing any external side effect.

    ``verify_after`` represents the continuous-violation debounce window.
    ``recovery_after`` is configurable so the same machine supports both an
    immediate recovery and a consecutive-normal confirmation window.
    """

    def __init__(
        self,
        *,
        verify_after: timedelta = timedelta(minutes=5),
        recovery_after: timedelta = timedelta(minutes=1),
    ) -> None:
        if verify_after < timedelta(0):
            raise ValueError("verify_after cannot be negative")
        if recovery_after < timedelta(0):
            raise ValueError("recovery_after cannot be negative")
        self.verify_after = verify_after
        self.recovery_after = recovery_after

    def apply(
        self,
        *,
        result: MonitorResult,
        current_state: AlarmState,
        now: datetime,
    ) -> StateTransition:
        """Return the next state and declarative actions for one evaluation."""
        if result.device_id != current_state.device_id:
            raise ValueError("result.device_id must match current_state.device_id")

        state = AlarmLifecycleState(current_state.state)
        if state is AlarmLifecycleState.NORMAL:
            return self._from_normal(result, current_state, now)
        if state is AlarmLifecycleState.PENDING:
            return self._from_pending(result, current_state, now)
        if state is AlarmLifecycleState.ALARM:
            return self._from_alarm(result, current_state, now)
        if state is AlarmLifecycleState.RECOVERY:
            return self._from_recovery(result, current_state, now)
        raise ValueError(f"unsupported alarm state: {state!r}")

    @staticmethod
    def _same(
        state: AlarmState,
        *,
        reason: str,
        actions: tuple[AlarmAction, ...] = (),
    ) -> StateTransition:
        return StateTransition(previous=state, next=state, actions=actions, reason=reason)

    def _from_normal(
        self,
        result: MonitorResult,
        state: AlarmState,
        now: datetime,
    ) -> StateTransition:
        if OverallStatus(result.overall_status) is not OverallStatus.VIOLATION:
            return self._same(state, reason="normal_sample")

        next_state = AlarmState(
            device_id=state.device_id,
            state=AlarmLifecycleState.PENDING,
            violation_started_at=now,
        )
        action = AlarmAction(
            action_type=AlarmActionType.CREATE_VERIFY_TASK,
            device_id=state.device_id,
            run_at=now + self.verify_after,
        )
        return StateTransition(
            previous=state,
            next=next_state,
            actions=(action,),
            reason="violation_started",
        )

    def _from_pending(
        self,
        result: MonitorResult,
        state: AlarmState,
        now: datetime,
    ) -> StateTransition:
        status = OverallStatus(result.overall_status)
        if status is OverallStatus.UNKNOWN:
            return self._same(state, reason="pending_unknown_sample")
        if status is OverallStatus.NORMAL:
            actions = self._cancel_task_action(state)
            next_state = AlarmState.normal(state.device_id)
            return StateTransition(
                previous=state,
                next=next_state,
                actions=actions,
                reason="violation_recovered_before_verification",
            )

        started_at = state.violation_started_at or now
        if now - started_at < self.verify_after:
            return self._same(state, reason="verification_window_open")

        actions = self._complete_task_action(state) + (
            AlarmAction(
                action_type=AlarmActionType.CREATE_ALARM_EVENT,
                device_id=state.device_id,
            ),
        )
        next_state = AlarmState(
            device_id=state.device_id,
            state=AlarmLifecycleState.ALARM,
            violation_started_at=started_at,
            alarm_started_at=now,
        )
        return StateTransition(
            previous=state,
            next=next_state,
            actions=actions,
            reason="violation_verified",
        )

    def _from_alarm(
        self,
        result: MonitorResult,
        state: AlarmState,
        now: datetime,
    ) -> StateTransition:
        status = OverallStatus(result.overall_status)
        if status is OverallStatus.VIOLATION:
            action = AlarmAction(
                action_type=AlarmActionType.UPDATE_ALARM_EVENT,
                device_id=state.device_id,
                alarm_id=state.active_alarm_id,
            )
            return self._same(state, reason="alarm_still_active", actions=(action,))
        if status is OverallStatus.UNKNOWN:
            return self._same(state, reason="alarm_unknown_sample")

        if self.recovery_after == timedelta(0):
            next_state = AlarmState.normal(state.device_id)
            action = AlarmAction(
                action_type=AlarmActionType.CLOSE_ALARM_EVENT,
                device_id=state.device_id,
                alarm_id=state.active_alarm_id,
            )
            return StateTransition(
                previous=state,
                next=next_state,
                actions=(action,),
                reason="alarm_recovered",
            )

        next_state = AlarmState(
            device_id=state.device_id,
            state=AlarmLifecycleState.RECOVERY,
            violation_started_at=state.violation_started_at,
            alarm_started_at=state.alarm_started_at,
            recovery_started_at=now,
            active_alarm_id=state.active_alarm_id,
        )
        action = AlarmAction(
            action_type=AlarmActionType.START_RECOVERY,
            device_id=state.device_id,
            alarm_id=state.active_alarm_id,
        )
        return StateTransition(
            previous=state,
            next=next_state,
            actions=(action,),
            reason="recovery_confirmation_started",
        )

    def _from_recovery(
        self,
        result: MonitorResult,
        state: AlarmState,
        now: datetime,
    ) -> StateTransition:
        status = OverallStatus(result.overall_status)
        if status is OverallStatus.VIOLATION:
            next_state = AlarmState(
                device_id=state.device_id,
                state=AlarmLifecycleState.ALARM,
                violation_started_at=state.violation_started_at,
                alarm_started_at=state.alarm_started_at,
                active_alarm_id=state.active_alarm_id,
            )
            action = AlarmAction(
                action_type=AlarmActionType.UPDATE_ALARM_EVENT,
                device_id=state.device_id,
                alarm_id=state.active_alarm_id,
            )
            return StateTransition(
                previous=state,
                next=next_state,
                actions=(action,),
                reason="violation_returned_during_recovery",
            )
        if status is OverallStatus.UNKNOWN:
            return self._same(state, reason="recovery_unknown_sample")

        started_at = state.recovery_started_at or now
        if now - started_at < self.recovery_after:
            return self._same(state, reason="recovery_window_open")

        next_state = AlarmState.normal(state.device_id)
        action = AlarmAction(
            action_type=AlarmActionType.CLOSE_ALARM_EVENT,
            device_id=state.device_id,
            alarm_id=state.active_alarm_id,
        )
        return StateTransition(
            previous=state,
            next=next_state,
            actions=(action,),
            reason="recovery_confirmed",
        )

    @staticmethod
    def _cancel_task_action(state: AlarmState) -> tuple[AlarmAction, ...]:
        if state.pending_task_id is None:
            return ()
        return (
            AlarmAction(
                action_type=AlarmActionType.CANCEL_VERIFY_TASK,
                device_id=state.device_id,
            ),
        )

    @staticmethod
    def _complete_task_action(state: AlarmState) -> tuple[AlarmAction, ...]:
        if state.pending_task_id is None:
            return ()
        return (
            AlarmAction(
                action_type=AlarmActionType.COMPLETE_VERIFY_TASK,
                device_id=state.device_id,
            ),
        )
