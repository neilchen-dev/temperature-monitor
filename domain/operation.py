"""Domain-neutral operation observation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OperationAction(str, Enum):
    START = "StartOperation"
    SWITCH = "SwitchOperation"
    END = "EndOperation"


@dataclass(frozen=True)
class OperationObservation:
    device_id: str
    area_id: str
    action: OperationAction
    operation_type: str | None
    work_order: str | None
    source_record_id: str
    source_created_at: datetime
    observed_at: datetime
    initiator_id: str | None = None
    initiator_id_type: str | None = None
    initiator_name: str | None = None


@dataclass(frozen=True)
class ActiveOperation:
    """Persisted details needed to decide and deliver an overdue reminder."""

    device_id: str
    area_id: str
    operation_type: str | None
    work_order: str | None
    source_record_id: str
    started_at: datetime
    initiator_id: str | None
    initiator_id_type: str | None
    initiator_name: str | None
    overdue_notification_status: str | None = None
    overdue_notification_at: datetime | None = None
    overdue_notification_sequence: int | None = None


def is_newer_operation(
    incoming: OperationObservation,
    current: OperationObservation | None,
) -> bool:
    """Return whether an observation may replace current state."""

    return current is None or incoming.source_created_at > current.source_created_at
