"""Application service connecting acquisition data to the domain pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from domain.models import (
    AlarmState,
    DeviceContext,
    MonitorResult,
    MonitorSample,
    OperationState,
    StateTransition,
    AutomationTask,
)
from domain.monitor_engine import MonitorEngine
from domain.standard_resolver import StandardNotFoundError, StandardResolver

from .action_executor import ActionExecution, ActionExecutor
from .actions import ApplicationAction, ApplicationActionMapper


class OperationStateProvider(Protocol):
    def get(self, device: DeviceContext) -> OperationState:
        """Return the current operation context for a device."""


class AlarmStateRepository(Protocol):
    def get(self, device_id: str) -> AlarmState | None:
        """Return the last persisted alarm state, if present."""

    def save(self, state: AlarmState) -> None:
        """Persist the next alarm state."""


class LatestSampleRepository(Protocol):
    def save(self, sample: MonitorSample) -> None:
        """Persist the latest sample for delayed verification."""


class AutomationTaskRepository(Protocol):
    def create_or_get(
        self,
        *,
        task_type: str,
        entity_type: str,
        entity_id: str,
        due_at: datetime,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: str | None = None,
        created_at: datetime,
    ) -> AutomationTask:
        """Create or reuse a local durable task."""

    def cancel(self, task_id: str, *, updated_at: datetime) -> AutomationTask:
        """Cancel a pending task."""


class LocalEnvironmentEventRepository(Protocol):
    def create_or_get_active(
        self,
        *,
        device_id: str,
        event_key: str,
        opened_at: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        """Create an application-owned projected event."""

    def close(self, event_id: str, *, closed_at: datetime) -> Any:
        """Close an application-owned projected event."""


@dataclass(frozen=True)
class MonitorHandlingResult:
    operation_state: OperationState
    monitor_result: MonitorResult
    transition: StateTransition
    actions: tuple[ApplicationAction, ...]
    executions: tuple[ActionExecution, ...]


class MonitorApplicationService:
    """Run one sample through the complete domain/application pipeline."""

    def __init__(
        self,
        *,
        operation_state_provider: OperationStateProvider,
        standard_resolver: StandardResolver,
        alarm_state_repository: AlarmStateRepository,
        alarm_state_machine: Any,
        action_mapper: ApplicationActionMapper,
        action_executor: ActionExecutor,
        now_provider: Callable[[], datetime] | None = None,
        task_repository: AutomationTaskRepository | None = None,
        event_repository: LocalEnvironmentEventRepository | None = None,
        latest_sample_repository: LatestSampleRepository | None = None,
    ) -> None:
        self.operation_state_provider = operation_state_provider
        self.standard_resolver = standard_resolver
        self.alarm_state_repository = alarm_state_repository
        self.alarm_state_machine = alarm_state_machine
        self.action_mapper = action_mapper
        self.action_executor = action_executor
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())
        self.task_repository = task_repository
        self.event_repository = event_repository
        self.latest_sample_repository = latest_sample_repository

    def handle_sample(
        self,
        *,
        device: DeviceContext,
        sample: MonitorSample,
        now: datetime | None = None,
        scheduler_task_id: str | None = None,
    ) -> MonitorHandlingResult:
        if self.latest_sample_repository is not None:
            self.latest_sample_repository.save(sample)
        operation_state = self.operation_state_provider.get(device)
        try:
            standard = self.standard_resolver.resolve(
                area_id=operation_state.area_id,
                operation_type=operation_state.operation_type,
                timestamp=sample.sample_time,
                device_id=sample.device_id,
            )
        except StandardNotFoundError:
            # A missing standard is a visible domain result, not a normal
            # reading and not a reason to create an alarm.
            standard = None
        monitor_result = MonitorEngine.evaluate(
            device=device,
            sample=sample,
            standard=standard,
            operation_state=operation_state,
        )
        current_state = self.alarm_state_repository.get(device.device_id)
        if current_state is None:
            current_state = AlarmState.normal(device.device_id)
        evaluated_at = now or self.now_provider()
        transition = self.alarm_state_machine.apply(
            result=monitor_result,
            current_state=current_state,
            now=evaluated_at,
        )
        actions = self.action_mapper.map(transition)
        transition = self._project_local_actions(
            transition,
            actions,
            sample=sample,
            created_at=evaluated_at,
            scheduler_task_id=scheduler_task_id,
        )
        self.alarm_state_repository.save(transition.next)
        executions = self.action_executor.execute(
            actions,
            context={
                "sample_time": sample.sample_time.isoformat(),
                "python_monitor_result": _monitor_result_dict(monitor_result),
                "python_alarm_transition": _transition_dict(transition),
                "operation_state": _operation_state_dict(operation_state),
            },
            created_at=evaluated_at,
        )
        return MonitorHandlingResult(
            operation_state=operation_state,
            monitor_result=monitor_result,
            transition=transition,
            actions=actions,
            executions=executions,
        )

    def _project_local_actions(
        self,
        transition: StateTransition,
        actions: tuple[ApplicationAction, ...],
        *,
        sample: MonitorSample,
        created_at: datetime,
        scheduler_task_id: str | None,
    ) -> StateTransition:
        """Project domain actions into Python-owned durable state.

        This is intentionally the only runtime-side effect path.  It knows
        about SQLite repositories, but no Feishu client and no Feishu write
        operation can enter this method.
        """
        next_state = transition.next
        previous_task_id = transition.previous.pending_task_id

        if self.task_repository is not None and previous_task_id is not None:
            leaves_recovery = (
                transition.previous.state.value == "RECOVERY"
                and next_state.state.value != "RECOVERY"
            )
            has_task_action = any(
                action.action_type.value in {"CANCEL_VERIFY_TASK", "COMPLETE_VERIFY_TASK"}
                for action in actions
            )
            if leaves_recovery and not has_task_action:
                self._finish_or_skip_task(
                    previous_task_id,
                    created_at=created_at,
                    scheduler_task_id=scheduler_task_id,
                )
                next_state = replace(next_state, pending_task_id=None)

        for action in actions:
            if action.kind.value == "SCHEDULE_TASK" and self.task_repository is not None:
                task = self.task_repository.create_or_get(
                    task_type=action.task_type or action.action_type.value,
                    entity_type=action.entity_type or "DEVICE",
                    entity_id=action.entity_id or sample.device_id,
                    due_at=action.run_at or created_at,
                    payload={
                        **dict(action.payload),
                        "device_id": sample.device_id,
                        "sample_time": sample.sample_time.isoformat(),
                    },
                    dedupe_key=action.dedupe_key,
                    created_at=created_at,
                )
                next_state = replace(next_state, pending_task_id=task.task_id)

            if action.action_type.value == "START_RECOVERY":
                next_state = self._schedule_recovery_task(
                    next_state,
                    created_at=created_at,
                    sample=sample,
                )

            if action.action_type.value in {"CANCEL_VERIFY_TASK", "COMPLETE_VERIFY_TASK"}:
                task_id = transition.previous.pending_task_id
                if task_id is not None:
                    self._finish_or_skip_task(
                        task_id,
                        created_at=created_at,
                        scheduler_task_id=scheduler_task_id,
                    )
                next_state = replace(next_state, pending_task_id=None)

            if (
                action.action_type.value == "CREATE_ALARM_EVENT"
                and self.event_repository is not None
            ):
                event_key = (
                    f"ENV:{sample.device_id}:"
                    f"{next_state.violation_started_at.isoformat() if next_state.violation_started_at else created_at.isoformat()}"
                )
                event = self.event_repository.create_or_get_active(
                    device_id=sample.device_id,
                    event_key=event_key,
                    opened_at=created_at,
                    payload={
                        "projection": "local_shadow_event",
                        "sample_time": sample.sample_time.isoformat(),
                    },
                )
                next_state = replace(next_state, active_alarm_id=event.event_id)

            if action.action_type.value == "CLOSE_ALARM_EVENT":
                event_id = action.alarm_id or transition.previous.active_alarm_id
                if event_id is not None and self.event_repository is not None:
                    self.event_repository.close(event_id, closed_at=created_at)

        return replace(transition, next=next_state)

    def _schedule_recovery_task(
        self,
        state: AlarmState,
        *,
        created_at: datetime,
        sample: MonitorSample,
    ) -> AlarmState:
        if self.task_repository is None:
            return state
        recovery_after = getattr(self.alarm_state_machine, "recovery_after", None)
        if recovery_after is None or recovery_after.total_seconds() <= 0:
            return state
        recovery_started = state.recovery_started_at or created_at
        alarm_id = state.active_alarm_id or "unknown"
        task = self.task_repository.create_or_get(
            task_type="VERIFY_RECOVERY",
            entity_type="DEVICE",
            entity_id=sample.device_id,
            due_at=recovery_started + recovery_after,
            payload={
                "device_id": sample.device_id,
                "sample_time": sample.sample_time.isoformat(),
                "alarm_id": state.active_alarm_id,
            },
            dedupe_key=(
                f"VERIFY_RECOVERY:{sample.device_id}:{alarm_id}:"
                f"{recovery_started.isoformat()}"
            ),
            created_at=created_at,
        )
        return replace(state, pending_task_id=task.task_id)

    def _finish_or_skip_task(
        self,
        task_id: str,
        *,
        created_at: datetime,
        scheduler_task_id: str | None,
    ) -> None:
        if task_id == scheduler_task_id:
            return
        if self.task_repository is None:
            return
        try:
            self.task_repository.cancel(task_id, updated_at=created_at)
        except (KeyError, ValueError):
            # A stale task may already have been reclaimed or completed.  The
            # state transition itself remains authoritative and idempotent.
            pass


def _monitor_result_dict(result: MonitorResult) -> dict[str, Any]:
    return {
        "device_id": result.device_id,
        "sample_time": result.sample_time.isoformat(),
        "temperature": result.temperature,
        "humidity": result.humidity,
        "temperature_status": result.temperature_status.value,
        "humidity_status": result.humidity_status.value,
        "overall_status": result.overall_status.value,
        "standard_id": result.standard_id,
        "standard_revision": result.standard_revision,
        "applicability": result.applicability.value,
        "data_quality": result.data_quality.value,
        "reasons": result.reasons,
    }


def _transition_dict(transition: StateTransition) -> dict[str, Any]:
    return {
        "from": _enum_value(transition.previous.state),
        "to": _enum_value(transition.next.state),
        "reason": transition.reason,
        "actions": [action.action_type.value for action in transition.actions],
    }


def _operation_state_dict(state: OperationState) -> dict[str, Any]:
    return {
        "area_id": state.area_id,
        "state": _enum_value(state.state),
        "operation_type": state.operation_type,
        "work_order": state.work_order,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "ended_at": state.ended_at.isoformat() if state.ended_at else None,
    }


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value
