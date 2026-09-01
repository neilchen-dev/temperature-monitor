"""Explicit write adapters for the three operational Feishu Base tables.

The domain layer remains unaware of Feishu field names.  These adapters are
the only place where the current ledger schema is translated into writable
CellValues.  They are intentionally usable with an injected writer so unit
tests do not need network access.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import uuid
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import config
from domain.models import MonitorResult, MonitorSample
from domain.operation import OperationAction, OperationObservation

from .feishu_records import FeishuRawRecord


_business_tz_cache: ZoneInfo | None = None


def _business_timezone() -> ZoneInfo:
    global _business_tz_cache
    if _business_tz_cache is None:
        _business_tz_cache = ZoneInfo(config.HISTORY_TIMEZONE)
    return _business_tz_cache


def _event_time_matches(value: Any, expected: datetime) -> bool:
    """Compare a Feishu datetime cell against an expected business instant.

    Feishu returns datetime cells as millisecond timestamps (aware UTC) while
    callers usually hold business-local datetimes; the comparison normalizes
    both sides to instants before matching. Unparseable cells never match.
    """
    if value is None or value == "":
        return False
    try:
        parsed = _parse_datetime(value)
    except (FeishuWriteError, ValueError, TypeError):
        return False
    if parsed is None:
        return False
    reference = expected
    if parsed.tzinfo is None and reference.tzinfo is not None:
        parsed = parsed.replace(tzinfo=_business_timezone())
    elif reference.tzinfo is None and parsed.tzinfo is not None:
        reference = reference.replace(tzinfo=_business_timezone())
    if parsed.tzinfo is None or reference.tzinfo is None:
        return parsed == reference
    return parsed.astimezone(timezone.utc) == reference.astimezone(timezone.utc)


class FeishuWriteError(RuntimeError):
    """A write was rejected because the ledger state is unsafe or ambiguous."""


class FeishuRecordWriter(Protocol):
    def create(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        client_token: str | None = None,
    ) -> Mapping[str, Any]:
        """Create one record in the explicitly named table."""

    def update(
        self,
        table_id: str,
        record_id: str,
        fields: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Update one record in the explicitly named table."""


class FeishuBitableRecordWriter:
    """Production writer backed by the existing Feishu HTTP service."""

    def create(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        client_token: str | None = None,
    ) -> Mapping[str, Any]:
        from services.feishu import create_bitable_record

        return create_bitable_record(
            table_id,
            dict(fields),
            client_token=client_token,
        )

    def update(
        self,
        table_id: str,
        record_id: str,
        fields: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from services.feishu import update_bitable_record

        return update_bitable_record(table_id, record_id, dict(fields))


@dataclass(frozen=True)
class FeishuOperationWriteFieldMap:
    """Writable fields in the operation registration and interval tables."""

    device_id: str = "监测点"
    area: str = "区域"
    action: str = "状态变更"
    operation_type: str = "当前工艺"
    work_order: str | None = None
    state_recorded_at: str = "状态记录时间"
    snapshot_temperature: str = "当时温度（°C）"
    snapshot_humidity: str = "当时湿度（%RH）"
    snapshot_online: str = "当时在线状态"
    snapshot_temperature_status: str = "当时温度判定"
    snapshot_humidity_status: str = "当时湿度判定"
    snapshot_reason: str = "当时异常说明"
    interval_device_id: str = "监测点"
    interval_area: str = "区域"
    interval_operation_type: str = "工艺"
    interval_status: str = "区间状态"
    interval_start: str = "开始时间"
    interval_end: str = "结束时间"
    interval_start_temperature: str = "开始温度（°C）"
    interval_start_humidity: str = "开始湿度（%RH）"
    interval_start_online: str = "开始在线状态"
    interval_start_temperature_status: str = "开始温度判定"
    interval_start_humidity_status: str = "开始湿度判定"
    interval_end_temperature: str = "结束温度（°C）"
    interval_end_humidity: str = "结束湿度（%RH）"
    interval_end_online: str = "结束在线状态"
    interval_end_temperature_status: str = "结束温度判定"
    interval_end_humidity_status: str = "结束湿度判定"
    device_operation_state: str = "当前作业状态"
    device_operation_type: str = "当前工艺"
    device_operation_started_at: str = "作业开始时间"


class FeishuOperationRecordWriter:
    """Write operation registrations, snapshots, intervals and main context."""

    def __init__(
        self,
        *,
        writer: FeishuRecordWriter,
        operation_table_id: str,
        interval_table_id: str,
        device_table_id: str,
        fields: FeishuOperationWriteFieldMap | None = None,
        source: FeishuEventSource | None = None,
    ) -> None:
        self.writer = writer
        self.operation_table_id = _required_id(operation_table_id, "operation_table_id")
        self.interval_table_id = _required_id(interval_table_id, "interval_table_id")
        self.device_table_id = _required_id(device_table_id, "device_table_id")
        self.fields = fields or FeishuOperationWriteFieldMap()
        # 只读 source 用于应用层幂等：同一 (设备, 动作, 状态记录时间) 的
        # 重复登记在写之前先查表复用 record_id，不依赖本地 SQLite 去重。
        self.source = source

    def create_registration(
        self,
        *,
        device_id: str,
        area: str,
        action: OperationAction | str,
        operation_type: str | None = None,
        work_order: str | None = None,
        snapshot: MonitorSample | MonitorResult | None = None,
        status_recorded_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        action_text = _action_text(action)
        if status_recorded_at is not None:
            existing = self._find_registration_by_business_key(
                device_id=device_id,
                action_text=action_text,
                recorded_at=status_recorded_at,
            )
            if existing is not None:
                # 同一逻辑登记已存在（上次调用超时但远端已写入等场景）：
                # 复用既有 record_id，不再创建第二条记录。
                return {
                    "existing": True,
                    "idempotent": True,
                    "record_id": existing.record_id,
                }
        fields = self._registration_fields(
            device_id=device_id,
            area=area,
            action=action,
            operation_type=operation_type,
            work_order=work_order,
        )
        if snapshot is not None:
            fields.update(self.snapshot_fields(snapshot))
        if status_recorded_at is not None:
            fields[self.fields.state_recorded_at] = _datetime_cell(status_recorded_at)
        token = idempotency_key or (
            f"PROC:{_device_id(device_id)}:{action_text}:"
            f"{_datetime_cell(status_recorded_at)}"
            if status_recorded_at is not None
            else f"PROC:{uuid.uuid4()}"
        )
        return self.writer.create(
            self.operation_table_id,
            fields,
            client_token=token,
        )

    def _find_registration_by_business_key(
        self,
        *,
        device_id: str,
        action_text: str,
        recorded_at: datetime,
    ) -> FeishuRawRecord | None:
        """Business key: (监测点, 状态变更, 状态记录时间)."""
        if self.source is None:
            return None
        normalized_device = _device_id(device_id)
        for record in self.source.read_records(self.operation_table_id):
            if (
                _field_text(record.fields.get(self.fields.device_id)).upper()
                != normalized_device
            ):
                continue
            if _field_text(record.fields.get(self.fields.action)) != action_text:
                continue
            if _event_time_matches(
                record.fields.get(self.fields.state_recorded_at), recorded_at
            ):
                return record
        return None

    def update_registration_snapshot(
        self,
        *,
        registration_record_id: str,
        snapshot: MonitorSample | MonitorResult,
        status_recorded_at: datetime | None = None,
    ) -> Mapping[str, Any]:
        fields = self.snapshot_fields(snapshot)
        if status_recorded_at is not None:
            fields[self.fields.state_recorded_at] = _datetime_cell(status_recorded_at)
        return self.writer.update(
            self.operation_table_id,
            _required_id(registration_record_id, "registration_record_id"),
            fields,
        )

    def create_interval(
        self,
        *,
        observation: OperationObservation,
        snapshot: MonitorSample | MonitorResult | None = None,
        interval_status: str = "作业中",
        record_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        fields = self._interval_base_fields(observation, interval_status)
        if record_type:
            raise ValueError("记录类型是飞书自动编号字段，不能由接口写入")
        if snapshot is not None:
            fields.update(self._interval_start_fields(snapshot))
        token = idempotency_key or f"RUN:{observation.source_record_id}"
        return self.writer.create(
            self.interval_table_id,
            fields,
            client_token=token,
        )

    def close_interval(
        self,
        *,
        interval_record_id: str,
        ended_at: datetime,
        snapshot: MonitorSample | MonitorResult | None = None,
        interval_status: str = "已结束",
    ) -> Mapping[str, Any]:
        fields = {
            self.fields.interval_end: _datetime_cell(ended_at),
            self.fields.interval_status: _interval_status_text(interval_status),
        }
        if snapshot is not None:
            fields.update(self._interval_end_fields(snapshot))
        return self.writer.update(
            self.interval_table_id,
            _required_id(interval_record_id, "interval_record_id"),
            fields,
        )

    def update_device_context(
        self,
        *,
        device_record_id: str,
        state: str,
        operation_type: str | None,
        started_at: datetime | None,
    ) -> Mapping[str, Any]:
        normalized_state = _operation_state_text(state)
        fields: dict[str, Any] = {
            self.fields.device_operation_state: normalized_state,
            self.fields.device_operation_type: _operation_type_text(operation_type or "N/A"),
            self.fields.device_operation_started_at: (
                _datetime_cell(started_at) if started_at is not None else None
            ),
        }
        return self.writer.update(
            self.device_table_id,
            _required_id(device_record_id, "device_record_id"),
            fields,
        )

    def snapshot_fields(
        self,
        snapshot: MonitorSample | MonitorResult,
    ) -> dict[str, Any]:
        fields = self._sample_fields(snapshot)
        if isinstance(snapshot, MonitorResult):
            fields[self.fields.snapshot_temperature_status] = _temperature_status_text(
                snapshot.temperature_status.value
            )
            fields[self.fields.snapshot_humidity_status] = _temperature_status_text(
                snapshot.humidity_status.value
            )
            if snapshot.reasons:
                fields[self.fields.snapshot_reason] = "; ".join(snapshot.reasons)
        return fields

    def _registration_fields(
        self,
        *,
        device_id: str,
        area: str,
        action: OperationAction | str,
        operation_type: str | None,
        work_order: str | None,
    ) -> dict[str, Any]:
        if not str(area).strip():
            raise ValueError("area 不能为空")
        normalized_device = _operation_device_id(device_id)
        normalized_area = _operation_area_for_device(normalized_device, area)
        fields: dict[str, Any] = {
            self.fields.device_id: normalized_device,
            self.fields.area: normalized_area,
            self.fields.action: _action_text(action),
        }
        if operation_type:
            fields[self.fields.operation_type] = _operation_type_text(operation_type)
        if work_order and self.fields.work_order:
            fields[self.fields.work_order] = work_order.strip()
        return fields

    def _interval_base_fields(
        self,
        observation: OperationObservation,
        interval_status: str,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            self.fields.interval_device_id: _operation_device_id(observation.device_id),
            self.fields.interval_area: _operation_area_for_device(
                _operation_device_id(observation.device_id), observation.area_id
            ),
            self.fields.interval_status: _interval_status_text(interval_status),
            self.fields.interval_start: _datetime_cell(observation.source_created_at),
        }
        if observation.operation_type:
            fields[self.fields.interval_operation_type] = _interval_operation_type_text(
                observation.operation_type
            )
        return fields

    def _sample_fields(
        self,
        snapshot: MonitorSample | MonitorResult,
    ) -> dict[str, Any]:
        online_text = _snapshot_online_text(snapshot)
        fields: dict[str, Any] = {}
        _put_text(fields, self.fields.snapshot_online, online_text)
        _put_number(fields, self.fields.snapshot_temperature, snapshot.temperature)
        _put_number(fields, self.fields.snapshot_humidity, snapshot.humidity)
        return fields

    def _interval_start_fields(
        self,
        snapshot: MonitorSample | MonitorResult,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        _put_number(fields, self.fields.interval_start_temperature, snapshot.temperature)
        _put_number(fields, self.fields.interval_start_humidity, snapshot.humidity)
        _put_text(
            fields,
            self.fields.interval_start_online,
            _snapshot_online_text(snapshot),
        )
        if isinstance(snapshot, MonitorResult):
            fields[self.fields.interval_start_temperature_status] = _temperature_status_text(
                snapshot.temperature_status.value
            )
            fields[self.fields.interval_start_humidity_status] = _temperature_status_text(
                snapshot.humidity_status.value
            )
        return fields

    def _interval_end_fields(
        self,
        snapshot: MonitorSample | MonitorResult,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        _put_number(fields, self.fields.interval_end_temperature, snapshot.temperature)
        _put_number(fields, self.fields.interval_end_humidity, snapshot.humidity)
        _put_text(
            fields,
            self.fields.interval_end_online,
            _snapshot_online_text(snapshot),
        )
        if isinstance(snapshot, MonitorResult):
            fields[self.fields.interval_end_temperature_status] = _temperature_status_text(
                snapshot.temperature_status.value
            )
            fields[self.fields.interval_end_humidity_status] = _temperature_status_text(
                snapshot.humidity_status.value
            )
        return fields


@dataclass(frozen=True)
class FeishuEventWriteFieldMap:
    """Writable fields in ``环境异常事件表``.

    Formula fields such as ``自动异常类型`` and ``处置时效状态`` are omitted
    deliberately.  They must be calculated by Feishu rather than overwritten.
    """

    device_id: str = "监测点"
    area: str = "区域"
    start_time: str = "开始时间"
    recovery_time: str = "恢复时间"
    close_time: str = "关闭时间"
    peak_temperature: str = "峰值温度(°C)"
    peak_humidity: str = "峰值湿度(%RH)"
    trigger_temperature_status: str = "触发温度判定"
    trigger_humidity_status: str = "触发湿度判定"
    anomaly_type: str = "异常类型"
    status: str = "处理状态"
    owner: str = "责任人"
    control_requirement: str = "控制要求"
    cause: str = "异常原因"
    measure: str = "处理措施"
    product_impact: str = "产品影响"


class FeishuEventSource(Protocol):
    def read_records(self, table_id: str) -> tuple[FeishuRawRecord, ...]:
        """Read records used for active-event and owner resolution."""


class FeishuEnvironmentEventWriter:
    """Create/update ENV events while preserving the manual-close boundary."""

    def __init__(
        self,
        *,
        writer: FeishuRecordWriter,
        source: FeishuEventSource,
        event_table_id: str,
        device_table_id: str,
        fields: FeishuEventWriteFieldMap | None = None,
        device_id_field: str = "设备编号",
        device_owner_field: str = "默认异常责任人",
        device_control_requirement_field: str = "要求来源",
        closed_statuses: tuple[str, ...] = ("关闭", "已关闭", "CLOSED"),
    ) -> None:
        self.writer = writer
        self.source = source
        self.event_table_id = _required_id(event_table_id, "event_table_id")
        self.device_table_id = _required_id(device_table_id, "device_table_id")
        self.fields = fields or FeishuEventWriteFieldMap()
        self.device_id_field = device_id_field
        self.device_owner_field = device_owner_field
        self.device_control_requirement_field = device_control_requirement_field
        self.closed_statuses = tuple(item.strip().lower() for item in closed_statuses)

    def create_event(
        self,
        *,
        device_id: str,
        area: str,
        start_time: datetime,
        temperature: float | None = None,
        humidity: float | None = None,
        temperature_status: str | None = None,
        humidity_status: str | None = None,
        owner: Any | None = None,
        control_requirement: str | None = None,
        idempotency_key: str | None = None,
        allow_existing: bool = False,
    ) -> Mapping[str, Any]:
        normalized_device = _device_id(device_id)
        # 业务幂等键 = (监测点, 开始时间)。飞书 API 超时但实际已写入时，
        # 重试会在这里找到既有记录并复用 record_id——包括事件已被人工
        # 提前关闭的情况（active 检查对已关闭记录不可见，会误判可创建）。
        existing_by_key = self._find_event_by_business_key(normalized_device, start_time)
        if existing_by_key is not None:
            return {
                "existing": True,
                "idempotent": True,
                "record_id": existing_by_key.record_id,
            }
        active = self._active_events(normalized_device)
        if len(active) > 1:
            raise FeishuWriteError(
                f"设备 {normalized_device} 存在 {len(active)} 条未关闭环境异常，拒绝继续写入"
            )
        if active:
            if allow_existing:
                return {"existing": True, "record_id": active[0].record_id}
            raise FeishuWriteError(
                f"设备 {normalized_device} 已有未关闭环境异常 {active[0].record_id}"
            )

        owner_cell = _user_cell(owner) if owner is not None else self._device_owner(
            normalized_device
        )
        if not owner_cell:
            raise FeishuWriteError(
                f"设备 {normalized_device} 缺少默认异常责任人，拒绝创建环境异常"
            )
        requirement = control_requirement or self._device_control_requirement(
            normalized_device
        )
        if not str(area).strip():
            raise ValueError("area 不能为空")
        fields: dict[str, Any] = {
            self.fields.device_id: normalized_device,
            self.fields.area: _area_text(area),
            self.fields.start_time: _datetime_cell(start_time),
            self.fields.status: "待处理",
            self.fields.owner: owner_cell,
        }
        if requirement:
            fields[self.fields.control_requirement] = requirement
        _put_number(fields, self.fields.peak_temperature, temperature)
        _put_number(fields, self.fields.peak_humidity, humidity)
        _put_status(fields, self.fields.trigger_temperature_status, temperature_status)
        _put_status(fields, self.fields.trigger_humidity_status, humidity_status)
        anomaly_type = _event_type_text(
            temperature_status=temperature_status,
            humidity_status=humidity_status,
        )
        if anomaly_type:
            fields[self.fields.anomaly_type] = anomaly_type
        token = idempotency_key or (
            f"ENV:{normalized_device}:{_datetime_cell(start_time)}"
        )
        return self.writer.create(
            self.event_table_id,
            fields,
            client_token=token,
        )

    def update_event(
        self,
        *,
        record_id: str,
        temperature: float | None = None,
        humidity: float | None = None,
        temperature_status: str | None = None,
        humidity_status: str | None = None,
    ) -> Mapping[str, Any]:
        fields: dict[str, Any] = {}
        _put_number(fields, self.fields.peak_temperature, temperature)
        _put_number(fields, self.fields.peak_humidity, humidity)
        _put_status(fields, self.fields.trigger_temperature_status, temperature_status)
        _put_status(fields, self.fields.trigger_humidity_status, humidity_status)
        anomaly_type = _event_type_text(
            temperature_status=temperature_status,
            humidity_status=humidity_status,
        )
        if anomaly_type:
            fields[self.fields.anomaly_type] = anomaly_type
        if not fields:
            return {"skipped": True, "reason": "no event fields to update"}
        return self.writer.update(
            self.event_table_id,
            _required_id(record_id, "record_id"),
            fields,
        )

    def update_active_event(
        self,
        *,
        device_id: str,
        temperature: float | None = None,
        humidity: float | None = None,
        temperature_status: str | None = None,
        humidity_status: str | None = None,
    ) -> Mapping[str, Any]:
        record = self._single_active_event(device_id)
        return self.update_event(
            record_id=record.record_id,
            temperature=temperature,
            humidity=humidity,
            temperature_status=temperature_status,
            humidity_status=humidity_status,
        )

    def recover_event(
        self,
        *,
        record_id: str,
        recovered_at: datetime,
    ) -> Mapping[str, Any]:
        return self.writer.update(
            self.event_table_id,
            _required_id(record_id, "record_id"),
            {self.fields.recovery_time: _datetime_cell(recovered_at)},
        )

    def recover_active_event(
        self,
        *,
        device_id: str,
        recovered_at: datetime,
    ) -> Mapping[str, Any]:
        record = self._single_active_event(device_id)
        return self.recover_event(record_id=record.record_id, recovered_at=recovered_at)

    def close_event(
        self,
        *,
        record_id: str,
        closed_at: datetime,
        cause: str,
        measure: str,
        product_impact: str,
        recovered_at: datetime | None = None,
    ) -> Mapping[str, Any]:
        required = {
            "cause": cause,
            "measure": measure,
            "product_impact": product_impact,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError("关闭环境异常前必须填写: " + ", ".join(missing))
        fields: dict[str, Any] = {
            self.fields.cause: cause.strip(),
            self.fields.measure: measure.strip(),
            self.fields.product_impact: product_impact.strip(),
            self.fields.close_time: _datetime_cell(closed_at),
            self.fields.status: "关闭",
        }
        if recovered_at is not None:
            fields[self.fields.recovery_time] = _datetime_cell(recovered_at)
        return self.writer.update(
            self.event_table_id,
            _required_id(record_id, "record_id"),
            fields,
        )

    def handle_alarm_action(
        self,
        action: Any,
        context: Mapping[str, Any],
    ) -> None:
        """Handle automatic alarm actions from Active mode.

        Recovery is written as ``恢复时间`` only.  ``处理状态=关闭`` remains a
        separately authorized manual-close operation, matching the current
        Feishu workflow and its completeness check.
        """
        action_type = str(getattr(action.action_type, "value", action.action_type))
        device_id = _device_id(getattr(action, "device_id", ""))
        sample = context.get("sample", {})
        result = context.get("python_monitor_result", {})
        transition = context.get("python_alarm_transition", {})
        if not isinstance(sample, Mapping):
            sample = {}
        if not isinstance(result, Mapping):
            result = {}
        if not isinstance(transition, Mapping):
            transition = {}

        if action_type == "CREATE_ALARM_EVENT":
            started_at = _parse_datetime(
                transition.get("violation_started_at")
                or context.get("sample_time")
            )
            if started_at is None:
                raise FeishuWriteError("创建环境异常缺少连续超限开始时间")
            self.create_event(
                device_id=device_id,
                area=str(context.get("operation_state", {}).get("area_id", "")).strip()
                if isinstance(context.get("operation_state"), Mapping)
                else "",
                start_time=started_at,
                temperature=_number_value(sample.get("temperature")),
                humidity=_number_value(sample.get("humidity")),
                temperature_status=str(result.get("temperature_status") or ""),
                humidity_status=str(result.get("humidity_status") or ""),
                idempotency_key=f"ENV:{device_id}:{_datetime_cell(started_at)}",
                allow_existing=True,
            )
            return
        if action_type == "UPDATE_ALARM_EVENT":
            self.update_active_event(
                device_id=device_id,
                temperature=_number_value(sample.get("temperature")),
                humidity=_number_value(sample.get("humidity")),
                temperature_status=str(result.get("temperature_status") or ""),
                humidity_status=str(result.get("humidity_status") or ""),
            )
            return
        if action_type == "START_RECOVERY":
            recovery_at = _parse_datetime(context.get("created_at") or context.get("sample_time"))
            if recovery_at is None:
                raise FeishuWriteError("恢复环境异常缺少恢复时间")
            self.recover_active_event(device_id=device_id, recovered_at=recovery_at)
            return
        if action_type == "CLOSE_ALARM_EVENT":
            # The Python state machine closes its local projection after the
            # one-minute recovery check; Feishu still requires human closure.
            return

    def _active_events(self, device_id: str) -> tuple[FeishuRawRecord, ...]:
        normalized_device = _device_id(device_id)
        return tuple(
            record
            for record in self.source.read_records(self.event_table_id)
            if _field_text(record.fields.get(self.fields.device_id)).upper()
            == normalized_device
            and _field_text(record.fields.get(self.fields.status)).lower()
            not in self.closed_statuses
        )

    def _find_event_by_business_key(
        self,
        device_id: str,
        start_time: datetime,
    ) -> FeishuRawRecord | None:
        """Business key: (监测点, 开始时间); status-agnostic by design."""
        for record in self.source.read_records(self.event_table_id):
            if (
                _field_text(record.fields.get(self.fields.device_id)).upper()
                != device_id
            ):
                continue
            if _event_time_matches(record.fields.get(self.fields.start_time), start_time):
                return record
        return None

    def _single_active_event(self, device_id: str) -> FeishuRawRecord:
        records = self._active_events(device_id)
        if len(records) != 1:
            raise FeishuWriteError(
                f"设备 {_device_id(device_id)} 需要恰好一条未关闭环境异常，实际 {len(records)} 条"
            )
        return records[0]

    def _device_owner(self, device_id: str) -> Any | None:
        for record in self.source.read_records(self.device_table_id):
            if _field_text(record.fields.get(self.device_id_field)).upper() == device_id:
                return record.fields.get(self.device_owner_field)
        return None

    def _device_control_requirement(self, device_id: str) -> str | None:
        for record in self.source.read_records(self.device_table_id):
            if _field_text(record.fields.get(self.device_id_field)).upper() == device_id:
                return _field_text(record.fields.get(self.device_control_requirement_field)) or None
        return None


@dataclass(frozen=True)
class FeishuInspectionWriteFieldMap:
    """Writable fields in ``仓库环境点检记录``."""

    area: str = "仓库区域"
    temperature: str = "当时温度（°C）"
    humidity: str = "当时湿度（%RH）"
    online_status: str = "当时在线状态"
    environment_status: str = "当时环境判定"
    temperature_status: str = "当时温度判定"
    humidity_status: str = "当时湿度判定"
    alarm_status: str = "当时报警状态"
    monitoring_system_status: str = "监测系统状态"
    site_storage_status: str = "现场仓储状态"
    abnormal_alarm_number: str = "异常/报警编号"
    abnormal_handling: str = "现场异常及处置说明"
    system_abnormal_description: str = "当时系统异常说明"
    parent_record: str = "父记录"
    state_recorded_at: str = "状态记录时间"


class FeishuInspectionRecordWriter:
    """Create/update inspection rows without writing formula or system fields."""

    def __init__(
        self,
        *,
        writer: FeishuRecordWriter,
        inspection_table_id: str,
        device_table_id: str,
        fields: FeishuInspectionWriteFieldMap | None = None,
        device_recent_inspection_field: str = "最近仓库点检时间",
        source: FeishuEventSource | None = None,
    ) -> None:
        self.writer = writer
        self.inspection_table_id = _required_id(inspection_table_id, "inspection_table_id")
        self.device_table_id = _required_id(device_table_id, "device_table_id")
        self.fields = fields or FeishuInspectionWriteFieldMap()
        self.device_recent_inspection_field = device_recent_inspection_field
        # 只读 source 用于应用层幂等（区域 + 状态记录时间）。
        self.source = source

    def create_snapshot(
        self,
        *,
        area: str,
        inspected_at: datetime,
        inspector: Any | None = None,
        temperature: float | None = None,
        humidity: float | None = None,
        online_status: str | None = None,
        environment_status: str | None = None,
        temperature_status: str | None = None,
        humidity_status: str | None = None,
        alarm_status: str | None = None,
        monitoring_system_status: str | None = None,
        site_storage_status: str | None = None,
        abnormal_alarm_number: int | float | None = None,
        abnormal_handling: str | None = None,
        system_abnormal_description: str | None = None,
        parent_record_id: str | None = None,
        state_recorded_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        existing = self._find_snapshot_by_business_key(area, inspected_at)
        if existing is not None:
            # 同一 (仓库区域, 状态记录时间) 的点检已存在：复用，不重复创建。
            return {
                "existing": True,
                "idempotent": True,
                "record_id": existing.record_id,
            }
        fields = self.snapshot_fields(
            area=area,
            inspected_at=inspected_at,
            inspector=inspector,
            temperature=temperature,
            humidity=humidity,
            online_status=online_status,
            environment_status=environment_status,
            temperature_status=temperature_status,
            humidity_status=humidity_status,
            alarm_status=alarm_status,
            monitoring_system_status=monitoring_system_status,
            site_storage_status=site_storage_status,
            abnormal_alarm_number=abnormal_alarm_number,
            abnormal_handling=abnormal_handling,
            system_abnormal_description=system_abnormal_description,
            parent_record_id=parent_record_id,
            state_recorded_at=state_recorded_at,
        )
        token = idempotency_key or f"WH:{area.strip()}:{_datetime_cell(inspected_at)}"
        return self.writer.create(
            self.inspection_table_id,
            fields,
            client_token=token,
        )

    def _find_snapshot_by_business_key(
        self,
        area: str,
        inspected_at: datetime,
    ) -> FeishuRawRecord | None:
        """Business key: (仓库区域, 状态记录时间)."""
        if self.source is None:
            return None
        normalized_area = area.strip()
        for record in self.source.read_records(self.inspection_table_id):
            if _field_text(record.fields.get(self.fields.area)) != normalized_area:
                continue
            if _event_time_matches(
                record.fields.get(self.fields.state_recorded_at), inspected_at
            ):
                return record
        return None

    def update_snapshot(
        self,
        *,
        inspection_record_id: str,
        **snapshot: Any,
    ) -> Mapping[str, Any]:
        fields = self.snapshot_fields(**snapshot)
        return self.writer.update(
            self.inspection_table_id,
            _required_id(inspection_record_id, "inspection_record_id"),
            fields,
        )

    def update_device_recent_inspection(
        self,
        *,
        device_record_id: str,
        inspected_at: datetime,
    ) -> Mapping[str, Any]:
        return self.writer.update(
            self.device_table_id,
            _required_id(device_record_id, "device_record_id"),
            {self.device_recent_inspection_field: _datetime_cell(inspected_at)},
        )

    def snapshot_fields(
        self,
        *,
        area: str,
        inspected_at: datetime,
        inspector: Any | None = None,
        temperature: float | None = None,
        humidity: float | None = None,
        online_status: str | None = None,
        environment_status: str | None = None,
        temperature_status: str | None = None,
        humidity_status: str | None = None,
        alarm_status: str | None = None,
        monitoring_system_status: str | None = None,
        site_storage_status: str | None = None,
        abnormal_alarm_number: int | float | None = None,
        abnormal_handling: str | None = None,
        system_abnormal_description: str | None = None,
        parent_record_id: str | None = None,
        state_recorded_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not area.strip():
            raise ValueError("area 不能为空")
        if inspector is not None:
            raise ValueError("点检时间和点检人是飞书系统字段，不能由接口手工写入")
        fields: dict[str, Any] = {
            self.fields.area: area.strip(),
            # The ledger's ``点检时间`` is created_at.  Keep the observation
            # time in the ordinary writable status-time field instead.
            self.fields.state_recorded_at: _datetime_cell(inspected_at),
        }
        _put_number(fields, self.fields.temperature, temperature)
        _put_number(fields, self.fields.humidity, humidity)
        _put_text(fields, self.fields.online_status, _online_status_text(online_status))
        _put_text(fields, self.fields.environment_status, _environment_status_text(environment_status))
        _put_status(fields, self.fields.temperature_status, temperature_status)
        _put_status(fields, self.fields.humidity_status, humidity_status)
        _put_alarm_status(fields, self.fields.alarm_status, alarm_status)
        _put_monitoring_system_status(fields, self.fields.monitoring_system_status, monitoring_system_status)
        _put_site_storage_status(fields, self.fields.site_storage_status, site_storage_status)
        if abnormal_alarm_number is not None:
            if isinstance(abnormal_alarm_number, bool) or not isinstance(
                abnormal_alarm_number, (int, float)
            ) or not math.isfinite(float(abnormal_alarm_number)):
                raise ValueError(
                    "异常/报警编号字段是数字；不能写入 ENV-... 文本编号"
                )
            if not float(abnormal_alarm_number).is_integer():
                raise ValueError("异常/报警编号字段精度为 0，必须是整数")
            fields[self.fields.abnormal_alarm_number] = abnormal_alarm_number
        _put_text(fields, self.fields.abnormal_handling, abnormal_handling)
        _put_text(
            fields,
            self.fields.system_abnormal_description,
            system_abnormal_description,
        )
        if parent_record_id:
            fields[self.fields.parent_record] = _link_cell(parent_record_id)
        if state_recorded_at is not None:
            fields[self.fields.state_recorded_at] = _datetime_cell(state_recorded_at)
        return fields


def _required_id(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _device_id(value: Any) -> str:
    normalized = _required_id(str(value), "device_id").upper()
    return normalized


def _action_text(value: OperationAction | str) -> str:
    if isinstance(value, OperationAction):
        value = value.value
    aliases = {
        "StartOperation": "开始作业",
        "SwitchOperation": "工艺切换",
        "EndOperation": "结束作业",
        "开始作业": "开始作业",
        "工艺切换": "工艺切换",
        "结束作业": "结束作业",
    }
    text = str(value).strip()
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError(f"不支持的作业动作: {value!r}") from exc


def _operation_state_text(value: str) -> str:
    aliases = {
        "OPERATING": "作业中",
        "IDLE": "无作业",
        "NOT_APPLICABLE": "N/A",
        "作业中": "作业中",
        "无作业": "无作业",
        "N/A": "N/A",
    }
    text = str(value).strip()
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError(f"不支持的作业状态: {value!r}") from exc


_OPERATION_AREAS = frozenset({"精密装配间", "电力电子实验室", "通用总装线", "特种工艺间"})
_OPERATION_DEVICE_AREAS = {
    "TH-03": "精密装配间",
    "TH-04": "电力电子实验室",
    "TH-05": "通用总装线",
    "TH-07": "特种工艺间",
}


def _area_text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("area 不能为空")
    return text


def _operation_area_text(value: Any) -> str:
    text = str(value).strip()
    if text not in _OPERATION_AREAS:
        raise ValueError(f"不支持的受控作业区域: {value!r}")
    return text


def _operation_device_id(value: Any) -> str:
    normalized = _device_id(value)
    if normalized not in _OPERATION_DEVICE_AREAS:
        raise ValueError(f"受控作业登记不支持设备: {value!r}")
    return normalized


def _operation_area_for_device(device_id: str, area: Any) -> str:
    normalized_area = _operation_area_text(area)
    expected_area = _OPERATION_DEVICE_AREAS[device_id]
    if normalized_area != expected_area:
        raise ValueError(
            f"设备 {device_id} 的作业区域必须是 {expected_area}，收到 {normalized_area!r}"
        )
    return normalized_area


def _operation_type_text(value: Any) -> str:
    text = str(value).strip()
    return text or "N/A"


def _interval_operation_type_text(value: Any) -> str:
    """Match the one trailing-space option currently present in the interval table."""

    text = _operation_type_text(value)
    if text == "未关联工艺文件（TH-03）":
        return text + " "
    return text


def _interval_status_text(value: Any) -> str:
    aliases = {
        "作业中": "作业中",
        "已结束": "已结束",
        "工艺切换结束": "工艺切换结束",
    }
    text = str(value).strip()
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError(f"不支持的作业区间状态: {value!r}") from exc


def _datetime_cell(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("时间字段必须是 datetime")
    if not value.tzinfo:
        # 已经是“业务墙上时间”的 naive 值按原样输出。
        return value.strftime("%Y-%m-%d %H:%M:%S")
    # 容器时区通常是 UTC；直接 astimezone() 会把事件时间写成 UTC 墙钟，
    # 飞书按租户时区解析后整体偏移 8 小时。统一换算到业务时区
    # （HISTORY_TIMEZONE，默认 Asia/Shanghai）再输出。
    return value.astimezone(_business_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def _snapshot_online_text(snapshot: MonitorSample | MonitorResult) -> str | None:
    quality = getattr(snapshot, "data_quality", None)
    if quality is not None and str(getattr(quality, "value", quality)).strip() == "OFFLINE":
        return "离线"
    return _online_status_text(getattr(snapshot, "online_status", None))


def _number_value(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _put_number(fields: dict[str, Any], name: str, value: Any) -> None:
    number = _number_value(value)
    if number is not None:
        # All temperature/humidity number fields in the confirmed Base schema
        # use one decimal place.
        fields[name] = round(number, 1)


def _put_text(fields: dict[str, Any], name: str, value: Any) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        fields[name] = text


def _environment_status_text(value: Any) -> str | None:
    if value is None:
        return None
    aliases = {
        "NORMAL": "正常",
        "VIOLATION": "超限",
        "UNKNOWN": "数据异常",
        "正常": "正常",
        "超限": "超限",
        "待工艺标准": "待工艺标准",
        "数据异常": "数据异常",
    }
    text = str(getattr(value, "value", value)).strip()
    return aliases.get(text, text or None)


def _put_monitoring_system_status(
    fields: dict[str, Any], name: str, value: Any,
) -> None:
    if value is None:
        return
    aliases = {
        "ONLINE": "在线正常",
        "在线": "在线正常",
        "在线正常": "在线正常",
        "OFFLINE": "离线/数据异常",
        "离线": "离线/数据异常",
        "离线/数据异常": "离线/数据异常",
        "UNKNOWN": "待确认",
        "待确认": "待确认",
    }
    text = str(getattr(value, "value", value)).strip()
    if text not in aliases:
        raise ValueError(f"不支持的监测系统状态: {value!r}")
    fields[name] = aliases[text]


def _put_site_storage_status(fields: dict[str, Any], name: str, value: Any) -> None:
    if value is None:
        return
    aliases = {
        "正常": "正常，无明显异常",
        "正常，无明显异常": "正常，无明显异常",
        "发现异常": "发现异常",
        "待跟进": "待跟进",
    }
    text = str(getattr(value, "value", value)).strip()
    if text not in aliases:
        raise ValueError(f"不支持的现场仓储状态: {value!r}")
    fields[name] = aliases[text]


def _put_status(fields: dict[str, Any], name: str, value: Any) -> None:
    if value is None:
        return
    text = _temperature_status_text(str(getattr(value, "value", value)))
    if text:
        fields[name] = text


def _put_alarm_status(fields: dict[str, Any], name: str, value: Any) -> None:
    if value is None:
        return
    aliases = {
        "NORMAL": "未触发",
        "PENDING": "计时中",
        "ALARM": "已发警报",
        "未触发": "未触发",
        "计时中": "计时中",
        "已发警报": "已发警报",
    }
    text = str(getattr(value, "value", value)).strip()
    if text not in aliases:
        raise ValueError(f"不支持的报警状态: {value!r}")
    fields[name] = aliases[text]


def _temperature_status_text(value: str) -> str:
    aliases = {
        "NORMAL": "正常",
        "LOW": "低于下限",
        "HIGH": "高于上限",
        "UNKNOWN": "数据异常",
        "正常": "正常",
        "低于下限": "低于下限",
        "高于上限": "高于上限",
        "数据异常": "数据异常",
        "数据缺失": "数据缺失",
    }
    return aliases.get(str(value).strip(), str(value).strip())


def _event_type_text(
    *, temperature_status: Any = None, humidity_status: Any = None,
) -> str | None:
    """Mirror the ledger formula's priority order for the writable type field."""

    temperature = str(getattr(temperature_status, "value", temperature_status) or "").strip()
    humidity = str(getattr(humidity_status, "value", humidity_status) or "").strip()
    if temperature in {"HIGH", "高于上限"}:
        return "温度高于上限"
    if temperature in {"LOW", "低于下限"}:
        return "温度低于下限"
    if humidity in {"HIGH", "高于上限"}:
        return "湿度高于上限"
    if humidity in {"LOW", "低于下限"}:
        return "湿度低于下限"
    if temperature in {"OFFLINE", "离线"} or humidity in {"OFFLINE", "离线"}:
        return "设备离线"
    if temperature or humidity:
        return "数据异常"
    return None


def _online_status_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    if text in {"online", "在线", "正常", "true", "1"}:
        return "在线"
    if text in {"offline", "离线", "false", "0"}:
        return "离线"
    return str(value).strip() or None


def _user_cell(value: Any) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value]
    result: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, str):
            user_id = item.strip()
        elif isinstance(item, Mapping):
            nested_value = item.get("value")
            if nested_value is not None:
                result.extend(_user_cell(nested_value))
                continue
            user_id = str(item.get("id", "")).strip()
        else:
            user_id = ""
        if not user_id:
            raise ValueError("人员字段必须提供 open_id/user id，不能使用姓名")
        result.append({"id": user_id})
    if not result:
        raise ValueError("人员字段不能为空")
    return result


def _link_cell(record_id: str) -> list[dict[str, str]]:
    return [{"id": _required_id(record_id, "parent_record_id")}]


def _field_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_field_text(item) for item in value).strip()
    if isinstance(value, Mapping):
        if "value" in value:
            return _field_text(value["value"])
        return str(value.get("text", value.get("name", ""))).strip()
    return str(value or "").strip()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        from datetime import timezone

        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        return datetime.fromisoformat(_field_text(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeishuWriteError(f"无法解析时间字段: {value!r}") from exc
