"""Read-only normalization of a future Feishu environment-standard table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

import config
from domain.models import EnvironmentStandard, parse_control_type

from .feishu_records import FeishuRawRecord


class FeishuStandardSource(Protocol):
    """Read raw Base records; this protocol performs no writes."""

    def read_records(self, table_id: str) -> Iterable[FeishuRawRecord]:
        """Return records from the explicitly configured standard table."""


@dataclass(frozen=True)
class FeishuStandardFieldMap:
    """Explicit source-field mapping; no Feishu field names are guessed."""

    standard_id: str
    revision: str
    area: str
    operation_type: str
    temperature_min: str
    temperature_max: str
    humidity_min: str
    humidity_max: str
    effective_from: str
    effective_to: str
    priority: str
    enabled: str
    source_document: str
    clause: str
    device_id: str | None = None
    control_type: str | None = None


@dataclass(frozen=True)
class StandardSourceRecord:
    """A normalized standard plus source metadata for sync/audit."""

    source_record_id: str
    standard: EnvironmentStandard
    source_created_at: datetime | None
    source_updated_at: datetime | None


class FeishuStandardAdapter:
    """Normalize records from an explicitly configured Feishu standard table.

    The adapter only reads and converts values.  Validation, conflict
    decisions, and SQLite activation remain in ``StandardSyncService`` and
    repositories.
    """

    def __init__(
        self,
        *,
        source: FeishuStandardSource,
        table_id: str,
        fields: FeishuStandardFieldMap,
    ) -> None:
        if not table_id.strip():
            raise ValueError("table_id must be explicitly configured")
        self.source = source
        self.table_id = table_id
        self.fields = fields

    def fetch_source_records(self) -> tuple[StandardSourceRecord, ...]:
        return tuple(self.normalize_record(record) for record in self.source.read_records(self.table_id))

    def fetch_standards(self) -> tuple[EnvironmentStandard, ...]:
        """Implement the application ``StandardSource`` protocol."""
        return tuple(record.standard for record in self.fetch_source_records())

    def normalize_record(self, record: FeishuRawRecord) -> StandardSourceRecord:
        values = record.fields
        standard = EnvironmentStandard(
            standard_id=_required_text(values, self.fields.standard_id),
            revision=_required_text(values, self.fields.revision),
            area=_required_text(values, self.fields.area),
            device_id=(
                _optional_text(values, self.fields.device_id)
                if self.fields.device_id is not None
                else None
            ),
            operation_type=_optional_text(values, self.fields.operation_type),
            control_type=(
                _standard_control_type(
                    values,
                    self.fields.control_type,
                    source_record_id=record.record_id,
                )
                if self.fields.control_type is not None
                else None
            ),
            temperature_min=_optional_number(values, self.fields.temperature_min),
            temperature_max=_optional_number(values, self.fields.temperature_max),
            humidity_min=_optional_number(values, self.fields.humidity_min),
            humidity_max=_optional_number(values, self.fields.humidity_max),
            effective_from=_required_datetime(values, self.fields.effective_from),
            effective_to=_optional_datetime(values, self.fields.effective_to),
            priority=_required_int(values, self.fields.priority),
            enabled=_required_bool(values, self.fields.enabled),
            source_document=_required_text(values, self.fields.source_document),
            clause=_optional_text(values, self.fields.clause),
        )
        return StandardSourceRecord(
            source_record_id=record.record_id,
            standard=standard,
            source_created_at=_parse_datetime(record.created_at),
            source_updated_at=_parse_datetime(record.updated_at),
        )


def _raw_value(values: dict[str, Any] | Any, field_name: str) -> Any:
    value = values.get(field_name)
    if isinstance(value, list):
        if not value:
            return None
        if len(value) == 1:
            return _raw_value({"value": value[0]}, "value")
        return [_raw_value({"value": item}, "value") for item in value]
    if isinstance(value, dict):
        for key in ("value", "text", "name"):
            if key in value:
                return _raw_value({"value": value[key]}, "value")
    return value


def _required_text(values: dict[str, Any] | Any, field_name: str) -> str:
    value = _optional_text(values, field_name)
    if value is None:
        raise ValueError(f"required Feishu field is empty: {field_name}")
    return value


def _optional_text(values: dict[str, Any] | Any, field_name: str) -> str | None:
    value = _raw_value(values, field_name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _standard_control_type(
    values: dict[str, Any] | Any,
    field_name: str,
    *,
    source_record_id: str,
):
    try:
        return parse_control_type(_optional_text(values, field_name))
    except ValueError as exc:
        raise ValueError(
            f"invalid Feishu standard record {source_record_id}, "
            f"field {field_name}: {exc}"
        ) from exc


def _optional_number(values: dict[str, Any] | Any, field_name: str) -> float | None:
    value = _raw_value(values, field_name)
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Feishu field is not numeric: {field_name}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Feishu field is not finite: {field_name}")
    return number


def _required_int(values: dict[str, Any] | Any, field_name: str) -> int:
    value = _raw_value(values, field_name)
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"required integer Feishu field is empty: {field_name}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Feishu field is not an integer: {field_name}") from exc
    if isinstance(value, float) and value != number:
        raise ValueError(f"Feishu field is not an integer: {field_name}")
    return number


def _required_bool(values: dict[str, Any] | Any, field_name: str) -> bool:
    value = _raw_value(values, field_name)
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "是", "启用", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "否", "停用", "disabled"}:
        return False
    raise ValueError(f"Feishu field is not boolean: {field_name}")


def _required_datetime(values: dict[str, Any] | Any, field_name: str) -> datetime:
    value = _optional_datetime(values, field_name)
    if value is None:
        raise ValueError(f"required datetime Feishu field is empty: {field_name}")
    return value


def _optional_datetime(values: dict[str, Any] | Any, field_name: str) -> datetime | None:
    return _parse_datetime(_raw_value(values, field_name))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Feishu field is not datetime: {value!r}") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(config.HISTORY_TIMEZONE))
    return parsed
