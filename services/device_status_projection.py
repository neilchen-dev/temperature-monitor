"""Asynchronous projection of the current device context to Feishu.

The monitor, operation and standard pipelines own local state.  This module
only turns that state into a desired device-row view and queues a durable
``PROJECT_DEVICE_STATUS`` task.  Feishu reads/updates happen in the scheduler
handler, after the local transaction has committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Any, Callable, Mapping

import config
from domain.models import (
    AlarmLifecycleState,
    AlarmState,
    ApplicabilityStatus,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationState,
    OperationStatus,
)
from domain.monitor_engine import MonitorEngine
from domain.standard_resolver import StandardResolutionError
from integrations.feishu_writers import FeishuDeviceStatusWriter, _user_cell
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.runtime_state import SQLiteDeviceStatusProjectionRepository


logger = logging.getLogger("temperature_monitor")

PROJECT_DEVICE_STATUS = "PROJECT_DEVICE_STATUS"
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class DeviceStatusDesired:
    """One immutable desired-state snapshot used by a projection task."""

    device_id: str
    fields: Mapping[str, Any]
    desired_hash: str
    standard: EnvironmentStandard | None
    operation_state: OperationState
    alarm_state: AlarmState
    sample: MonitorSample | None


def _normalized_device(device_id: str) -> str:
    value = str(device_id).strip().upper()
    if not value:
        raise ValueError("device_id cannot be empty")
    return value


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _canonical(value: Any) -> Any:
    """Compare Feishu cell values without treating equivalent cell shapes as drift."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return value
    if isinstance(value, list):
        items = tuple(_canonical(item) for item in value)
        return tuple(sorted(items, key=repr)) or None
    if isinstance(value, Mapping):
        for key in ("id", "open_id", "user_id", "union_id", "email"):
            if value.get(key):
                return (key, str(value[key]).strip())
        if "value" in value:
            return _canonical(value["value"])
        return tuple(sorted((str(key), _canonical(item)) for key, item in value.items()))
    return str(value).strip()


def _stable_hash(fields: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(fields), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _control_type_text(value: ControlType | str | None) -> str | None:
    if value is None:
        return None
    normalized = getattr(value, "value", value)
    return {
        "ALL_DAY": "全天控制",
        "OPERATION_PERIOD": "作业期间控制",
        "MONITOR_ONLY": "仅监测",
    }.get(str(normalized).strip(), str(normalized).strip() or None)


def _operation_state_text(value: OperationStatus | str) -> str:
    normalized = getattr(value, "value", value)
    return {
        "OPERATING": "作业中",
        "IDLE": "无作业",
        "NOT_APPLICABLE": "N/A",
    }.get(str(normalized).strip(), "N/A")


def _alarm_state_text(
    *,
    alarm_state: AlarmState,
    standard: EnvironmentStandard | None,
    operation_state: OperationState,
    sample: MonitorSample | None,
    device: DeviceContext,
) -> str:
    """Map the Python lifecycle to the existing Feishu select labels."""
    if alarm_state.prewarning_active:
        return "预警"
    state = alarm_state.state
    if state is AlarmLifecycleState.NORMAL and sample is not None and standard is not None:
        try:
            result = MonitorEngine.evaluate(
                device=device,
                sample=sample,
                standard=standard,
                operation_state=operation_state,
                previous_prewarning_reasons=(),
                temperature_prewarning_margin=config.TEMPERATURE_PREWARNING_MARGIN_C,
                humidity_prewarning_margin=config.HUMIDITY_PREWARNING_MARGIN_RH,
                temperature_prewarning_exit_margin=config.TEMPERATURE_PREWARNING_EXIT_MARGIN_C,
                humidity_prewarning_exit_margin=config.HUMIDITY_PREWARNING_EXIT_MARGIN_RH,
            )
            if result.applicability is not ApplicabilityStatus.APPLICABLE:
                return "N/A"
        except Exception:  # noqa: BLE001 - a projection must not affect monitoring
            logger.warning(
                "device status applicability projection fallback | device=%s",
                device.device_id,
                exc_info=True,
            )
    return {
        AlarmLifecycleState.NORMAL: "未触发",
        AlarmLifecycleState.PENDING: "计时中",
        AlarmLifecycleState.ALARM: "已发警报",
        AlarmLifecycleState.RECOVERY: "恢复中",
    }.get(state, "N/A")


class DeviceStatusProjector:
    """Build, queue and execute idempotent device status projections."""

    def __init__(
        self,
        *,
        devices: Mapping[str, DeviceContext],
        standard_resolver: Any,
        operation_state_provider: Any,
        alarm_state_repository: Any,
        latest_sample_repository: Any,
        task_repository: SQLiteAutomationTaskRepository,
        state_repository: SQLiteDeviceStatusProjectionRepository,
        writer: FeishuDeviceStatusWriter,
        active_device_ids: tuple[str, ...] = (),
        standards_ready_provider: Callable[[], bool] | None = None,
        default_owner_by_device: Mapping[str, Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.devices = {
            _normalized_device(key): value for key, value in devices.items()
        }
        self.standard_resolver = standard_resolver
        self.operation_state_provider = operation_state_provider
        self.alarm_state_repository = alarm_state_repository
        self.latest_sample_repository = latest_sample_repository
        self.task_repository = task_repository
        self.state_repository = state_repository
        self.writer = writer
        self.active_device_ids = tuple(_normalized_device(item) for item in active_device_ids)
        self.standards_ready_provider = standards_ready_provider
        self.default_owner_by_device = {
            _normalized_device(key): value
            for key, value in (default_owner_by_device or {}).items()
        }
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())

    def _validate_writer_schema(self) -> None:
        validate_schema = getattr(self.writer, "validate_schema", None)
        if callable(validate_schema):
            validate_schema()

    @staticmethod
    def _observed_and_changed(
        record: Any,
        desired_fields: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        observed_fields = {
            key: record.fields.get(key) for key in desired_fields
        }
        changed_fields = [
            key
            for key, value in desired_fields.items()
            if _canonical(record.fields.get(key)) != _canonical(value)
        ]
        return observed_fields, changed_fields

    def build_desired(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
    ) -> DeviceStatusDesired:
        normalized = _normalized_device(device_id)
        try:
            device = self.devices[normalized]
        except KeyError as exc:
            raise ValueError(f"device is not configured: {normalized}") from exc
        current = now or self.now_provider()
        operation_state = self.operation_state_provider.get(device)
        sample = self.latest_sample_repository.get(normalized)
        alarm_state = self.alarm_state_repository.get(normalized) or AlarmState.normal(normalized)
        standard: EnvironmentStandard | None = None
        resolution_time = sample.sample_time if sample is not None else current
        try:
            standard = self.standard_resolver.resolve(
                area_id=operation_state.area_id,
                operation_type=operation_state.operation_type,
                timestamp=resolution_time,
                device_id=normalized,
            )
        except StandardResolutionError:
            # A missing/temporarily unavailable standard is represented as an
            # empty current-applicable limit set; the Active write gate still
            # refuses the task until standards_ready is true.
            logger.warning(
                "device status has no resolved standard | device=%s",
                normalized,
            )

        fields: dict[str, Any] = {
            self.writer.fields.temperature_min: _number(
                standard.temperature_min if standard else None
            ),
            self.writer.fields.temperature_max: _number(
                standard.temperature_max if standard else None
            ),
            self.writer.fields.humidity_min: _number(
                standard.humidity_min if standard else None
            ),
            self.writer.fields.humidity_max: _number(
                standard.humidity_max if standard else None
            ),
            self.writer.fields.control_type: _control_type_text(
                standard.control_type if standard else None
            ),
            self.writer.fields.operation_state: _operation_state_text(operation_state.state),
            self.writer.fields.operation_type: (
                operation_state.operation_type
                if operation_state.state is OperationStatus.OPERATING
                and operation_state.operation_type
                else "N/A"
            ),
            self.writer.fields.alarm_status: _alarm_state_text(
                alarm_state=alarm_state,
                standard=standard,
                operation_state=operation_state,
                sample=sample,
                device=device,
            ),
            self.writer.fields.operation_started_at: (
                int(operation_state.started_at.timestamp() * 1000)
                if operation_state.state is OperationStatus.OPERATING
                and operation_state.started_at is not None
                else None
            ),
        }
        if normalized in self.default_owner_by_device:
            fields[self.writer.fields.default_owner] = _user_cell(
                self.default_owner_by_device[normalized]
            )
        return DeviceStatusDesired(
            device_id=normalized,
            fields=fields,
            desired_hash=_stable_hash(fields),
            standard=standard,
            operation_state=operation_state,
            alarm_state=alarm_state,
            sample=sample,
        )

    def request(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
        force: bool = False,
        trigger: str = "state_change",
    ) -> Any | None:
        """Commit desired state locally and enqueue one deduplicated task."""
        current = now or self.now_provider()
        desired = self.build_desired(device_id, now=current)
        existing = self.state_repository.get(desired.device_id)
        same = bool(
            existing is not None
            and existing.get("desired_hash") == desired.desired_hash
        )
        self.state_repository.save_desired(
            device_id=desired.device_id,
            record_id=existing.get("record_id") if existing else None,
            desired_hash=desired.desired_hash,
            desired_fields=desired.fields,
            updated_at=current,
        )
        activation = self.task_repository.runtime_context()
        if (
            activation.mode == "active" and desired.device_id not in self.active_device_ids
        ):
            self.state_repository.mark_shadow_only(
                device_id=desired.device_id,
                updated_at=current,
            )
            return None
        if (
            not force
            and same
            and existing
            and existing.get("status") in {"SUCCEEDED", "SHADOW_ONLY"}
            and not (
                existing.get("status") == "SHADOW_ONLY"
                and config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED
                and activation.mode == "active"
            )
        ):
            return None

        task = self.task_repository.create_or_get_unfinished(
            task_type=PROJECT_DEVICE_STATUS,
            entity_type="DEVICE",
            entity_id=desired.device_id,
            due_at=current,
            payload={
                "device_id": desired.device_id,
                "desired_hash": desired.desired_hash,
                "desired_fields": dict(desired.fields),
                "projection_version": 1,
                "projection_attempt": 0,
                "trigger": trigger,
                "external_effect_policy": (
                    "DEVICE_STATUS_PROJECTION"
                    if activation.mode == "active"
                    else "SHADOW_ONLY"
                ),
            },
            dedupe_key=(
                f"PROJECT_DEVICE_STATUS:{desired.device_id}:{desired.desired_hash}"
            ),
            created_at=current,
        )
        if activation.mode == "active":
            self.state_repository.mark_pending(
                device_id=desired.device_id,
                task_id=task.task_id,
            )
        else:
            self.state_repository.mark_shadow_only(
                device_id=desired.device_id,
                updated_at=current,
            )
        return task

    def execute_task(self, task: Any, *, now: datetime | None = None) -> None:
        """Execute one task; all Feishu I/O is outside the SQLite writes."""
        device_id = _normalized_device(task.entity_id)
        payload = dict(task.payload)
        desired_hash = str(payload.get("desired_hash") or "")
        state = self.state_repository.get(device_id)
        if state is None or state.get("desired_hash") != desired_hash:
            logger.info(
                "device status projection task superseded | device=%s | task=%s",
                device_id,
                task.task_id,
            )
            return

        desired_fields = dict(payload.get("desired_fields") or {})

        # The independent projection gate deliberately permits a read-only
        # reconcile even while formal Active remains enabled.  This records
        # the real remote row and drift without treating it as a successful
        # write, and it never evaluates the external-write safety gates as a
        # reason to perform a remote update.
        if not config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED:
            self._validate_writer_schema()
            record = self.writer.read_device_record(device_id)
            observed_fields, changed_fields = self._observed_and_changed(
                record, desired_fields
            )
            reconciled_at = now or self.now_provider()
            self.state_repository.mark_gated(
                device_id=device_id,
                record_id=record.record_id,
                observed_fields=observed_fields,
                changed_fields=changed_fields,
                reconciled_at=reconciled_at,
                task_id=task.task_id,
            )
            logger.info(
                "device status projection gated; remote update skipped | "
                "device=%s | task=%s | changed_fields=%s",
                device_id,
                task.task_id,
                ",".join(sorted(changed_fields)) or "none",
            )
            return

        if not config.FEISHU_WRITE_ENABLED:
            raise RuntimeError("FEISHU_WRITE_ENABLED=false; device status projection is disabled")
        if config.ACTIVE_CUTOVER_ACK != config.ACTIVE_CUTOVER_ACK_EXPECTED:
            raise RuntimeError("ACTIVE_CUTOVER_ACK is not valid; device status projection is disabled")
        if not self.task_repository.external_effect_allowed(task):
            raise RuntimeError("device status projection is outside current Active boundary")
        if device_id not in self.active_device_ids:
            self.state_repository.mark_shadow_only(
                device_id=device_id,
                updated_at=now or self.now_provider(),
            )
            return
        if self.standards_ready_provider is not None and not self.standards_ready_provider():
            raise RuntimeError("standards_ready=false; device status projection remains pending")

        self._validate_writer_schema()
        record = self.writer.read_device_record(device_id)
        # The default owner is device configuration on the same Feishu row.
        # If no explicit local owner mapping was supplied, leave that source-
        # owned field untouched and out of the desired-state hash.  This both
        # preserves the configured recipient and prevents status from showing
        # a permanent desired/observed mismatch.  Explicit mappings were
        # normalized in build_desired and remain managed fields.
        _observed_fields, changed_fields = self._observed_and_changed(
            record, desired_fields
        )
        patch = {key: desired_fields[key] for key in changed_fields}
        if patch:
            # This call is the only remote write in the entire projection
            # path.  Local status is marked successful only after it returns.
            self.writer.update(record_id=record.record_id, fields=patch)
        self.state_repository.mark_success(
            device_id=device_id,
            record_id=record.record_id,
            observed_fields=desired_fields,
            completed_at=now or self.now_provider(),
        )
        logger.info(
            "device status projection succeeded | device=%s | task=%s | "
            "updated_fields=%s",
            device_id,
            task.task_id,
            ",".join(sorted(patch)) or "none",
        )

    def handle_task(self, task: Any, *, now: datetime | None = None) -> None:
        """Retry transient projection failures without losing task identity."""
        current = now or self.now_provider()
        try:
            self.execute_task(task, now=current)
        except Exception as exc:
            attempt = int(task.payload.get("projection_attempt", 0) or 0) + 1
            max_attempts = max(
                1,
                int(getattr(config, "FEISHU_DEVICE_STATUS_MAX_RETRIES", _DEFAULT_MAX_RETRIES)),
            )
            terminal = attempt >= max_attempts
            self.state_repository.mark_failure(
                device_id=task.entity_id,
                error=f"{type(exc).__name__}: {exc}",
                attempted_at=current,
                terminal=terminal,
            )
            if not terminal:
                delay = float(
                    getattr(
                        config,
                        "FEISHU_DEVICE_STATUS_BACKOFF_SECONDS",
                        _DEFAULT_BACKOFF_SECONDS,
                    )
                )
                retry_payload = dict(task.payload)
                retry_payload["projection_attempt"] = attempt
                self.task_repository.reschedule_running(
                    task,
                    due_at=current + timedelta(seconds=max(1.0, delay * (2 ** (attempt - 1)))),
                    updated_at=current,
                    payload=retry_payload,
                )
            logger.warning(
                "device status projection failed | device=%s | task=%s | "
                "attempt=%s/%s | terminal=%s | error=%s",
                task.entity_id,
                task.task_id,
                attempt,
                max_attempts,
                terminal,
                str(exc),
            )
            raise

    def summary(self) -> dict[str, Any]:
        result = self.state_repository.summary()
        result["enabled"] = bool(config.FEISHU_DEVICE_STATUS_PROJECTION_ENABLED)
        schema_status = getattr(self.writer, "schema_status", None)
        result["schema"] = (
            dict(schema_status())
            if callable(schema_status)
            else {"status": "not_supported", "missing_fields": []}
        )
        return result


__all__ = [
    "PROJECT_DEVICE_STATUS",
    "DeviceStatusDesired",
    "DeviceStatusProjector",
]
