"""Shared field-value helpers for the Feishu adapters.

Feishu Bitable cells arrive as nested list/Mapping values (rich text,
person cells, select options).  Adapters previously each carried a private
copy of the unwrapping and datetime-parsing logic; keep exactly one here.

Semantic contracts:
- ``field_text`` flattens one cell to plain text; a missing cell yields ``""``.
- ``parse_feishu_datetime`` accepts datetime passthrough, epoch seconds or
  milliseconds, and ISO text with a trailing ``Z``; invalid text raises
  ``ValueError`` so adapter callers can wrap their own error types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo


def field_text(value: Any) -> str:
    """Flatten a Feishu cell value (list/Mapping rich text) to plain text."""
    if isinstance(value, Mapping):
        if "value" in value:
            return field_text(value["value"])
        if "text" in value:
            return field_text(value["text"])
        if "name" in value:
            return field_text(value["name"])
        return ""
    if isinstance(value, list):
        return " ".join(item for item in (field_text(part) for part in value) if item)
    return str(value).strip() if value is not None else ""


def field_text_at(fields: Any, field_name: str | None = None) -> str:
    """``field_text`` for one named cell of a record's field mapping."""
    if field_name is None:
        return field_text(fields)
    return field_text(fields.get(field_name))


def parse_feishu_datetime(
    value: Any,
    *,
    assume_timezone: str | None = None,
) -> datetime | None:
    """Parse a Feishu datetime cell.

    Accepts a ``datetime`` as-is, epoch seconds/milliseconds (heuristic:
    values above 10_000_000_000 are treated as milliseconds), or ISO-8601
    text.  Naive timestamps get ``assume_timezone`` applied when provided.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = field_text(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Feishu field is not datetime: {value!r}") from exc

    if assume_timezone and (parsed.tzinfo is None or parsed.utcoffset() is None):
        parsed = parsed.replace(tzinfo=ZoneInfo(assume_timezone))
    return parsed
