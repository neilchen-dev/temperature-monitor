"""Normalize Feishu record fields before Shadow comparison."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any, Protocol

from application.shadow import ObservedAutomationState


class FeishuObservationSource(Protocol):
    def read(self, device_id: str) -> Mapping[str, Any]:
        """Read raw fields from Feishu; field names remain inside this adapter."""


@dataclass(frozen=True)
class FeishuObservationFieldMap:
    alarm_state: str
    operation_state: str
    event_exists: str
    overall_status: str | None = None
    standard_id: str | None = None
    standard_revision: str | None = None
    active_event_count: str | None = None
    observed_at: str | None = None
    applicability: str | None = None
    data_quality: str | None = None
    temperature_status: str | None = None
    humidity_status: str | None = None
    active_event_ids: str | None = None
    operation_type: str | None = None


@dataclass(frozen=True)
class FeishuObservationTableFieldMap:
    """Field names needed to compose a device/event read-only snapshot."""

    device_id: str = "设备编号"
    event_device_id: str = "监测点"
    # The current ENV table calls this field ``处理状态``.  ``事件状态`` was
    # used by an earlier draft and would make closed records look active.
    event_status: str = "处理状态"
    closed_statuses: tuple[str, ...] = ("关闭", "已关闭", "CLOSED")


class FeishuBitableObservationSource:
    """Read the device table and ENV event table without any write API."""

    def __init__(
        self,
        *,
        source: Any,
        device_table_id: str,
        event_table_id: str,
        fields: FeishuObservationTableFieldMap | None = None,
    ) -> None:
        if not device_table_id.strip() or not event_table_id.strip():
            raise ValueError("device and event table IDs must be configured")
        self.source = source
        self.device_table_id = device_table_id
        self.event_table_id = event_table_id
        self.fields = fields or FeishuObservationTableFieldMap()

    def read(self, device_id: str) -> Mapping[str, Any]:
        normalized_device = device_id.strip().upper()
        device_records = tuple(self.source.read_records(self.device_table_id))
        matches = tuple(
            record
            for record in device_records
            if _field_text(record.fields, self.fields.device_id).upper()
            == normalized_device
        )
        if len(matches) != 1:
            raise RuntimeError(
                f"Feishu device observation expected one record for {normalized_device}, "
                f"found {len(matches)}"
            )
        device_record = matches[0]
        active_events = tuple(
            record
            for record in self.source.read_records(self.event_table_id)
            if _field_text(record.fields, self.fields.event_device_id).upper()
            == normalized_device
            and not self._is_closed(record.fields.get(self.fields.event_status))
        )
        raw = dict(device_record.fields)
        raw["__event_exists"] = bool(active_events)
        raw["__active_event_count"] = len(active_events)
        raw["__active_event_ids"] = [record.record_id for record in active_events]
        raw["__observed_at"] = _record_time(device_record.updated_at) or _record_time(
            device_record.created_at
        )
        return raw

    def _is_closed(self, value: Any) -> bool:
        normalized_value = _field_text(value).lower()
        return normalized_value in {
            status.strip().lower() for status in self.fields.closed_statuses
        }


class FeishuObservationAdapter:
    """Convert one raw Feishu observation to the canonical comparison shape."""

    def __init__(
        self,
        *,
        source: FeishuObservationSource,
        fields: FeishuObservationFieldMap,
    ) -> None:
        self.source = source
        self.fields = fields

    def observe(self, device_id: str) -> ObservedAutomationState:
        raw = self.source.read(device_id)
        return ObservedAutomationState(
            device_id=device_id,
            alarm_state=_alarm_state(raw.get(self.fields.alarm_state)),
            operation_state=_operation_state(raw.get(self.fields.operation_state)),
            operation_type=_optional_text(raw, self.fields.operation_type),
            event_exists=_bool(raw.get(self.fields.event_exists)),
            overall_status=_overall_status(raw.get(self.fields.overall_status)),
            standard_id=_optional_text(raw, self.fields.standard_id),
            standard_revision=_optional_text(raw, self.fields.standard_revision),
            active_event_count=_optional_int(
                raw, self.fields.active_event_count
            ),
            observed_at=_optional_datetime(raw, self.fields.observed_at),
            applicability=_applicability(raw, self.fields.applicability),
            data_quality=_data_quality(raw, self.fields.data_quality),
            temperature_status=_temperature_status(raw, self.fields.temperature_status),
            humidity_status=_temperature_status(raw, self.fields.humidity_status),
            active_event_ids=_optional_ids(raw, self.fields.active_event_ids),
        )


def _alarm_state(value: Any) -> str:
    aliases = {
        "未触发": "NORMAL",
        "计时中": "PENDING",
        "已发警报": "ALARM",
    }
    text = _text(value)
    return aliases.get(text, text)


def _operation_state(value: Any) -> str:
    aliases = {"作业中": "OPERATING", "无作业": "IDLE", "N/A": "NOT_APPLICABLE"}
    text = _text(value)
    return aliases.get(text, text)


def _overall_status(value: Any) -> str | None:
    aliases = {
        "正常": "NORMAL",
        "超限": "VIOLATION",
        # OverallStatus intentionally has no NO_STANDARD member.  The
        # separate applicability field carries that information.
        "待工艺标准": "UNKNOWN",
        "仅监测": "UNKNOWN",
        "设备离线": "UNKNOWN",
        "数据异常": "UNKNOWN",
        "数据缺失": "UNKNOWN",
    }
    text = _text(value)
    return aliases.get(text, text) or None


def _temperature_status(raw: Mapping[str, Any], field_name: str | None) -> str | None:
    if field_name is None:
        return None
    aliases = {
        "正常": "NORMAL",
        "低于下限": "LOW",
        "高于上限": "HIGH",
        "超限": "UNKNOWN",
        "数据缺失": "UNKNOWN",
        "数据异常": "UNKNOWN",
        "离线": "UNKNOWN",
    }
    text = _text(raw.get(field_name))
    return aliases.get(text, text) or None


def _applicability(raw: Mapping[str, Any], field_name: str | None) -> str | None:
    if field_name is not None:
        text = _text(raw.get(field_name))
        aliases = {"适用": "APPLICABLE", "仅监测": "NOT_APPLICABLE"}
        return aliases.get(text, text) or None
    overall = _text(raw.get("当前判定状态"))
    if overall == "仅监测":
        return "NOT_APPLICABLE"
    if overall == "待工艺标准":
        return "NO_STANDARD"
    return None


def _data_quality(raw: Mapping[str, Any], field_name: str | None) -> str | None:
    if field_name is None:
        return None
    text = _text(raw.get(field_name))
    aliases = {
        "在线": "GOOD",
        "online": "GOOD",
        "离线": "OFFLINE",
        "offline": "OFFLINE",
        "数据缺失": "MISSING",
        "数据异常": "ERROR",
    }
    return aliases.get(text, text.upper() if text else None)


def _text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_text(item) for item in value).strip()
    if isinstance(value, dict):
        if "value" in value:
            return _text(value["value"])
        return str(value.get("text", value.get("name", ""))).strip()
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {
        "1", "true", "yes", "y", "是", "有", "存在", "active", "alarm"
    }


def _optional_text(raw: Mapping[str, Any], field_name: str | None) -> str | None:
    if field_name is None:
        return None
    value = _text(raw.get(field_name))
    return value or None


def _optional_int(raw: Mapping[str, Any], field_name: str | None) -> int | None:
    if field_name is None:
        return None
    value = raw.get(field_name)
    if value is None or value == "":
        return None
    try:
        return int(_text(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Feishu observation field is not an integer: {field_name}") from exc


def _optional_ids(raw: Mapping[str, Any], field_name: str | None) -> tuple[str, ...]:
    if field_name is None:
        return ()
    value = raw.get(field_name)
    if isinstance(value, (list, tuple)):
        return tuple(item for item in (_text(item) for item in value) if item)
    text = _text(value)
    return (text,) if text else ()


def _optional_datetime(raw: Mapping[str, Any], field_name: str | None) -> datetime | None:
    if field_name is None:
        return None
    value = raw.get(field_name)
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return datetime.fromisoformat(_text(value).replace("Z", "+00:00"))


def _field_text(fields: Mapping[str, Any] | Any, field_name: str | None = None) -> str:
    if field_name is None:
        value = fields
    else:
        value = fields.get(field_name)
    return _text(value)


def _record_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        from datetime import timezone

        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
