"""Application service connecting acquisition data to the domain pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
from typing import Any, Callable, Mapping, Protocol

from domain.models import (
    AlarmAction,
    AlarmActionType,
    AlarmLifecycleState,
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

from .action_executor import (
    ActionExecution,
    ActionExecutionStatus,
    ActionExecutor,
    AutomationMode,
)
from .actions import ApplicationAction, ApplicationActionMapper
from .active_scope import active_scope_allows, normalize_device_id


logger = logging.getLogger("temperature_monitor")


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

    def create_or_get_unfinished(
        self,
        *,
        task_type: str,
        entity_type: str,
        entity_id: str,
        due_at: datetime,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: str,
        created_at: datetime,
    ) -> AutomationTask:
        """Create or reuse one unfinished task for a business identity."""


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

    def mark_recovered(self, event_id: str, *, recovered_at: datetime) -> Any:
        """Finish the monitoring cycle without claiming business closure."""


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
        if monitor_result.control_type_consistency == "mismatch" and standard is not None:
            legacy_control = getattr(device.control_type, "value", device.control_type)
            logger.warning(
                "control_type mismatch | device_id=%s | standard_id=%s | revision=%s "
                "| standard_control_type=%s | legacy_control_type=%s | source=standard_table",
                device.device_id,
                standard.standard_id,
                standard.revision,
                standard.control_type.value if standard.control_type is not None else None,
                legacy_control,
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
        event_reconciliation_task_ids = self._prepare_event_reconciliation_tasks(
            transition,
            actions,
            sample=sample,
            monitor_result=monitor_result,
            operation_state=operation_state,
            created_at=evaluated_at,
        )
        self.alarm_state_repository.save(transition.next)
        executions = self.action_executor.execute(
            actions,
            context={
                "device_id": sample.device_id,
                "created_at": evaluated_at.isoformat(),
                "sample_time": sample.sample_time.isoformat(),
                "sample": _sample_dict(sample),
                "python_monitor_result": _monitor_result_dict(monitor_result),
                "python_alarm_transition": _transition_dict(transition),
                "operation_state": _operation_state_dict(operation_state),
            },
            created_at=evaluated_at,
        )
        self._finish_successful_event_reconciliation_tasks(
            event_reconciliation_task_ids,
            executions,
            updated_at=evaluated_at,
        )
        return MonitorHandlingResult(
            operation_state=operation_state,
            monitor_result=monitor_result,
            transition=transition,
            actions=actions,
            executions=executions,
        )

    def reconcile_alarm_event_task(
        self,
        *,
        task: AutomationTask,
        device: DeviceContext,
        now: datetime,
    ) -> tuple[ActionExecution, ...]:
        """Retry an Active Feishu event projection from durable task data.

        This intentionally does not call ``handle_sample``: a later normal or
        unknown sample must not advance the alarm state while an older failed
        CREATE is being reconciled.  The persisted ALARM state is the guard;
        the task payload is the last known violating snapshot used to create
        the same business event.
        """
        device_id = normalize_device_id(task.entity_id)
        if device_id is None or device_id != normalize_device_id(device.device_id):
            raise ValueError("alarm event reconciliation device mismatch")
        state = self.alarm_state_repository.get(device_id)
        if state is None or AlarmLifecycleState(state.state) is not AlarmLifecycleState.ALARM:
            return ()

        expected_start = _parse_payload_datetime(task.payload.get("violation_started_at"))
        current_start = state.violation_started_at or state.alarm_started_at
        if expected_start is not None and current_start is not None:
            if _same_instant(expected_start, current_start) is False:
                # A retry from an older alarm must not create/update the new
                # alarm event after a fast recover/re-alarm cycle.
                return ()

        source_action = AlarmAction(
            action_type=AlarmActionType.UPDATE_ALARM_EVENT,
            device_id=device_id,
            alarm_id=state.active_alarm_id,
        )
        transition = StateTransition(
            previous=state,
            next=state,
            actions=(source_action,),
            reason="alarm_event_reconciliation",
        )
        actions = self.action_mapper.map(transition)
        payload = task.payload
        sample_time = str(payload.get("sample_time") or now.isoformat())
        context = {
            "device_id": device_id,
            "created_at": now.isoformat(),
            "sample_time": sample_time,
            "sample": {
                "device_id": device_id,
                "sample_time": sample_time,
                "temperature": payload.get("temperature"),
                "humidity": payload.get("humidity"),
                "online_status": payload.get("online_status"),
                "data_quality": payload.get("data_quality"),
            },
            "python_monitor_result": {
                "temperature_status": payload.get("temperature_status") or "",
                "humidity_status": payload.get("humidity_status") or "",
            },
            "python_alarm_transition": {
                "from": AlarmLifecycleState.ALARM.value,
                "to": AlarmLifecycleState.ALARM.value,
                "reason": "alarm_event_reconciliation",
                "violation_started_at": (
                    expected_start.isoformat() if expected_start is not None else None
                ),
                "alarm_started_at": payload.get("alarm_started_at"),
                "active_alarm_id": state.active_alarm_id,
            },
            "operation_state": {"area_id": str(payload.get("area") or "").strip()},
        }
        executions = self.action_executor.execute(
            actions,
            context=context,
            created_at=now,
        )
        failed = next(
            (
                execution
                for execution in executions
                if execution.status is ActionExecutionStatus.FAILED
            ),
            None,
        )
        if failed is not None:
            self._schedule_event_reconciliation_retry(task, now=now)
            raise RuntimeError(failed.error or "alarm event reconciliation failed")
        return executions

    def _prepare_event_reconciliation_tasks(
        self,
        transition: StateTransition,
        actions: tuple[ApplicationAction, ...],
        *,
        sample: MonitorSample,
        monitor_result: MonitorResult,
        operation_state: OperationState,
        created_at: datetime,
    ) -> tuple[str, ...]:
        """Durably arm a safety retry before invoking an external event write."""
        if not self._active_event_writes_enabled(sample.device_id):
            return ()
        if self.task_repository is None:
            return ()
        event_actions = tuple(
            action
            for action in actions
            if action.action_type
            in {
                AlarmActionType.CREATE_ALARM_EVENT,
                AlarmActionType.UPDATE_ALARM_EVENT,
            }
        )
        if not event_actions:
            return ()
        started_at = (
            transition.next.violation_started_at
            or transition.next.alarm_started_at
            or created_at
        )
        normalized_device = normalize_device_id(sample.device_id) or sample.device_id
        payload = {
            "device_id": normalized_device,
            "area": operation_state.area_id,
            "sample_time": sample.sample_time.isoformat(),
            "temperature": sample.temperature,
            "humidity": sample.humidity,
            "online_status": sample.online_status,
            "data_quality": _enum_value(sample.data_quality),
            "temperature_status": _enum_value(monitor_result.temperature_status),
            "humidity_status": _enum_value(monitor_result.humidity_status),
            "violation_started_at": started_at.isoformat(),
            "alarm_started_at": (
                transition.next.alarm_started_at.isoformat()
                if transition.next.alarm_started_at is not None
                else None
            ),
            "retry_attempt": 0,
        }
        task_ids: list[str] = []
        for _ in event_actions:
            task = self.task_repository.create_or_get_unfinished(
                task_type="RECONCILE_ALARM_EVENT",
                entity_type="DEVICE",
                entity_id=normalized_device,
                due_at=created_at,
                payload=payload,
                dedupe_key=_event_reconciliation_key(normalized_device, started_at),
                created_at=created_at,
            )
            task_ids.append(task.task_id)
        return tuple(task_ids)

    def _finish_successful_event_reconciliation_tasks(
        self,
        task_ids: tuple[str, ...],
        executions: tuple[ActionExecution, ...],
        *,
        updated_at: datetime,
    ) -> None:
        if self.task_repository is None or not task_ids:
            return
        task_index = 0
        for execution in executions:
            if execution.action.action_type not in {
                AlarmActionType.CREATE_ALARM_EVENT,
                AlarmActionType.UPDATE_ALARM_EVENT,
            }:
                continue
            if task_index >= len(task_ids):
                break
            task_id = task_ids[task_index]
            task_index += 1
            if execution.status is ActionExecutionStatus.FAILED:
                continue
            try:
                self.task_repository.cancel(task_id, updated_at=updated_at)
            except (KeyError, ValueError):
                # A scheduler may have reclaimed the safety task already; its
                # own execution remains a valid idempotent retry path.
                logger.debug(
                    "event reconciliation task was already claimed or finished | task=%s",
                    task_id,
                )

    def _schedule_event_reconciliation_retry(
        self,
        task: AutomationTask,
        *,
        now: datetime,
    ) -> None:
        if self.task_repository is None:
            return
        attempt = _retry_attempt(task.payload) + 1
        device_id = normalize_device_id(task.entity_id) or str(task.entity_id).strip()
        started_at = _parse_payload_datetime(task.payload.get("violation_started_at"))
        if started_at is None:
            started_at = now
        payload = dict(task.payload)
        payload["retry_attempt"] = attempt
        delay = min(
            _active_event_retry_base_seconds() * (2 ** max(attempt - 1, 0)),
            _active_event_retry_max_seconds(),
        )
        self.task_repository.create_or_get(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id=device_id,
            due_at=now + timedelta(seconds=delay),
            payload=payload,
            dedupe_key=(
                f"{_event_reconciliation_key(device_id, started_at)}:retry:{attempt}"
            ),
            created_at=now,
        )

    def _active_event_writes_enabled(self, device_id: str) -> bool:
        mode = getattr(self.action_executor, "mode", None)
        mode_value = getattr(mode, "value", mode)
        if mode_value != AutomationMode.ACTIVE.value:
            return False
        return active_scope_allows(
            device_id,
            active_device_ids=getattr(self.action_executor, "active_device_ids", ()),
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

        This is the Python-owned projection path.  It knows about SQLite
        repositories only; Feishu writes remain behind ActionExecutor's
        explicitly injected Active handlers.
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

            if action.action_type.value == "MARK_ALARM_RECOVERED":
                event_id = action.alarm_id or transition.previous.active_alarm_id
                if event_id is not None and self.event_repository is not None:
                    self.event_repository.mark_recovered(event_id, recovered_at=created_at)

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
        "resolved_control_type": (
            result.resolved_control_type.value
            if result.resolved_control_type is not None
            else None
        ),
        "control_type_source": result.control_type_source,
        "control_type_consistency": result.control_type_consistency,
        "reasons": result.reasons,
    }


def _transition_dict(transition: StateTransition) -> dict[str, Any]:
    violation_started_at = (
        transition.next.violation_started_at or transition.previous.violation_started_at
    )
    alarm_started_at = transition.next.alarm_started_at or transition.previous.alarm_started_at
    active_alarm_id = transition.next.active_alarm_id or transition.previous.active_alarm_id
    return {
        "from": _enum_value(transition.previous.state),
        "to": _enum_value(transition.next.state),
        "reason": transition.reason,
        "actions": [action.action_type.value for action in transition.actions],
        "violation_started_at": (
            violation_started_at.isoformat()
            if violation_started_at
            else None
        ),
        "alarm_started_at": (
            alarm_started_at.isoformat()
            if alarm_started_at
            else None
        ),
        "recovery_started_at": (
            transition.next.recovery_started_at.isoformat()
            if transition.next.recovery_started_at
            else None
        ),
        "active_alarm_id": active_alarm_id,
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


def _sample_dict(sample: MonitorSample) -> dict[str, Any]:
    quality = sample.data_quality
    return {
        "device_id": sample.device_id,
        "sample_time": sample.sample_time.isoformat(),
        "temperature": sample.temperature,
        "humidity": sample.humidity,
        "online_status": sample.online_status,
        "data_quality": _enum_value(quality) if quality is not None else None,
    }


def _parse_payload_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _same_instant(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None or right.tzinfo is None:
        return left == right
    return left.astimezone().timestamp() == right.astimezone().timestamp()


def _event_reconciliation_key(device_id: str, started_at: datetime) -> str:
    return f"RECONCILE_ALARM_EVENT:{device_id}:{started_at.isoformat()}"


def _retry_attempt(payload: Mapping[str, Any]) -> int:
    try:
        return max(0, int(payload.get("retry_attempt", 0)))
    except (TypeError, ValueError):
        return 0


def _active_event_retry_base_seconds() -> float:
    # Keep this fallback local so older test/application configurations can
    # load the new reconciliation path without requiring a config migration.
    import config

    return max(
        1.0,
        float(
            getattr(
                config,
                "ACTIVE_EVENT_RECONCILIATION_BACKOFF_SECONDS",
                getattr(config, "FEISHU_PROJECTION_BACKOFF_SECONDS", 30.0),
            )
        ),
    )


def _active_event_retry_max_seconds() -> float:
    import config

    return max(
        _active_event_retry_base_seconds(),
        float(
            getattr(
                config,
                "ACTIVE_EVENT_RECONCILIATION_MAX_BACKOFF_SECONDS",
                600.0,
            )
        ),
    )
