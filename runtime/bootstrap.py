"""Dependency assembly for the long-running Shadow Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
from typing import Any, Callable

import config
from application.action_executor import ActionExecutor, AutomationMode
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from application.operation_sync import OperationObservationService
from application.shadow import ShadowComparisonService
from application.standard_sync import StandardSyncService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import AlarmActionType, ControlType, DeviceContext
from integrations.feishu_observation import (
    FeishuBitableObservationSource,
    FeishuObservationAdapter,
    FeishuObservationFieldMap,
    FeishuObservationTableFieldMap,
)
from integrations.feishu_operation import FeishuOperationAdapter, FeishuOperationFieldMap
from integrations.feishu_records import FeishuBitableRecordSource
from integrations.feishu_standard import FeishuStandardAdapter
from integrations.feishu_standard_config import FEISHU_STANDARD_FIELD_MAP
from integrations.feishu_writers import (
    FeishuBitableRecordWriter,
    FeishuEnvironmentEventWriter,
    FeishuInspectionRecordWriter,
    FeishuOperationRecordWriter,
)
from repositories import (
    SQLiteAlarmStateRepository,
    SQLiteAutomationRunRepository,
    SQLiteAutomationTaskRepository,
    SQLiteEnvironmentEventRepository,
    SQLiteLatestSampleRepository,
    SQLiteOperationRepository,
    SQLiteStandardRepository,
    SQLiteStandardResolver,
    connect,
)
from repositories.sqlite import verify_runtime_schema
from scheduler.worker import TaskScheduler

from .shadow_runner import ShadowRuntime


logger = logging.getLogger("temperature_monitor")


class RuntimeBootstrapError(ValueError):
    """Configuration prevents a safe Shadow Runtime from being assembled."""


DEFAULT_DEVICE_CONTEXTS: dict[str, tuple[str, ControlType]] = {
    "TH-01": ("对拖测试区", ControlType.MONITOR_ONLY),
    "TH-02": ("螺旋桨测试间", ControlType.MONITOR_ONLY),
    "TH-03": ("精密装配间", ControlType.OPERATION_PERIOD),
    "TH-04": ("电力电子实验室", ControlType.OPERATION_PERIOD),
    "TH-05": ("通用总装线", ControlType.OPERATION_PERIOD),
    "TH-06": ("通用总装线", ControlType.MONITOR_ONLY),
    "TH-07": ("特种工艺间", ControlType.OPERATION_PERIOD),
    "TH-08": ("防爆仓库", ControlType.ALL_DAY),
    "TH-09": ("仓库", ControlType.ALL_DAY),
    "TH-10": ("PE仓库", ControlType.ALL_DAY),
    "TH-11": ("设备区", ControlType.ALL_DAY),
}


@dataclass
class RuntimeComponents:
    """Inspectable bootstrap result that delegates lifecycle to the runtime."""

    runtime: ShadowRuntime
    connection: Any
    task_repository: SQLiteAutomationTaskRepository
    event_repository: SQLiteEnvironmentEventRepository
    standard_repository: SQLiteStandardRepository
    operation_repository: SQLiteOperationRepository
    latest_sample_repository: SQLiteLatestSampleRepository
    operation_writer: FeishuOperationRecordWriter
    event_writer: FeishuEnvironmentEventWriter
    inspection_writer: FeishuInspectionRecordWriter

    def start(self) -> None:
        self.runtime.start()

    def stop(self) -> None:
        self.runtime.stop()

    def handle_sample(self, sample: Any) -> Any:
        return self.runtime.handle_sample(sample)

    def status(self) -> dict[str, Any]:
        return self.runtime.status()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)


# 最近一次 build_runtime 的结果，供 /api/system/status 只读暴露运行时健康。
# 测试会反复 build/stop，这里只保存引用，不持有额外资源。
_last_components: Any = None


def _active_write_allowed(mode: str) -> bool:
    """Active 写回三开关：mode=active + FEISHU_WRITE_ENABLED + CUTOVER ACK。"""
    return (
        mode == AutomationMode.ACTIVE.value
        and config.FEISHU_WRITE_ENABLED
        and config.ACTIVE_CUTOVER_ACK == config.ACTIVE_CUTOVER_ACK_EXPECTED
    )


def active_block_reason() -> str | None:
    """Why Active writes are blocked; None when not attempting Active mode."""
    if str(config.AUTOMATION_MODE).strip().lower() != AutomationMode.ACTIVE.value:
        return None
    reasons: list[str] = []
    if not config.FEISHU_WRITE_ENABLED:
        reasons.append("FEISHU_WRITE_ENABLED=true is required")
    if config.ACTIVE_CUTOVER_ACK != config.ACTIVE_CUTOVER_ACK_EXPECTED:
        reasons.append(
            f"ACTIVE_CUTOVER_ACK={config.ACTIVE_CUTOVER_ACK_EXPECTED} is required "
            "(legacy Feishu automation workflows must be disabled by hand first)"
        )
    return "; ".join(reasons) if reasons else None


def runtime_status() -> dict[str, Any]:
    """Return the live Shadow Runtime status for observability endpoints."""
    if _last_components is None:
        return {"available": False, "reason": "runtime not built"}
    try:
        status = _last_components.status()
    except Exception:  # noqa: BLE001 - status endpoint must never raise
        return {"available": False, "reason": "runtime status unavailable"}
    block = active_block_reason()
    if block is not None:
        status["active_block_reason"] = block
    return status


def shadow_summary_snapshot(*, hours: int = 24) -> dict[str, Any]:
    """Read-only Shadow aggregates for /api/shadow/summary."""
    if _last_components is None:
        return {
            "available": False,
            "reason": "runtime not built",
            "hours": hours,
        }
    try:
        summary = _last_components.shadow_summary(hours=hours)
    except Exception:  # noqa: BLE001 - summary endpoint must never raise
        logger.exception("Shadow summary 聚合失败")
        return {"available": False, "reason": "shadow summary unavailable", "hours": hours}
    summary["available"] = True
    return summary


def build_runtime(
    *,
    connection: Any | None = None,
    record_source: FeishuBitableRecordSource | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> RuntimeComponents:
    """Build the complete dependency graph without performing network I/O."""
    mode = str(config.AUTOMATION_MODE).strip().lower()
    if mode not in {item.value for item in AutomationMode}:
        mode = "disabled"
        mode_error = "AUTOMATION_MODE 必须是 disabled、shadow 或 active"
    else:
        mode_error = None

    runtime_connection = connection or connect(
        config.SQLITE_DB_PATH if config.SQLITE_ENABLED else ":memory:"
    )
    task_repository = SQLiteAutomationTaskRepository(runtime_connection)
    event_repository = SQLiteEnvironmentEventRepository(runtime_connection)
    standard_repository = SQLiteStandardRepository(runtime_connection)
    operation_repository = SQLiteOperationRepository(runtime_connection)
    latest_sample_repository = SQLiteLatestSampleRepository(runtime_connection)
    run_repository = SQLiteAutomationRunRepository(runtime_connection)
    # 半旧 schema 拒绝启动：各仓储构造时会做增量迁移/建表，此处校验迁移后
    # 仍缺关键列时显式报错，绝不静默运行在残缺 schema 上（旧采集链路不受
    # 影响——build_runtime 的调用方会捕获并记录，legacy 继续可用）。
    schema_missing = verify_runtime_schema(runtime_connection)
    if schema_missing:
        raise RuntimeBootstrapError(
            "SQLite schema incomplete; refusing to start Shadow Runtime: "
            + "; ".join(schema_missing)
        )

    source = record_source or FeishuBitableRecordSource()
    standard_table_id = config.FEISHU_STANDARD_TABLE_ID or "__missing_standard_table__"
    operation_table_id = config.FEISHU_OPERATION_TABLE_ID or "__missing_operation_table__"
    device_table_id = config.FEISHU_DEVICE_TABLE_ID or "__missing_device_table__"
    event_table_id = config.FEISHU_EVENT_TABLE_ID or "__missing_event_table__"
    standard_adapter = FeishuStandardAdapter(
        source=source,
        table_id=standard_table_id,
        fields=FEISHU_STANDARD_FIELD_MAP,
    )
    operation_adapter = FeishuOperationAdapter(
        source=source,
        table_id=operation_table_id,
        fields=FeishuOperationFieldMap(
            device_id=config.FEISHU_OPERATION_DEVICE_FIELD,
            area_id=config.FEISHU_OPERATION_AREA_FIELD,
            action=config.FEISHU_OPERATION_ACTION_FIELD,
            operation_type=config.FEISHU_OPERATION_TYPE_FIELD,
            work_order=config.FEISHU_OPERATION_WORK_ORDER_FIELD,
            validation=config.FEISHU_OPERATION_VALIDATION_FIELD or None,
            valid_values=(config.FEISHU_OPERATION_VALIDATION_VALUE,),
            allowed_device_ids=frozenset(config.FEISHU_OPERATION_ALLOWED_DEVICES),
        ),
    )
    record_writer = FeishuBitableRecordWriter()
    operation_writer = FeishuOperationRecordWriter(
        writer=record_writer,
        operation_table_id=operation_table_id,
        interval_table_id=config.FEISHU_OPERATION_INTERVAL_TABLE_ID,
        device_table_id=device_table_id,
        source=source,
    )
    event_writer = FeishuEnvironmentEventWriter(
        writer=record_writer,
        source=source,
        event_table_id=event_table_id,
        device_table_id=device_table_id,
        device_id_field=config.DEVICE_ID_FIELD,
    )
    inspection_writer = FeishuInspectionRecordWriter(
        writer=record_writer,
        inspection_table_id=config.FEISHU_INSPECTION_TABLE_ID,
        device_table_id=device_table_id,
        source=source,
    )
    observation_source = FeishuBitableObservationSource(
        source=source,
        device_table_id=device_table_id,
        event_table_id=event_table_id,
        fields=FeishuObservationTableFieldMap(
            device_id=config.DEVICE_ID_FIELD,
            event_device_id=config.FEISHU_EVENT_DEVICE_FIELD,
            event_status=config.FEISHU_EVENT_STATUS_FIELD,
        ),
    )
    observation_adapter = FeishuObservationAdapter(
        source=observation_source,
        fields=FeishuObservationFieldMap(
            alarm_state=config.FEISHU_OBSERVATION_ALARM_FIELD,
            operation_state=config.FEISHU_OBSERVATION_OPERATION_FIELD,
            operation_type=config.FEISHU_OBSERVATION_OPERATION_TYPE_FIELD,
            event_exists="__event_exists",
            overall_status=config.FEISHU_OBSERVATION_OVERALL_FIELD,
            standard_id=config.FEISHU_OBSERVATION_STANDARD_ID_FIELD or None,
            standard_revision=config.FEISHU_OBSERVATION_STANDARD_REVISION_FIELD or None,
            active_event_count="__active_event_count",
            pending_closure_count="__pending_closure_count",
            observed_at="__observed_at",
            data_quality=config.FEISHU_OBSERVATION_DATA_QUALITY_FIELD,
            temperature_status=config.FEISHU_OBSERVATION_TEMP_STATUS_FIELD,
            humidity_status=config.FEISHU_OBSERVATION_HUMIDITY_STATUS_FIELD,
            active_event_ids="__active_event_ids",
        ),
    )

    effective_mode = (
        mode
        if mode == AutomationMode.SHADOW.value or _active_write_allowed(mode)
        else AutomationMode.DISABLED.value
    )
    action_executor = ActionExecutor(
        mode=effective_mode,
        handlers={
            # Verification tasks are already persisted by the application
            # service; Active mode should audit them as successful local work,
            # not treat them as missing Feishu handlers.
            AlarmActionType.CREATE_VERIFY_TASK: lambda action: None,
            AlarmActionType.CANCEL_VERIFY_TASK: lambda action: None,
            AlarmActionType.COMPLETE_VERIFY_TASK: lambda action: None,
        },
        context_handlers={
            AlarmActionType.CREATE_ALARM_EVENT: event_writer.handle_alarm_action,
            AlarmActionType.UPDATE_ALARM_EVENT: event_writer.handle_alarm_action,
            AlarmActionType.START_RECOVERY: event_writer.handle_alarm_action,
            AlarmActionType.CLOSE_ALARM_EVENT: event_writer.handle_alarm_action,
        },
        recorder=run_repository,
    )
    operation_state_provider = operation_repository
    monitor_service = MonitorApplicationService(
        operation_state_provider=operation_state_provider,
        standard_resolver=SQLiteStandardResolver(standard_repository),
        alarm_state_repository=SQLiteAlarmStateRepository(runtime_connection),
        alarm_state_machine=AlarmStateMachine(),
        action_mapper=ApplicationActionMapper(),
        action_executor=action_executor,
        now_provider=now_provider,
        task_repository=task_repository,
        event_repository=event_repository,
        latest_sample_repository=latest_sample_repository,
    )
    shadow_comparison = ShadowComparisonService(
        observation_adapter=observation_adapter,
        recorder=run_repository,
        max_feishu_delay=timedelta(
            seconds=config.SHADOW_FEISHU_DELAY_SECONDS
        ),
    )

    devices, device_error = _build_device_contexts()
    missing = _missing_configuration()
    reason_parts = [part for part in (mode_error, device_error, *missing) if part]
    if mode == AutomationMode.ACTIVE.value:
        active_block = active_block_reason()
        if active_block:
            reason_parts.append(
                f"Active mode blocked: {active_block}; no Feishu writes are enabled"
            )
    if not config.SQLITE_ENABLED:
        reason_parts.append("SQLITE_ENABLED=false，Shadow 无法持久化内部状态")
    available = mode in {
        AutomationMode.SHADOW.value,
        AutomationMode.ACTIVE.value,
    } and not reason_parts
    worker_id = config.SHADOW_WORKER_ID or f"shadow-{os.getpid()}"

    runtime_holder: dict[str, ShadowRuntime] = {}
    scheduler = TaskScheduler(
        repository=task_repository,
        worker_id=worker_id,
        poll_interval=config.SHADOW_SCHEDULER_POLL_SECONDS,
        handlers={
            "VERIFY_ALARM": lambda task: runtime_holder["runtime"].handle_verify_alarm(task),
            "VERIFY_RECOVERY": lambda task: runtime_holder["runtime"].handle_verify_recovery(task),
            "SHADOW_COMPARE": lambda task: runtime_holder["runtime"].handle_shadow_compare(task),
            "SYNC_STANDARD": lambda task: runtime_holder["runtime"].handle_standard_sync(task),
            "SYNC_OPERATIONS": lambda task: runtime_holder["runtime"].handle_operation_sync(task),
        },
        now_provider=now_provider,
    )
    runtime = ShadowRuntime(
        mode=mode,
        available=available,
        unavailable_reason="; ".join(reason_parts) if reason_parts else None,
        feishu_readonly_available=not missing,
        feishu_write_enabled=_active_write_allowed(mode),
        devices=devices,
        monitor_service=monitor_service,
        standard_sync=StandardSyncService(
            source=standard_adapter,
            repository=standard_repository,
            source_name=f"feishu:{standard_table_id}",
        ),
        operation_adapter=operation_adapter,
        operation_sync=OperationObservationService(store=operation_repository),
        shadow_comparison=shadow_comparison,
        scheduler=scheduler,
        task_repository=task_repository,
        event_repository=event_repository,
        latest_sample_repository=latest_sample_repository,
        standard_repository=standard_repository,
        connection=runtime_connection,
        worker_id=worker_id,
        operation_sync_interval=config.SHADOW_OPERATION_SYNC_SECONDS,
        standard_sync_interval=config.SHADOW_STANDARD_SYNC_SECONDS,
        now_provider=now_provider,
        shutdown_timeout=config.RUNTIME_SHUTDOWN_TIMEOUT_SECONDS,
    )
    runtime_holder["runtime"] = runtime
    global _last_components
    _last_components = runtime
    return RuntimeComponents(
        runtime=runtime,
        connection=runtime_connection,
        task_repository=task_repository,
        event_repository=event_repository,
        standard_repository=standard_repository,
        operation_repository=operation_repository,
        latest_sample_repository=latest_sample_repository,
        operation_writer=operation_writer,
        event_writer=event_writer,
        inspection_writer=inspection_writer,
    )


def _missing_configuration() -> tuple[str, ...]:
    missing: list[str] = []
    if not config.APP_ID:
        missing.append("缺少 FEISHU_APP_ID/APP_ID")
    if not config.APP_SECRET:
        missing.append("缺少 FEISHU_APP_SECRET/APP_SECRET")
    if not config.APP_TOKEN:
        missing.append("缺少 FEISHU_BASE_APP_TOKEN/APP_TOKEN")
    if not config.FEISHU_DEVICE_TABLE_ID:
        missing.append("缺少 TABLE_ID/FEISHU_DEVICE_TABLE_ID")
    if not config.FEISHU_STANDARD_TABLE_ID:
        missing.append("缺少 FEISHU_STANDARD_TABLE_ID")
    if not config.FEISHU_OPERATION_TABLE_ID:
        missing.append("缺少 FEISHU_OPERATION_TABLE_ID")
    if not config.FEISHU_EVENT_TABLE_ID:
        missing.append("缺少 FEISHU_EVENT_TABLE_ID")
    if not config.FEISHU_OPERATION_INTERVAL_TABLE_ID:
        missing.append("缺少 FEISHU_OPERATION_INTERVAL_TABLE_ID")
    if not config.FEISHU_INSPECTION_TABLE_ID:
        missing.append("缺少 FEISHU_INSPECTION_TABLE_ID")
    return tuple(missing)


def _build_device_contexts() -> tuple[dict[str, DeviceContext], str | None]:
    contexts = dict(DEFAULT_DEVICE_CONTEXTS)
    for device_id, override in config.SHADOW_DEVICE_CONTEXTS.items():
        control = override.get("control_type", "")
        contexts[device_id] = (
            override["area"],
            ControlType(control) if control in {item.value for item in ControlType} else control,
        )
    devices: dict[str, DeviceContext] = {}
    missing: list[str] = []
    for device_id in config.SHADOW_DEVICE_IDS:
        context = contexts.get(device_id)
        if context is None:
            missing.append(device_id)
            continue
        area, control_type = context
        devices[device_id] = DeviceContext(
            device_id=device_id,
            area=area,
            control_type=control_type,
        )
    if missing:
        return devices, "缺少 Shadow 设备上下文: " + ",".join(missing)
    return devices, None
