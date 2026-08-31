"""Application commands mapped from domain alarm actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Any, Mapping

from domain.models import AlarmAction, AlarmActionType


class ApplicationActionKind(str, Enum):
    SCHEDULE_TASK = "SCHEDULE_TASK"
    CANCEL_TASK = "CANCEL_TASK"
    COMPLETE_TASK = "COMPLETE_TASK"
    EVENT = "EVENT"
    INTEGRATION = "INTEGRATION"


@dataclass(frozen=True)
class ApplicationAction:
    """A domain action enriched with application-owned routing metadata."""

    action_type: AlarmActionType
    kind: ApplicationActionKind
    device_id: str
    source: AlarmAction
    run_at: datetime | None = None
    alarm_id: str | None = None
    task_type: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    task_id: str | None = None
    dedupe_key: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


class ApplicationActionMapper:
    """Translate domain actions without executing them."""

    def map(self, transition: Any) -> tuple[ApplicationAction, ...]:
        mapped: list[ApplicationAction] = []
        violation_started_at = transition.next.violation_started_at
        for action in transition.actions:
            if action.action_type is AlarmActionType.CREATE_VERIFY_TASK:
                if action.run_at is None or violation_started_at is None:
                    raise ValueError("verification action requires a due time and start time")
                dedupe_key = (
                    f"VERIFY_ALARM:{action.device_id}:"
                    f"{violation_started_at.isoformat()}"
                )
                mapped.append(
                    ApplicationAction(
                        action_type=action.action_type,
                        kind=ApplicationActionKind.SCHEDULE_TASK,
                        device_id=action.device_id,
                        source=action,
                        run_at=action.run_at,
                        task_type="VERIFY_ALARM",
                        entity_type="DEVICE",
                        entity_id=action.device_id,
                        dedupe_key=dedupe_key,
                        payload={"reason": transition.reason},
                    )
                )
                continue

            if action.action_type is AlarmActionType.CANCEL_VERIFY_TASK:
                mapped.append(
                    ApplicationAction(
                        action_type=action.action_type,
                        kind=ApplicationActionKind.CANCEL_TASK,
                        device_id=action.device_id,
                        source=action,
                        task_type="VERIFY_ALARM",
                        entity_type="DEVICE",
                        entity_id=action.device_id,
                        task_id=transition.previous.pending_task_id,
                    )
                )
                continue

            if action.action_type is AlarmActionType.COMPLETE_VERIFY_TASK:
                mapped.append(
                    ApplicationAction(
                        action_type=action.action_type,
                        kind=ApplicationActionKind.COMPLETE_TASK,
                        device_id=action.device_id,
                        source=action,
                        task_type="VERIFY_ALARM",
                        entity_type="DEVICE",
                        entity_id=action.device_id,
                        task_id=transition.previous.pending_task_id,
                    )
                )
                continue

            kind = (
                ApplicationActionKind.EVENT
                if action.action_type
                in {
                    AlarmActionType.CREATE_ALARM_EVENT,
                    AlarmActionType.UPDATE_ALARM_EVENT,
                    AlarmActionType.START_RECOVERY,
                    AlarmActionType.CLOSE_ALARM_EVENT,
                }
                else ApplicationActionKind.INTEGRATION
            )
            mapped.append(
                ApplicationAction(
                    action_type=action.action_type,
                    kind=kind,
                    device_id=action.device_id,
                    source=action,
                    run_at=action.run_at,
                    alarm_id=action.alarm_id,
                    payload={"reason": transition.reason},
                )
            )
        return tuple(mapped)
