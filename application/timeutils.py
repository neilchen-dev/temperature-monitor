"""Shared datetime helpers for the application and runtime layers.

These were previously duplicated across ``application/monitor_service.py``
and ``runtime/shadow_runner.py``; keep exactly one implementation here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def iso_value(value: Any) -> str | None:
    """ISO-format a datetime; pass through None and already-serialized text."""
    return value.isoformat() if isinstance(value, datetime) else value


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO datetime from an arbitrary payload value; None on failure."""
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def align_datetime(value: datetime, reference: datetime) -> datetime:
    """Align legacy naive values to the reference timezone for comparison."""
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value
