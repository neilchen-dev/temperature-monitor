"""Read-only normalization of Feishu controlled-operation registrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

import config

from domain.operation import (
    OperationAction,
    OperationObservation,
)

from .feishu_records import FeishuRawRecord


logger = logging.getLogger("temperature_monitor")


class FeishuOperationSource(Protocol):
    def read_records(self, table_id: str) -> Iterable[FeishuRawRecord]:
        """Return records from the explicitly configured operation table."""

    def read_field_names(self, table_id: str) -> Iterable[str]:
        """Return table field names for a lightweight schema check."""


class OperationTableSchemaError(RuntimeError):
    """The configured table cannot be an operation-registration table."""


@dataclass(frozen=True)
class OperationFetchStats:
    """Observability for one operation-table fetch and normalization pass."""

    records_fetched: int = 0
    observations: int = 0
    accepted: int = 0
    rejected: int = 0
    transport_status: str = "not_run"
    schema_status: str = "not_checked"
    outcome: str = "not_run"
    last_observation_at: datetime | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_fetched": self.records_fetched,
            "observations": self.observations,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "transport_status": self.transport_status,
            "schema_status": self.schema_status,
            "outcome": self.outcome,
            "last_observation_at": (
                self.last_observation_at.isoformat()
                if self.last_observation_at is not None
                else None
            ),
            "error": self.error,
        }


@dataclass(frozen=True)
class FeishuOperationFieldMap:
    """Explicit mapping for the confirmed operation-registration entry."""

    device_id: str
    area_id: str
    action: str
    operation_type: str | None = None
    work_order: str | None = None
    source_created_at: str | None = None
    validation: str | None = None
    valid_values: tuple[str, ...] = ("有效",)
    allowed_device_ids: frozenset[str] = frozenset()


class FeishuOperationAdapter:
    """Convert operation-registration records without writing Feishu."""

    def __init__(
        self,
        *,
        source: FeishuOperationSource,
        table_id: str,
        fields: FeishuOperationFieldMap,
    ) -> None:
        if not table_id.strip():
            raise ValueError("table_id must be explicitly configured")
        self.source = source
        self.table_id = table_id
        self.fields = fields
        self._schema_validated = False
        self._last_fetch_stats = OperationFetchStats()

    @property
    def last_fetch_stats(self) -> OperationFetchStats:
        return self._last_fetch_stats

    @property
    def required_schema_fields(self) -> tuple[str, ...]:
        fields = self.fields
        required = [fields.device_id, fields.area_id, fields.action]
        if fields.source_created_at:
            required.append(fields.source_created_at)
        if fields.validation:
            required.append(fields.validation)
        return tuple(dict.fromkeys(field for field in required if field))

    def validate_schema(self) -> None:
        """Fail clearly when the configured table is not the source table.

        Test/fake sources may not expose metadata and remain compatible.  The
        real Feishu source does, so an empty but correctly shaped table remains
        a valid empty sync while an interval/event table fails explicitly.
        """
        if self._schema_validated:
            return
        read_field_names = getattr(self.source, "read_field_names", None)
        if not callable(read_field_names):
            self._last_fetch_stats = OperationFetchStats(
                schema_status="not_supported"
            )
            self._schema_validated = True
            return
        try:
            available = {
                str(field).strip()
                for field in read_field_names(self.table_id)
                if str(field).strip()
            }
        except Exception as exc:  # noqa: BLE001 - caller records transport error
            self._last_fetch_stats = OperationFetchStats(
                transport_status="error",
                schema_status="error",
                outcome="schema_error",
                error=str(exc),
            )
            raise
        missing = [field for field in self.required_schema_fields if field not in available]
        if missing:
            message = (
                "Feishu operation table schema mismatch: "
                f"table={self.table_id}; missing={','.join(missing)}"
            )
            self._last_fetch_stats = OperationFetchStats(
                schema_status="invalid",
                outcome="schema_error",
                error=message,
            )
            raise OperationTableSchemaError(message)
        self._last_fetch_stats = OperationFetchStats(schema_status="valid")
        self._schema_validated = True

    def fetch_observations(
        self,
        *,
        observed_at: datetime | None = None,
    ) -> tuple[OperationObservation, ...]:
        self.validate_schema()
        try:
            records = tuple(self.source.read_records(self.table_id))
        except Exception as exc:  # noqa: BLE001 - preserve transport visibility
            self._last_fetch_stats = OperationFetchStats(
                transport_status="error",
                schema_status=self._last_fetch_stats.schema_status,
                outcome="transport_error",
                error=str(exc),
            )
            raise

        observations: list[OperationObservation] = []
        rejected = 0
        for record in records:
            try:
                if not self._is_workflow_eligible(record):
                    rejected += 1
                    continue
                observations.append(self.normalize_record(record, observed_at=observed_at))
            except (KeyError, TypeError, ValueError) as exc:
                rejected += 1
                logger.warning(
                    "Feishu operation record rejected | table=%s | record_id=%s | error=%s",
                    self.table_id,
                    record.record_id,
                    str(exc),
                )
        last_observation_at = max(
            (observation.source_created_at for observation in observations),
            default=None,
        )
        if not records:
            outcome = "transport_success_empty"
        elif not observations:
            outcome = "transport_success_records_no_observations"
            logger.warning(
                "Feishu operation sync returned records but no observations | "
                "table=%s | records=%s | rejected=%s",
                self.table_id,
                len(records),
                rejected,
            )
        else:
            outcome = "transport_success_with_observations"
        self._last_fetch_stats = OperationFetchStats(
            records_fetched=len(records),
            observations=len(observations),
            rejected=rejected,
            transport_status="success",
            schema_status=self._last_fetch_stats.schema_status,
            outcome=outcome,
            last_observation_at=last_observation_at,
        )
        return tuple(observations)

    def _is_workflow_eligible(self, record: FeishuRawRecord) -> bool:
        """Apply the same gate as the four Feishu operation workflows.

        Invalid registrations remain in Feishu for the reminder workflow, but
        must not change the Python operation state.
        """
        fields = self.fields
        if fields.allowed_device_ids:
            raw_device = _optional_text(record.fields, fields.device_id)
            if raw_device is None or raw_device.upper() not in fields.allowed_device_ids:
                return False
        if fields.validation is None:
            return True
        validation = _optional_text(record.fields, fields.validation)
        return validation in {value.strip() for value in fields.valid_values}

    def normalize_record(
        self,
        record: FeishuRawRecord,
        *,
        observed_at: datetime | None = None,
    ) -> OperationObservation:
        fields = self.fields
        device_id = _required_text(record.fields, fields.device_id).upper()
        area_id = _required_text(record.fields, fields.area_id)
        action = _action(_required_text(record.fields, fields.action))
        operation_type = _optional_text(record.fields, fields.operation_type)
        work_order = _optional_text(record.fields, fields.work_order)
        source_created_at = _parse_datetime(record.created_at)
        if source_created_at is None and fields.source_created_at is not None:
            source_created_at = _parse_datetime(record.fields.get(fields.source_created_at))
        if source_created_at is None:
            raise ValueError("operation record must provide its creation time")
        current_time = observed_at or _parse_datetime(record.updated_at) or source_created_at
        return OperationObservation(
            device_id=device_id,
            area_id=area_id,
            action=action,
            operation_type=operation_type,
            work_order=work_order,
            source_record_id=record.record_id,
            source_created_at=source_created_at,
            observed_at=current_time,
        )


def _raw_value(fields: Any, field_name: str | None) -> Any:
    if field_name is None:
        return None
    value = fields.get(field_name)
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if isinstance(value, dict):
        for key in ("value", "text", "name"):
            if key in value:
                return _raw_value({"value": value[key]}, "value")
    return value


def _required_text(fields: Any, field_name: str) -> str:
    value = _optional_text(fields, field_name)
    if value is None:
        raise ValueError(f"required Feishu field is empty: {field_name}")
    return value


def _optional_text(fields: Any, field_name: str | None) -> str | None:
    value = _raw_value(fields, field_name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _action(value: str) -> OperationAction:
    aliases = {
        "开始作业": OperationAction.START,
        "工艺切换": OperationAction.SWITCH,
        "结束作业": OperationAction.END,
        OperationAction.START.value: OperationAction.START,
        OperationAction.SWITCH.value: OperationAction.SWITCH,
        OperationAction.END.value: OperationAction.END,
    }
    try:
        return aliases[value]
    except KeyError as exc:
        raise ValueError(f"unsupported operation action: {value}") from exc


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Feishu operation field is not datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(config.HISTORY_TIMEZONE))
    return parsed
