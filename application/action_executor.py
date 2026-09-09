"""Single entry point for domain action execution modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol

from domain.models import AlarmAction, AlarmActionType

from .actions import ApplicationAction
from .active_scope import active_scope_allows, normalize_device_id, normalize_device_ids


class AutomationMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ACTIVE = "active"


class ActionExecutionStatus(str, Enum):
    SKIPPED = "SKIPPED"
    PLANNED = "PLANNED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ActionExecution:
    """Audit-friendly outcome of one declarative domain action."""

    action: AlarmAction | ApplicationAction
    mode: AutomationMode
    status: ActionExecutionStatus
    error: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


ActionHandler = Callable[[AlarmAction | ApplicationAction], None]
ContextActionHandler = Callable[
    [AlarmAction | ApplicationAction, Mapping[str, Any]], None
]


class ActionRunRecorder(Protocol):
    """Persist an action plan or execution result for audit and comparison."""

    def record(self, execution: ActionExecution) -> None:
        """Record one action execution outcome."""


class ActionExecutor:
    """Execute or simulate domain actions through one centralized boundary."""

    def __init__(
        self,
        *,
        mode: AutomationMode | str,
        handlers: Mapping[AlarmActionType | str, ActionHandler] | None = None,
        context_handlers: Mapping[
            AlarmActionType | str, ContextActionHandler
        ] | None = None,
        active_device_ids: Iterable[str] | str | None = None,
        recorder: ActionRunRecorder | None = None,
        standards_ready_provider: Callable[[], bool] | None = None,
        action_enabled_provider: Callable[[AlarmAction | ApplicationAction], bool] | None = None,
        active_epoch_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.mode = AutomationMode(mode)
        self.active_device_ids = normalize_device_ids(active_device_ids)
        self.handlers = {
            AlarmActionType(action_type): handler
            for action_type, handler in (handlers or {}).items()
        }
        self.context_handlers = {
            AlarmActionType(action_type): handler
            for action_type, handler in (context_handlers or {}).items()
        }
        self.recorder = recorder
        self.standards_ready_provider = standards_ready_provider
        self.action_enabled_provider = action_enabled_provider
        self.active_epoch_provider = active_epoch_provider

    def standards_ready(self) -> bool:
        """Return the current production-standard gate, failing closed."""
        if self.standards_ready_provider is None:
            # Active execution must never be enabled by omission.  Tests and
            # alternate runtimes must explicitly provide a ready predicate.
            return False
        try:
            return bool(self.standards_ready_provider())
        except Exception:  # noqa: BLE001 - a broken health gate must deny writes
            return False

    def execute(
        self,
        actions: tuple[AlarmAction | ApplicationAction, ...]
        | list[AlarmAction | ApplicationAction],
        *,
        context: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> tuple[ActionExecution, ...]:
        """Process actions according to the configured mode.

        ``shadow`` records what would have happened but never invokes a
        handler.  ``active`` invokes the injected handler only for an
        allowlisted ``context["device_id"]`` and captures failures as
        auditable results.  External adapters are deliberately supplied by
        the application bootstrap rather than imported here.
        """
        action_context = dict(context or {})
        executions: list[ActionExecution] = []
        for action in actions:
            per_action_context = dict(action_context)
            task_id = getattr(action, "task_id", None)
            if task_id is not None:
                per_action_context["automation_task_id"] = task_id
            event_id = getattr(action, "alarm_id", None)
            if event_id is not None:
                per_action_context["event_id"] = event_id
            dedupe_key = getattr(action, "dedupe_key", None)
            if dedupe_key is not None:
                per_action_context["dedupe_key"] = dedupe_key
            payload = getattr(action, "payload", None)
            if isinstance(payload, Mapping) and payload.get("external_effect_key"):
                per_action_context["external_effect_key"] = payload[
                    "external_effect_key"
                ]
            execution = self._execute_one(
                action,
                context=per_action_context,
                created_at=created_at,
            )
            executions.append(execution)
            if self.recorder is not None:
                self.recorder.record(execution)
        return tuple(executions)

    def _execute_one(
        self,
        action: AlarmAction | ApplicationAction,
        *,
        context: Mapping[str, Any],
        created_at: datetime | None,
    ) -> ActionExecution:
        if self.mode is AutomationMode.DISABLED:
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.SKIPPED,
                context=context,
                created_at=created_at,
            )
        if self.mode is AutomationMode.SHADOW:
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.PLANNED,
                context=context,
                created_at=created_at,
            )

        # This is the final production-write gate.  A missing/failed standard
        # snapshot is observable as a planned action, never as a normal alarm
        # write and never as a handler exception.
        if not self.standards_ready():
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.PLANNED,
                error="standards_ready=false; production action remains PLANNED",
                context=context,
                created_at=created_at,
            )

        # The Active canary gate deliberately runs before handler lookup.  A
        # device outside the allowlist must remain PLANNED even when its
        # action has no handler, and no external adapter may be reached.
        device_id = self._context_device_id(context)
        if device_id is None:
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.FAILED,
                error=(
                    "active action requires a non-empty context.device_id; "
                    "refusing external write"
                ),
                context=context,
                created_at=created_at,
            )
        if not self.active_device_ids:
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.PLANNED,
                error="ACTIVE_DEVICE_IDS is empty; action remains PLANNED",
                context=context,
                created_at=created_at,
            )
        if not active_scope_allows(
            device_id,
            active_device_ids=self.active_device_ids,
        ):
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.PLANNED,
                error=(
                    f"device_id {device_id} is not in ACTIVE_DEVICE_IDS; "
                    "action remains PLANNED"
                ),
                context=context,
                created_at=created_at,
            )

        action_device_id = self._action_device_id(action)
        if action_device_id != device_id:
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.FAILED,
                error=(
                    "active action device scope mismatch: "
                    f"context.device_id={device_id}, action.device_id={action_device_id}; "
                    "refusing external write"
                ),
                context=context,
                created_at=created_at,
            )

        # A scheduler task carries the epoch in its durable audit metadata.
        # When configured, require that metadata to belong to the process's
        # current Active epoch before any external handler is reached.  Direct
        # sample actions remain compatible for alternate/test callers that do
        # not inject an epoch provider; production bootstrap always injects it.
        if self.active_epoch_provider is not None:
            try:
                current_epoch = self.active_epoch_provider()
            except Exception:  # noqa: BLE001 - a broken cutover gate denies writes
                current_epoch = None
            task_mode = context.get("created_mode")
            task_epoch = context.get("active_epoch")
            has_task_context = (
                context.get("automation_task_id") is not None
                or "created_mode" in context
                or "active_epoch" in context
            )
            if has_task_context and (
                task_mode != AutomationMode.ACTIVE.value
                or not current_epoch
                or task_epoch != current_epoch
            ):
                return ActionExecution(
                    action=action,
                    mode=self.mode,
                    status=ActionExecutionStatus.PLANNED,
                    error=(
                        "external effect is outside the current Active epoch; "
                        "action remains PLANNED"
                    ),
                    context=context,
                    created_at=created_at,
                )

        if self.action_enabled_provider is not None:
            try:
                enabled = bool(self.action_enabled_provider(action))
            except Exception:  # noqa: BLE001 - a broken feature gate fails closed
                enabled = False
            if not enabled:
                return ActionExecution(
                    action=action,
                    mode=self.mode,
                    status=ActionExecutionStatus.SKIPPED,
                    error=(
                        f"action {action.action_type.value} is disabled by its "
                        "runtime feature gate"
                    ),
                    context=context,
                    created_at=created_at,
                )

        context_handler = self.context_handlers.get(action.action_type)
        handler = self.handlers.get(action.action_type)
        if context_handler is None and handler is None:
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.FAILED,
                error=f"no handler for {action.action_type.value}",
                context=context,
                created_at=created_at,
            )
        try:
            if context_handler is not None:
                context_handler(action, context)
            else:
                handler(action)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - audit the adapter failure
            return ActionExecution(
                action=action,
                mode=self.mode,
                status=ActionExecutionStatus.FAILED,
                error=str(exc),
                context=context,
                created_at=created_at,
            )
        return ActionExecution(
            action=action,
            mode=self.mode,
            status=ActionExecutionStatus.SUCCEEDED,
            context=context,
            created_at=created_at,
        )

    @staticmethod
    def _context_device_id(context: Mapping[str, Any]) -> str | None:
        """Read and normalize the device scope supplied to ``execute``.

        Active authorization is intentionally based on the execution scope,
        not on a field carried by the action object.  This keeps the gate at
        the action boundary and prevents a stale/mismatched action field from
        widening the write scope.
        """
        raw_device_id = context.get("device_id")
        if not isinstance(raw_device_id, str):
            return None
        return normalize_device_id(raw_device_id)

    @staticmethod
    def _action_device_id(action: AlarmAction | ApplicationAction) -> str | None:
        raw_device_id = getattr(action, "device_id", None)
        if not isinstance(raw_device_id, str):
            return None
        return normalize_device_id(raw_device_id)
