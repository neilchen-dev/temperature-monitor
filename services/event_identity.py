"""Canonical identities shared by alarm projection and external effects."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import config


def epoch_milliseconds(value: datetime) -> int:
    if not isinstance(value, datetime):
        raise TypeError("event time must be datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(config.HISTORY_TIMEZONE))
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def canonical_instant(value: datetime | str | None) -> str | None:
    """Return one timezone-independent representation for an instant."""
    parsed = _parse_datetime(value)
    return str(epoch_milliseconds(parsed)) if parsed is not None else None


def external_effect_key(
    action_type: str,
    event_id: str,
    *,
    sample_time: datetime | str | None = None,
    recovered_at: datetime | str | None = None,
    sample: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
) -> str:
    """Build a deterministic identity for one external alarm effect.

    CREATE is identified by the local event identity. UPDATE additionally
    includes the exact snapshot so separate real samples remain distinct while
    replaying one sample is a no-op. Recovery is identified by the local event
    and the logical recovery instant; no wall-clock or random value is used.
    """
    normalized_action = str(action_type).strip().upper()
    normalized_event = str(event_id).strip()
    if not normalized_event:
        raise ValueError("event_id cannot be empty")
    if normalized_action == "CREATE_ALARM_EVENT":
        return f"ENV_CREATE:{normalized_event}"
    if normalized_action == "MARK_ALARM_RECOVERED":
        instant = canonical_instant(recovered_at or sample_time)
        if instant is None:
            raise ValueError("recovery effect requires a logical recovery instant")
        return f"ENV_RECOVERY:{normalized_event}:{instant}"
    if normalized_action == "UPDATE_ALARM_EVENT":
        material = {
            "event_id": normalized_event,
            "sample_time": canonical_instant(sample_time),
            "sample": dict(sample or {}),
            "result": dict(result or {}),
        }
        digest = hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return f"ENV_UPDATE:{normalized_event}:{digest}"
    return f"ENV_EFFECT:{normalized_action}:{normalized_event}"
