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


def is_newer_operation(
    incoming: OperationObservation,
    current: OperationObservation | None,
) -> bool:
    """Return whether an observation may replace current state."""

    return current is None or incoming.source_created_at > current.source_created_at
