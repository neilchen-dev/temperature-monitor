"""Read-only normalization of Feishu controlled-operation registrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

from domain.operation import (
    OperationAction,
    OperationObservation,
)

from .feishu_records import FeishuRawRecord


class FeishuOperationSource(Protocol):
    def read_records(self, table_id: str) -> Iterable[FeishuRawRecord]:
        """Return records from the explicitly configured operation table."""


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

    def fetch_observations(
        self,
        *,
        observed_at: datetime | None = None,
    ) -> tuple[OperationObservation, ...]:
        observations: list[OperationObservation] = []
        for record in self.source.read_records(self.table_id):
            if not self._is_workflow_eligible(record):
                continue
            observations.append(self.normalize_record(record, observed_at=observed_at))
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
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        from datetime import timezone

        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
