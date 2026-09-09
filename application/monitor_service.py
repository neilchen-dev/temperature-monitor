"""Application service connecting acquisition data to the domain pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
from typing import Any, Callable, Mapping, Protocol

import config
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
from services.event_identity import epoch_milliseconds, external_effect_key

from .action_executor import (
    ActionExecution,
    ActionExecutionStatus,
    ActionExecutor,
    AutomationMode,
)
from .actions import ApplicationAction, ApplicationActionKind, ApplicationActionMapper
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

    def get(self, task_id: str) -> AutomationTask | None:
        """Return one durable task by identity."""

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

    def reschedule_running(self, task: AutomationTask, *, due_at: datetime,
                           updated_at: datetime, payload: Mapping[str, Any]) -> None:
        """Reschedule the same owned task without replacing its identity."""


class LocalEnvironmentEventRepository(Protocol):
    def patch_external_projection(self, event_id: str, **values: Any) -> Any:
        """Atomically merge durable external projection metadata."""

    def get(self, event_id: str) -> Any | None:
        """Return a local event by its durable identity."""

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

    def mark_external_binding_pending(
        self,
        event_id: str,
        *,
        requested_at: datetime,
    ) -> Any:
        """Persist that a Feishu CREATE must still be reconciled."""


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
                "| standard_control_type=%s | legacy_control_type=%s | standard_source=%s",
                device.device_id,
                standard.standard_id,
                standard.revision,
                standard.control_type.value if standard.control_type is not None else None,
                legacy_control,
                standard.standard_source,
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
            operation_state=operation_state,
            created_at=evaluated_at,
            scheduler_task_id=scheduler_task_id,
        )
        actions = self._enrich_action_metadata(
            transition,
            actions,
            sample=sample,
            monitor_result=monitor_result,
            created_at=evaluated_at,
            scheduler_task_id=scheduler_task_id,
        )
        event_reconciliation_task_ids = self._prepare_event_reconciliation_tasks(
            transition,
            actions,
            sample=sample,
            monitor_result=monitor_result,
            operation_state=operation_state,
            standard=standard,
            created_at=evaluated_at,
        )
        actions = self._attach_reconciliation_task_metadata(
            actions,
            event_reconciliation_task_ids,
            event_id=transition.next.active_alarm_id or transition.previous.active_alarm_id,
        )
        notification_task_ids = self._prepare_notification_tasks(
            transition,
            actions,
            sample=sample,
            monitor_result=monitor_result,
            operation_state=operation_state,
            standard=standard,
            created_at=evaluated_at,
        )
        actions = self._attach_notification_task_metadata(
            actions,
            notification_task_ids,
            event_id=transition.next.active_alarm_id or transition.previous.active_alarm_id,
        )
        self.alarm_state_repository.save(transition.next)
        context = {
            "device_id": sample.device_id,
            "automation_task_id": scheduler_task_id,
            "created_at": evaluated_at.isoformat(),
            "sample_time": sample.sample_time.isoformat(),
            "sample": _sample_dict(sample),
            "python_monitor_result": _monitor_result_dict(monitor_result),
            "python_alarm_transition": _transition_dict(transition),
            "operation_state": _operation_state_dict(operation_state),
            "standard": _standard_dict(standard, monitor_result),
        }
        executions = self.action_executor.execute(
            actions,
            context=context,
            created_at=evaluated_at,
        )
        self._finish_successful_event_reconciliation_tasks(
            event_reconciliation_task_ids,
            executions,
            updated_at=evaluated_at,
        )
        self._finish_successful_notification_tasks(
            notification_task_ids,
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

    def execute_notification_task(
        self,
        *,
        task: AutomationTask,
        now: datetime | None = None,
    ) -> tuple[ActionExecution, ...]:
        """Run one notification task, retaining identity across scheduler retry."""
        action_type = AlarmActionType(task.task_type)
        if action_type not in {
            AlarmActionType.NOTIFY_ALARM,
            AlarmActionType.NOTIFY_RECOVERY,
        }:
            raise ValueError(f"unsupported notification task type: {task.task_type}")
        created_at = now or self.now_provider()
        event_id = str(task.payload.get("event_id") or task.entity_id).strip()
        action = ApplicationAction(
            action_type=action_type,
            kind=ApplicationActionKind.NOTIFICATION,
            device_id=str(task.payload.get("device_id") or event_id),
            source=AlarmAction(
                action_type=action_type,
                device_id=str(task.payload.get("device_id") or event_id),
                alarm_id=event_id,
            ),
            alarm_id=event_id,
            task_id=task.task_id,
            dedupe_key=task.dedupe_key,
            payload=dict(task.payload),
        )
        context = dict(task.payload)
        context.update(
            {
                "device_id": str(task.payload.get("device_id") or event_id),
                "event_id": event_id,
                "automation_task_id": task.task_id,
                "dedupe_key": task.dedupe_key,
                "created_at": context.get("created_at") or created_at.isoformat(),
                "notification_attempted_at": created_at.isoformat(),
            }
        )
        executions = self.action_executor.execute(
            (action,),
            context=context,
            created_at=created_at,
        )
        execution = executions[0]
        if execution.status is ActionExecutionStatus.SUCCEEDED:
            return executions

        retryable = bool(execution.context.get("retryable"))
        attempt = _retry_attempt(task.payload) + 1
        max_attempts = max(1, int(getattr(config, "FEISHU_NOTIFY_MAX_RETRIES", 5)))
        if retryable and attempt < max_attempts and self.task_repository is not None:
            retry_payload = dict(task.payload)
            retry_payload["retry_attempt"] = attempt
            delay = min(
                _notification_retry_base_seconds()
                * (2 ** min(max(attempt - 1, 0), 20)),
                _notification_retry_max_seconds(),
            )
            due_at = created_at + timedelta(seconds=delay)
            self.task_repository.reschedule_running(
                task,
                due_at=due_at,
                payload=retry_payload,
                updated_at=created_at,
            )
            logger.info(
                "notification_retry | task=%s action_type=%s event_id=%s "
                "attempt=%s due_at=%s error_code=%s",
                task.task_id,
                action_type.value,
                event_id,
                attempt,
                due_at.isoformat(),
                execution.context.get("error_code"),
            )
        raise RuntimeError(
            execution.error
            or execution.context.get("error_code")
            or f"{action_type.value} failed"
        )

    def reconcile_alarm_event_task(
        self, *, task: AutomationTask, device: DeviceContext, now: datetime,
    ) -> tuple[ActionExecution, ...]:
        try:
            return self._reconcile_alarm_event_task(task=task, device=device, now=now)
        except Exception:
            self._schedule_event_reconciliation_retry(task, now=now)
            raise

    def _reconcile_alarm_event_task(
        self,
        *,
        task: AutomationTask,
        device: DeviceContext,
        now: datetime,
    ) -> tuple[ActionExecution, ...]:
        """Retry an Active Feishu event projection from durable task data.

        This intentionally does not call ``handle_sample``: a later normal or
        unknown sample must not advance the alarm state while an older failed
        CREATE is being reconciled. The task payload carries the local event
        identity and last known violating snapshot. An unbound event remains
        eligible even after the local cycle is recovered/CLOSED; a bound event
        is a completed projection and is a no-op.
        """
        device_id = normalize_device_id(task.entity_id)
        if device_id is None or device_id != normalize_device_id(device.device_id):
            raise ValueError("alarm event reconciliation device mismatch")
        expected_start = _parse_payload_datetime(task.payload.get("violation_started_at"))
        local_event_id = str(task.payload.get("local_event_id") or "").strip() or None
        local_event = (
            self.event_repository.get(local_event_id)
            if local_event_id is not None and self.event_repository is not None
            else None
        )
        if local_event is not None:
            if normalize_device_id(local_event.device_id) != device_id:
                raise ValueError("reconciliation local event/device mismatch")
            local_start = _parse_payload_datetime(local_event.payload.get("violation_started_at"))
            if local_start is None and local_event.event_key.startswith("ENV:"):
                local_start = _parse_payload_datetime(local_event.event_key.split(":", 2)[-1])
            if local_start is None:
                raise ValueError("reconciliation cycle start is unknown; manual review required")
            if expected_start is not None and not _same_instant(expected_start, local_start):
                raise ValueError("reconciliation cycle start mismatch; manual review required")
            expected_start = local_start
            if "feishu_create_attempted" not in local_event.payload and not local_event.payload.get("feishu_record_id"):
                self.event_repository.patch_external_projection(
                    local_event.event_id, feishu_create_attempted=True
                )
            if (local_event.payload.get("feishu_record_id")
                    and not local_event.payload.get("feishu_recovery_pending")
                    and not local_event.payload.get("feishu_update_pending")):
                # Binding is an independent durable completion condition. A
                # task left behind after recovery must not issue another UPDATE.
                return ()
            # The durable task itself is sufficient evidence for legacy
            # pre-marker events. New events also carry the PENDING marker, but
            # a recovered task must not depend on the monitoring lifecycle or
            # on that marker having been written by an older deployment.
            binding_pending = True
        else:
            binding_pending = False
            if local_event_id is not None:
                raise ValueError("reconciliation local event is missing; manual review required")

        state = self.alarm_state_repository.get(device_id)
        state_is_alarm = (
            state is not None
            and AlarmLifecycleState(state.state) is AlarmLifecycleState.ALARM
        )
        if not state_is_alarm and not binding_pending:
            raise ValueError("reconciliation has no trustworthy local cycle identity")

        if state is None:
            state = AlarmState.normal(device_id)

        current_start = (
            state.violation_started_at or state.alarm_started_at
            if state is not None and state_is_alarm
            else None
        )
        if (
            not binding_pending
            and expected_start is not None
            and current_start is not None
            and _same_instant(expected_start, current_start) is False
        ):
            # A retry from an older alarm must not create/update the new alarm
            # event after a fast recover/re-alarm cycle.
            raise ValueError("reconciliation cycle differs from current alarm; manual review required")

        if local_event_id is None and state is not None:
            local_event_id = state.active_alarm_id

        source_action = AlarmAction(
            action_type=(
                AlarmActionType.CREATE_ALARM_EVENT
                if local_event is not None and not local_event.payload.get("feishu_record_id")
                else AlarmActionType.UPDATE_ALARM_EVENT
            ),
            device_id=device_id,
            alarm_id=local_event_id,
        )
        recovery_pending = local_event is not None and local_event.payload.get("feishu_recovery_pending")
        source_actions = (source_action,)
        if recovery_pending:
            recovery_action = AlarmAction(
                action_type=AlarmActionType.MARK_ALARM_RECOVERED,
                device_id=device_id,
                alarm_id=local_event_id,
            )
            source_actions = (
                (recovery_action,)
                if local_event.payload.get("feishu_record_id") and not local_event.payload.get("feishu_update_pending")
                else (source_action, recovery_action)
            )
        transition = StateTransition(
            previous=state,
            next=state,
            actions=source_actions,
            reason="alarm_event_reconciliation",
        )
        actions = self.action_mapper.map(transition)
        payload = task.payload
        sample_time = str(payload.get("sample_time") or now.isoformat())
        area = str(payload.get("area") or device.area or "").strip()
        context = {
            "binding_attempt": _retry_attempt(task.payload),
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
                "standard_id": payload.get("standard_id"),
                "standard_revision": payload.get("standard_revision"),
                "standard_source": payload.get("standard_source"),
            },
            "standard": payload.get("standard", {}),
            "python_alarm_transition": {
                "from": AlarmLifecycleState.ALARM.value,
                "to": AlarmLifecycleState.ALARM.value,
                "reason": "alarm_event_reconciliation",
                "violation_started_at": (
                    expected_start.isoformat() if expected_start is not None else None
                ),
                "alarm_started_at": payload.get("alarm_started_at"),
                "active_alarm_id": local_event_id,
            },
            "operation_state": {"area_id": area},
        }
        if recovery_pending:
            context["recovered_at"] = local_event.payload.get("feishu_recovered_at") or (
                local_event.closed_at.isoformat() if local_event.closed_at else None
            )
        if local_event is not None and local_event.payload.get("feishu_update_pending"):
            snapshot = local_event.payload.get("feishu_update_snapshot", {})
            context["sample"].update({key: snapshot.get(key) for key in ("temperature", "humidity")})
            context["python_monitor_result"].update({key: snapshot.get(key) for key in ("temperature_status", "humidity_status")})
        actions = self._enrich_reconciliation_actions(
            actions,
            context=context,
            task_id=task.task_id,
            local_event_id=local_event_id,
            now=now,
        )
        notification_actions = tuple(
            action
            for action in actions
            if action.kind is ApplicationActionKind.NOTIFICATION
        )
        notification_task_ids = self._prepare_notification_tasks_from_context(
            notification_actions,
            context=context,
            created_at=now,
        )
        actions = self._attach_notification_task_metadata(
            actions,
            notification_task_ids,
            event_id=local_event_id,
        )
        event_actions = tuple(
            action
            for action in actions
            if action.kind is not ApplicationActionKind.NOTIFICATION
        )
        executions = self.action_executor.execute(
            event_actions,
            context=context,
            created_at=now,
        )
        failed = next(
            (
                execution
                for execution in executions
                if execution.status is not ActionExecutionStatus.SUCCEEDED
            ),
            None,
        )
        if failed is not None:
            raise RuntimeError(failed.error or "alarm event reconciliation failed")
        return executions

    def _enrich_action_metadata(
        self,
        transition: StateTransition,
        actions: tuple[ApplicationAction, ...],
        *,
        sample: MonitorSample,
        monitor_result: MonitorResult,
        created_at: datetime,
        scheduler_task_id: str | None,
    ) -> tuple[ApplicationAction, ...]:
        """Attach durable event/task identities before an action is audited."""
        event_id = transition.next.active_alarm_id or transition.previous.active_alarm_id
        monitor_result_dict = _monitor_result_dict(monitor_result)
        enriched: list[ApplicationAction] = []
        for action in actions:
            action_type = action.action_type.value
            task_id = action.task_id
            if action_type in {"CREATE_VERIFY_TASK", "START_RECOVERY"}:
                task_id = transition.next.pending_task_id or scheduler_task_id
            elif action_type in {"CANCEL_VERIFY_TASK", "COMPLETE_VERIFY_TASK"}:
                task_id = transition.previous.pending_task_id or scheduler_task_id
            elif action_type in {
                AlarmActionType.NOTIFY_ALARM.value,
                AlarmActionType.NOTIFY_RECOVERY.value,
            }:
                # Notification tasks have their own durable identity.  Do not
                # accidentally attribute them to the sample/verification task.
                task_id = action.task_id
            elif task_id is None:
                task_id = scheduler_task_id

            alarm_id = action.alarm_id or (
                event_id if action_type in {
                    "CREATE_ALARM_EVENT",
                    "UPDATE_ALARM_EVENT",
                    "START_RECOVERY",
                    "MARK_ALARM_RECOVERED",
                    "NOTIFY_ALARM",
                    "NOTIFY_RECOVERY",
                } else None
            )
            payload = dict(action.payload)
            dedupe_key = action.dedupe_key
            if alarm_id is not None and action_type == AlarmActionType.NOTIFY_ALARM.value:
                dedupe_key = f"NOTIFY_ALARM:{alarm_id}"
                payload["notification_type"] = "ALARM"
            elif alarm_id is not None and action_type == AlarmActionType.NOTIFY_RECOVERY.value:
                recovery_started_at = _recovery_started_at(transition, created_at)
                dedupe_key = f"NOTIFY_RECOVERY:{alarm_id}:{recovery_started_at.isoformat()}"
                payload["notification_type"] = "RECOVERY"
                payload["recovery_started_at"] = recovery_started_at.isoformat()
            if alarm_id is not None and action_type in {
                "CREATE_ALARM_EVENT",
                "UPDATE_ALARM_EVENT",
                "START_RECOVERY",
                "MARK_ALARM_RECOVERED",
            }:
                effect_key = external_effect_key(
                    action_type,
                    alarm_id,
                    sample_time=sample.sample_time,
                    recovered_at=created_at,
                    sample=_sample_dict(sample),
                    result=monitor_result_dict,
                )
                payload["external_effect_key"] = effect_key
                dedupe_key = effect_key
            enriched.append(
                replace(
                    action,
                    alarm_id=alarm_id,
                    task_id=task_id,
                    dedupe_key=dedupe_key,
                    payload=payload,
                )
            )
        return tuple(enriched)

    @staticmethod
    def _attach_reconciliation_task_metadata(
        actions: tuple[ApplicationAction, ...],
        task_ids: tuple[str, ...],
        *,
        event_id: str | None,
    ) -> tuple[ApplicationAction, ...]:
        """Make the safety task identity visible on its external action."""
        if not task_ids:
            return actions
        enriched: list[ApplicationAction] = []
        task_index = 0
        for action in actions:
            if action.action_type in {
                AlarmActionType.CREATE_ALARM_EVENT,
                AlarmActionType.UPDATE_ALARM_EVENT,
                AlarmActionType.MARK_ALARM_RECOVERED,
            } and task_index < len(task_ids):
                local_event_id = action.alarm_id or event_id
                dedupe_key = (
                    f"RECONCILE_ALARM_EVENT:{local_event_id}"
                    if local_event_id is not None
                    else action.dedupe_key
                )
                action = replace(
                    action,
                    task_id=task_ids[task_index],
                    dedupe_key=dedupe_key,
                    alarm_id=local_event_id,
                )
                task_index += 1
            enriched.append(action)
        return tuple(enriched)

    @staticmethod
    def _attach_notification_task_metadata(
        actions: tuple[ApplicationAction, ...],
        task_ids: tuple[str, ...],
        *,
        event_id: str | None,
    ) -> tuple[ApplicationAction, ...]:
        """Attach each notification's own durable task identity."""
        if not task_ids:
            return actions
        enriched: list[ApplicationAction] = []
        task_index = 0
        for action in actions:
            if (
                action.kind is ApplicationActionKind.NOTIFICATION
                and task_index < len(task_ids)
            ):
                local_event_id = action.alarm_id or event_id
                if local_event_id is not None:
                    action = replace(
                        action,
                        task_id=task_ids[task_index],
                        alarm_id=local_event_id,
                    )
                task_index += 1
            enriched.append(action)
        return tuple(enriched)

    @staticmethod
    def _enrich_reconciliation_actions(
        actions: tuple[ApplicationAction, ...],
        *,
        context: Mapping[str, Any],
        task_id: str,
        local_event_id: str | None,
        now: datetime,
    ) -> tuple[ApplicationAction, ...]:
        """Attach the same task/effect identities to scheduler retries."""
        enriched: list[ApplicationAction] = []
        for action in actions:
            if action.kind is ApplicationActionKind.NOTIFICATION:
                alarm_id = action.alarm_id or local_event_id
                if alarm_id is None:
                    enriched.append(action)
                    continue
                action_type = action.action_type.value
                payload = dict(action.payload)
                if action_type == AlarmActionType.NOTIFY_ALARM.value:
                    dedupe_key = f"NOTIFY_ALARM:{alarm_id}"
                    payload["notification_type"] = "ALARM"
                else:
                    recovery_started_at = _recovery_started_at_from_context(
                        context, now
                    )
                    dedupe_key = (
                        f"NOTIFY_RECOVERY:{alarm_id}:"
                        f"{recovery_started_at.isoformat()}"
                    )
                    payload["notification_type"] = "RECOVERY"
                    payload["recovery_started_at"] = recovery_started_at.isoformat()
                enriched.append(
                    replace(
                        action,
                        alarm_id=alarm_id,
                        task_id=None,
                        dedupe_key=dedupe_key,
                        payload=payload,
                    )
                )
                continue
            if action.action_type not in {
                AlarmActionType.CREATE_ALARM_EVENT,
                AlarmActionType.UPDATE_ALARM_EVENT,
                AlarmActionType.MARK_ALARM_RECOVERED,
            }:
                enriched.append(action)
                continue
            alarm_id = action.alarm_id or local_event_id
            if alarm_id is None:
                enriched.append(action)
                continue
            effect_key = external_effect_key(
                action.action_type.value,
                alarm_id,
                sample_time=context.get("sample_time"),
                recovered_at=context.get("recovered_at") or context.get("created_at") or now,
                sample=context.get("sample") if isinstance(context.get("sample"), Mapping) else {},
                result=(
                    context.get("python_monitor_result")
                    if isinstance(context.get("python_monitor_result"), Mapping)
                    else {}
                ),
            )
            payload = dict(action.payload)
            payload["external_effect_key"] = effect_key
            enriched.append(
                replace(
                    action,
                    alarm_id=alarm_id,
                    task_id=task_id,
                    dedupe_key=f"RECONCILE_ALARM_EVENT:{alarm_id}",
                    payload=payload,
                )
            )
        return tuple(enriched)

    def _prepare_event_reconciliation_tasks(
        self,
        transition: StateTransition,
        actions: tuple[ApplicationAction, ...],
        *,
        sample: MonitorSample,
        monitor_result: MonitorResult,
        operation_state: OperationState,
        standard: Any | None,
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
                AlarmActionType.MARK_ALARM_RECOVERED,
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
            "local_event_id": transition.next.active_alarm_id
            or transition.previous.active_alarm_id,
            "area": operation_state.area_id,
            "sample_time": sample.sample_time.isoformat(),
            "temperature": sample.temperature,
            "humidity": sample.humidity,
            "peak_temperature": sample.temperature,
            "peak_humidity": sample.humidity,
            "online_status": sample.online_status,
            "data_quality": _enum_value(sample.data_quality),
            "temperature_status": _enum_value(monitor_result.temperature_status),
            "humidity_status": _enum_value(monitor_result.humidity_status),
            "standard_id": monitor_result.standard_id,
            "standard_revision": monitor_result.standard_revision,
            "standard_source": monitor_result.standard_source,
            "standard": _standard_dict(standard, monitor_result),
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
                dedupe_key=f"RECONCILE_ALARM_EVENT:{payload['local_event_id']}",
                created_at=created_at,
            )
            task_ids.append(task.task_id)
        return tuple(task_ids)

    def _prepare_notification_tasks(
        self,
        transition: StateTransition,
        actions: tuple[ApplicationAction, ...],
        *,
        sample: MonitorSample,
        monitor_result: MonitorResult,
        operation_state: OperationState,
        standard: Any | None,
        created_at: datetime,
    ) -> tuple[str, ...]:
        """Arm independent notification tasks only under the full Active gate."""
        context = {
            "device_id": sample.device_id,
            "created_at": created_at.isoformat(),
            "sample_time": sample.sample_time.isoformat(),
            "sample": _sample_dict(sample),
            "python_monitor_result": _monitor_result_dict(monitor_result),
            "python_alarm_transition": _transition_dict(transition),
            "operation_state": _operation_state_dict(operation_state),
            "standard": _standard_dict(standard, monitor_result),
        }
        return self._prepare_notification_tasks_from_context(
            tuple(
                action
                for action in actions
                if action.kind is ApplicationActionKind.NOTIFICATION
            ),
            context=context,
            created_at=created_at,
        )

    def _prepare_notification_tasks_from_context(
        self,
        actions: tuple[ApplicationAction, ...],
        *,
        context: Mapping[str, Any],
        created_at: datetime,
    ) -> tuple[str, ...]:
        if self.task_repository is None:
            return ()
        task_ids: list[str] = []
        for action in actions:
            if not self._active_notification_enabled(
                action.action_type,
                str(context.get("device_id") or action.device_id),
            ):
                continue
            event_id = action.alarm_id or context.get("event_id")
            dedupe_key = action.dedupe_key
            if not event_id or not dedupe_key:
                continue
            payload = dict(context)
            payload.update(
                {
                    "device_id": str(context.get("device_id") or action.device_id),
                    "event_id": event_id,
                    "alarm_id": event_id,
                    "action_type": action.action_type.value,
                    "dedupe_key": dedupe_key,
                    "retry_attempt": _retry_attempt(payload),
                }
            )
            if action.action_type is AlarmActionType.NOTIFY_RECOVERY:
                recovery_started_at = (
                    action.payload.get("recovery_started_at")
                    or context.get("recovery_started_at")
                    or _mapping(context.get("python_alarm_transition")).get(
                        "recovery_started_at"
                    )
                )
                if recovery_started_at:
                    payload["recovery_started_at"] = recovery_started_at
                payload["recovered_at"] = (
                    context.get("recovered_at")
                    or context.get("created_at")
                )
            task = self.task_repository.create_or_get(
                task_type=action.action_type.value,
                entity_type="EVENT",
                entity_id=str(event_id),
                due_at=created_at,
                payload=payload,
                dedupe_key=dedupe_key,
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
                AlarmActionType.MARK_ALARM_RECOVERED,
            }:
                continue
            if task_index >= len(task_ids):
                break
            task_id = task_ids[task_index]
            task_index += 1
            if execution.status is not ActionExecutionStatus.SUCCEEDED:
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

    def _finish_successful_notification_tasks(
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
                AlarmActionType.NOTIFY_ALARM,
                AlarmActionType.NOTIFY_RECOVERY,
            }:
                continue
            if task_index >= len(task_ids):
                break
            task_id = task_ids[task_index]
            task_index += 1
            if execution.status is not ActionExecutionStatus.SUCCEEDED:
                continue
            try:
                self.task_repository.cancel(task_id, updated_at=updated_at)
            except (KeyError, ValueError):
                logger.debug(
                    "notification task was already claimed or finished | task=%s",
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
        payload = dict(task.payload)
        payload["retry_attempt"] = attempt
        delay = min(
            _active_event_retry_base_seconds() * (2 ** min(max(attempt - 1, 0), 20)),
            _active_event_retry_max_seconds(),
        )
        self.task_repository.reschedule_running(
            task,
            due_at=now + timedelta(seconds=delay),
            payload=payload,
            updated_at=now,
        )
        logger.info(
            "binding_retry | device_id=%s local_event_id=%s event_key=%s record_id=%s attempt=%s due_at=%s",
            task.entity_id, task.payload.get("local_event_id"),
            task.payload.get("violation_started_at"), None, attempt,
            (now + timedelta(seconds=delay)).isoformat(),
        )

    def _active_event_writes_enabled(self, device_id: str) -> bool:
        mode = getattr(self.action_executor, "mode", None)
        mode_value = getattr(mode, "value", mode)
        if mode_value != AutomationMode.ACTIVE.value:
            return False
        if not getattr(self.action_executor, "standards_ready", lambda: True)():
            return False
        return active_scope_allows(
            device_id,
            active_device_ids=getattr(self.action_executor, "active_device_ids", ()),
        )

    def _active_notification_enabled(
        self,
        action_type: AlarmActionType,
        device_id: str,
    ) -> bool:
        if not self._active_event_writes_enabled(device_id):
            return False
        import config

        if action_type is AlarmActionType.NOTIFY_ALARM:
            return bool(getattr(config, "FEISHU_ALARM_NOTIFY_ENABLED", False))
        if action_type is AlarmActionType.NOTIFY_RECOVERY:
            return bool(getattr(config, "FEISHU_RECOVERY_NOTIFY_ENABLED", False))
        return False

    def _project_local_actions(
        self,
        transition: StateTransition,
        actions: tuple[ApplicationAction, ...],
        *,
        sample: MonitorSample,
        operation_state: OperationState,
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
                        "violation_started_at": (
                            next_state.violation_started_at.isoformat()
                            if next_state.violation_started_at is not None
                            else None
                        ),
                        "area": operation_state.area_id,
                        "temperature": sample.temperature,
                        "humidity": sample.humidity,
                        "peak_temperature": sample.temperature,
                        "peak_humidity": sample.humidity,
                        "online_status": sample.online_status,
                        "data_quality": _enum_value(sample.data_quality),
                        "feishu_binding_status": (
                            "PENDING"
                            if self._active_event_writes_enabled(sample.device_id)
                            else None
                        ),
                        "feishu_create_attempted": False,
                    },
                )
                if self._active_event_writes_enabled(sample.device_id):
                    self.event_repository.mark_external_binding_pending(
                        event.event_id,
                        requested_at=created_at,
                    )
                next_state = replace(next_state, active_alarm_id=event.event_id)

            if (
                action.action_type.value == "UPDATE_ALARM_EVENT"
                and self.event_repository is not None
            ):
                event_id = action.alarm_id or transition.previous.active_alarm_id
                event = (
                    self.event_repository.get(event_id)
                    if event_id is not None
                    else None
                )
                if event is not None:
                    self.event_repository.patch_external_projection(
                        event_id,
                        temperature=sample.temperature,
                        humidity=sample.humidity,
                        peak_temperature=_max_number(
                            event.payload.get("peak_temperature"), sample.temperature
                        ),
                        peak_humidity=_max_number(
                            event.payload.get("peak_humidity"), sample.humidity
                        ),
                    )

            if action.action_type.value == "MARK_ALARM_RECOVERED":
                event_id = action.alarm_id or transition.previous.active_alarm_id
                if event_id is not None and self.event_repository is not None:
                    if self._active_event_writes_enabled(sample.device_id):
                        self.event_repository.patch_external_projection(
                            event_id,
                            feishu_recovery_pending=True,
                            feishu_recovered_at=created_at.isoformat(),
                        )
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


def _standard_dict(
    standard: Any | None,
    result: MonitorResult | None = None,
) -> dict[str, Any]:
    """Serialize the resolved standard without coupling notification to ORM rows."""
    if standard is None:
        return {
            "standard_id": result.standard_id if result is not None else None,
            "revision": result.standard_revision if result is not None else None,
            "standard_source": result.standard_source if result is not None else None,
        }
    return {
        "standard_id": getattr(standard, "standard_id", None),
        "revision": getattr(standard, "revision", None),
        "area": getattr(standard, "area", None),
        "device_id": getattr(standard, "device_id", None),
        "operation_type": getattr(standard, "operation_type", None),
        "temperature_min": getattr(standard, "temperature_min", None),
        "temperature_max": getattr(standard, "temperature_max", None),
        "humidity_min": getattr(standard, "humidity_min", None),
        "humidity_max": getattr(standard, "humidity_max", None),
        "effective_from": _iso_value(getattr(standard, "effective_from", None)),
        "effective_to": _iso_value(getattr(standard, "effective_to", None)),
        "source_document": getattr(standard, "source_document", None),
        "clause": getattr(standard, "clause", None),
        "standard_source": getattr(standard, "standard_source", None),
    }


def _iso_value(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _max_number(previous: Any, current: Any) -> float | None:
    values = [value for value in (previous, current) if isinstance(value, (int, float))]
    return max(values) if values else None


def _recovery_started_at(transition: StateTransition, fallback: datetime) -> datetime:
    return (
        transition.next.recovery_started_at
        or transition.previous.recovery_started_at
        or fallback
    )


def _recovery_started_at_from_context(
    context: Mapping[str, Any], fallback: datetime
) -> datetime:
    transition = _mapping(context.get("python_alarm_transition"))
    value = (
        context.get("recovery_started_at")
        or transition.get("recovery_started_at")
        or context.get("recovered_at")
        or context.get("created_at")
    )
    parsed = _parse_payload_datetime(value)
    return parsed or fallback


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _notification_retry_base_seconds() -> float:
    import config

    return max(1.0, float(getattr(config, "FEISHU_NOTIFY_BACKOFF_SECONDS", 30.0)))


def _notification_retry_max_seconds() -> float:
    import config

    return max(
        _notification_retry_base_seconds(),
        float(getattr(config, "FEISHU_NOTIFY_MAX_BACKOFF_SECONDS", 600.0)),
    )


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
        "standard_source": result.standard_source,
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
            (
                transition.next.recovery_started_at
                or transition.previous.recovery_started_at
            ).isoformat()
            if (transition.next.recovery_started_at or transition.previous.recovery_started_at)
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
    return epoch_milliseconds(left) == epoch_milliseconds(right)


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
