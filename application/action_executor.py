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
            execution = self._execute_one(
                action,
                context=action_context,
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
