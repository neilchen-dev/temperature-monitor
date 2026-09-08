"""Canonical precision shared by alarm projection and recovery lookups."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config


def epoch_milliseconds(value: datetime) -> int:
    if not isinstance(value, datetime):
        raise TypeError("event time must be datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(config.HISTORY_TIMEZONE))
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
