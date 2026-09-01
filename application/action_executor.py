"""Single entry point for domain action execution modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from domain.models import AlarmAction, AlarmActionType

from .actions import ApplicationAction


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
        recorder: ActionRunRecorder | None = None,
    ) -> None:
        self.mode = AutomationMode(mode)
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
        handler.  ``active`` invokes the injected handler and captures a
        failure as an auditable result.  External adapters are deliberately
        supplied by the application bootstrap rather than imported here.
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
