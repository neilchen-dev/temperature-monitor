"""Continuous Shadow Runtime orchestration.

The runner is an application boundary: it receives normalized samples from the
existing acquisition path, persists Python-owned projections, and queues all
network observation work on the generic durable scheduler.  Feishu writes are
available only through explicitly injected Active-mode handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
import sqlite3
import threading
from typing import Any, Callable, Mapping

from application.monitor_service import MonitorApplicationService, MonitorHandlingResult
from application.operation_sync import OperationObservationService
from application.shadow import (
    ExpectedAutomationState,
    ShadowComparisonService,
    expected_state_from,
)
from application.standard_sync import StandardSyncService
from domain.models import DeviceContext, MonitorSample
from integrations.feishu_operation import FeishuOperationAdapter
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.runtime_state import SQLiteLatestSampleRepository
from scheduler.worker import TaskScheduler


logger = logging.getLogger("temperature_monitor")


@dataclass(frozen=True)
class RuntimeStatus:
    mode: str
    available: bool
    degraded: bool
    reason: str | None
    worker_id: str
    feishu_readonly_available: bool
    feishu_write_enabled: bool
    configured_shadow_devices: tuple[str, ...]
    scheduler_running: bool
    last_standard_sync_time: datetime | None
    enabled_standard_count: int
    last_processed_sample_time: datetime | None
    last_shadow_compare_time: datetime | None
    last_shadow_diff: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "available": self.available,
            "degraded": self.degraded,
            "reason": self.reason,
            "worker_id": self.worker_id,
            "feishu_readonly_available": self.feishu_readonly_available,
            "feishu_write_enabled": self.feishu_write_enabled,
            "configured_shadow_devices": list(self.configured_shadow_devices),
            "scheduler_running": self.scheduler_running,
            "last_standard_sync_time": _iso(self.last_standard_sync_time),
            "enabled_standard_count": self.enabled_standard_count,
            "last_processed_sample_time": _iso(self.last_processed_sample_time),
            "last_shadow_compare_time": _iso(self.last_shadow_compare_time),
            "last_shadow_diff": self.last_shadow_diff,
        }


class ShadowRuntime:
    """Own the lifecycle and durable orchestration of the Shadow chain."""

    def __init__(
        self,
        *,
        mode: str,
        available: bool,
        unavailable_reason: str | None,
        feishu_readonly_available: bool,
        feishu_write_enabled: bool,
        devices: Mapping[str, DeviceContext],
        monitor_service: MonitorApplicationService,
        standard_sync: StandardSyncService,
        operation_adapter: FeishuOperationAdapter,
        operation_sync: OperationObservationService,
        shadow_comparison: ShadowComparisonService,
        scheduler: TaskScheduler,
        task_repository: SQLiteAutomationTaskRepository,
        event_repository: SQLiteEnvironmentEventRepository,
        latest_sample_repository: SQLiteLatestSampleRepository,
        standard_repository: Any,
        connection: sqlite3.Connection,
        worker_id: str,
        operation_sync_interval: float,
        standard_sync_interval: float,
        now_provider: Callable[[], datetime] | None = None,
        shutdown_timeout: float = 15.0,
    ) -> None:
        self.mode = mode
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.feishu_readonly_available = feishu_readonly_available
        self.feishu_write_enabled = feishu_write_enabled
        self.devices = {key.strip().upper(): value for key, value in devices.items()}
        self.monitor_service = monitor_service
        self.standard_sync = standard_sync
        self.operation_adapter = operation_adapter
        self.operation_sync = operation_sync
        self.shadow_comparison = shadow_comparison
        self.scheduler = scheduler
        self.task_repository = task_repository
        self.event_repository = event_repository
        self.latest_sample_repository = latest_sample_repository
        self.standard_repository = standard_repository
        self.connection = connection
        self.worker_id = worker_id
        self.operation_sync_interval = operation_sync_interval
        self.standard_sync_interval = standard_sync_interval
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())
        self.shutdown_timeout = shutdown_timeout

        self._execution_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._started = False
        self._accepting_samples = False
        self._closed = False
        self._last_standard_sync_time: datetime | None = None
        self._last_processed_sample_time: datetime | None = None
        self._last_shadow_compare_time: datetime | None = None
        self._last_shadow_diff: str | None = None
        self._enabled_standard_count = 0

    def start(self) -> None:
        """Start local scheduling; Feishu sync and observation are background work."""
        with self._lifecycle_lock:
            if self._started:
                return
            self._started = True
            if not self.available:
                logger.error(
                    "Shadow Runtime 不可用 | mode=%s | reason=%s",
                    self.mode,
                    self.unavailable_reason,
                )
                return

            from services import devices as device_service

            self._accepting_samples = True
            device_service.register_sample_listener(self.handle_sample)
            now = self.now_provider()
            with self._execution_lock:
                self._schedule_periodic("SYNC_STANDARD", now)
                self._schedule_periodic("SYNC_OPERATIONS", now)
            self._scheduler_thread = threading.Thread(
                target=self._run_scheduler,
                args=(self._stop_event,),
                name="shadow-scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()
            logger.info(
                "Shadow Runtime ready | worker_id=%s | devices=%s | scheduler=running",
                self.worker_id,
                ",".join(self.devices) if self.devices else "none",
            )

    def stop(self) -> None:
        """Stop sample intake first, then let the durable worker release its lease."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._accepting_samples = False
            from services import devices as device_service

            device_service.unregister_sample_listener(self.handle_sample)
            self._stop_event.set()
            thread = self._scheduler_thread
            self._closed = True

        # Do not hold _lifecycle_lock while waiting: the scheduler's finally
        # block takes that lock to release the connection after its task ends.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.shutdown_timeout)
        if thread is None or not thread.is_alive():
            with self._lifecycle_lock:
                self._scheduler_thread = None
            self._close_connection()
        else:
            logger.error(
                "Shadow Runtime scheduler 在关闭超时后仍运行；保留 SQLite 连接，"
                "由 scheduler 退出回调关闭 | worker_id=%s",
                self.worker_id,
            )
        logger.info("Shadow Runtime stopped | worker_id=%s", self.worker_id)

    close = stop

    def _run_scheduler(self, stop_event: threading.Event) -> None:
        try:
            # Claiming and dispatching share the same lock as the sample
            # listener.  This keeps all repositories on the runtime's single
            # SQLite connection transaction-safe while preserving the
            # domain-agnostic TaskScheduler implementation.
            while not stop_event.is_set():
                with self._execution_lock:
                    self.scheduler.run_once()
                if stop_event.wait(self.scheduler.poll_interval):
                    return
        finally:
            with self._lifecycle_lock:
                if self._closed:
                    self._scheduler_thread = None
                    self._close_connection()

    def _close_connection(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            logger.exception("关闭 Shadow Runtime SQLite 连接失败")

    def handle_sample(self, sample: MonitorSample) -> MonitorHandlingResult | None:
        """Route one normalized acquisition sample into the Shadow pipeline."""
        device_id = sample.device_id.strip().upper()
        if not self._accepting_samples or device_id not in self.devices:
            return None
        normalized_sample = (
            sample
            if sample.device_id == device_id
            else replace(sample, device_id=device_id)
        )
        with self._execution_lock:
            result = self.monitor_service.handle_sample(
                device=self.devices[device_id],
                sample=normalized_sample,
                now=self.now_provider(),
            )
            active_events = self.event_repository.list_active(device_id=device_id)
            expected = expected_state_from(
                device_id=device_id,
                alarm_state=result.transition.next.state,
                operation_state=result.operation_state,
                overall_status=result.monitor_result.overall_status.value,
                standard_id=result.monitor_result.standard_id,
                standard_revision=result.monitor_result.standard_revision,
                active_event_count=len(active_events),
                active_event_ids=tuple(event.event_id for event in active_events),
                expected_at=self.now_provider(),
                applicability=result.monitor_result.applicability.value,
                data_quality=result.monitor_result.data_quality.value,
                temperature_status=result.monitor_result.temperature_status.value,
                humidity_status=result.monitor_result.humidity_status.value,
                operation_type=result.operation_state.operation_type,
            )
            self._schedule_shadow_compare(expected, sample_time=normalized_sample.sample_time)
            self._last_processed_sample_time = normalized_sample.sample_time
            return result

    def status(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            scheduler_running = bool(
                self._scheduler_thread is not None
                and self._scheduler_thread.is_alive()
            )
            status = RuntimeStatus(
                mode=self.mode,
                available=self.available,
                degraded=not self.available,
                reason=self.unavailable_reason,
                worker_id=self.worker_id,
                feishu_readonly_available=self.feishu_readonly_available,
                feishu_write_enabled=self.feishu_write_enabled,
                configured_shadow_devices=tuple(self.devices),
                scheduler_running=scheduler_running,
                last_standard_sync_time=self._last_standard_sync_time,
                enabled_standard_count=self._enabled_standard_count,
                last_processed_sample_time=self._last_processed_sample_time,
                last_shadow_compare_time=self._last_shadow_compare_time,
                last_shadow_diff=self._last_shadow_diff,
            ).as_dict()
            status["scheduler"] = {"running": scheduler_running}
            return status

    # The following methods are handlers for TaskScheduler.  They deliberately
    # contain orchestration only; business decisions stay in existing services.
    def handle_verify_alarm(self, task: Any) -> None:
        self._handle_verification_task(task)

    def handle_verify_recovery(self, task: Any) -> None:
        self._handle_verification_task(task)

    def handle_shadow_compare(self, task: Any) -> None:
        expected = _expected_from_payload(task.payload["expected"])
        try:
            with self._execution_lock:
                diff = self.shadow_comparison.compare(
                    expected=expected,
                    sample_time=datetime.fromisoformat(task.payload["sample_time"]),
                    created_at=self.now_provider(),
                )
                self._last_shadow_compare_time = self.now_provider()
                self._last_shadow_diff = ",".join(diff.difference_type) or "MATCH"
                logger.info(
                    "shadow sample %s %s -> %s",
                    expected.device_id,
                    expected.alarm_state,
                    self._last_shadow_diff,
                )
        except Exception:
            # Keep a durable retry task for temporary read failures while the
            # original task records its failed attempt and error context.
            retry_at = self.now_provider() + timedelta(seconds=30)
            self.task_repository.create_or_get(
                task_type="SHADOW_COMPARE",
                entity_type="DEVICE",
                entity_id=expected.device_id,
                due_at=retry_at,
                payload=dict(task.payload),
                dedupe_key=f"SHADOW_RETRY:{task.task_id}:{task.attempt_count}",
                created_at=self.now_provider(),
            )
            raise

    def handle_standard_sync(self, task: Any) -> None:
        with self._execution_lock:
            report = self.standard_sync.sync(now=self.now_provider())
            self._last_standard_sync_time = self.now_provider()
            # Read the cache after both success and failure so status keeps
            # reporting the previous active version when a remote sync fails.
            self._enabled_standard_count = sum(
                1 for standard in self.standard_repository.list_all() if standard.enabled
            )
            if report.errors:
                logger.error(
                    "Shadow 标准同步失败，保留上一版标准 | errors=%s",
                    "; ".join(report.errors),
                )
            else:
                logger.info("Shadow 标准同步完成 | enabled=%s", self._enabled_standard_count)
            self._schedule_periodic(
                "SYNC_STANDARD",
                self.now_provider() + timedelta(seconds=self.standard_sync_interval),
            )

    def handle_operation_sync(self, task: Any) -> None:
        with self._execution_lock:
            try:
                observations = self.operation_adapter.fetch_observations(
                    observed_at=self.now_provider()
                )
                accepted = 0
                for observation in observations:
                    if self.operation_sync.apply(observation).accepted:
                        accepted += 1
                logger.info(
                    "Shadow 作业状态同步完成 | observations=%s | accepted=%s",
                    len(observations),
                    accepted,
                )
            except Exception:
                logger.exception("Shadow 作业状态同步失败，保留上一版状态")
            finally:
                self._schedule_periodic(
                    "SYNC_OPERATIONS",
                    self.now_provider() + timedelta(seconds=self.operation_sync_interval),
                )

    def _handle_verification_task(self, task: Any) -> None:
        with self._execution_lock:
            sample = self.latest_sample_repository.get(task.entity_id)
            if sample is None:
                raise RuntimeError(f"no latest sample for verification device {task.entity_id}")
            result = self.monitor_service.handle_sample(
                device=self.devices[task.entity_id],
                sample=sample,
                now=self.now_provider(),
                scheduler_task_id=task.task_id,
            )
            active_events = self.event_repository.list_active(device_id=task.entity_id)
            expected = expected_state_from(
                device_id=task.entity_id,
                alarm_state=result.transition.next.state,
                operation_state=result.operation_state,
                overall_status=result.monitor_result.overall_status.value,
                standard_id=result.monitor_result.standard_id,
                standard_revision=result.monitor_result.standard_revision,
                active_event_count=len(active_events),
                active_event_ids=tuple(event.event_id for event in active_events),
                expected_at=self.now_provider(),
                applicability=result.monitor_result.applicability.value,
                data_quality=result.monitor_result.data_quality.value,
                temperature_status=result.monitor_result.temperature_status.value,
                humidity_status=result.monitor_result.humidity_status.value,
                operation_type=result.operation_state.operation_type,
            )
            self._schedule_shadow_compare(expected, sample_time=sample.sample_time)

    def _schedule_shadow_compare(
        self,
        expected: ExpectedAutomationState,
        *,
        sample_time: datetime,
    ) -> None:
        created_at = self.now_provider()
        self.task_repository.create_or_get(
            task_type="SHADOW_COMPARE",
            entity_type="DEVICE",
            entity_id=expected.device_id,
            due_at=created_at,
            payload={
                "sample_time": sample_time.isoformat(),
                "expected": _expected_payload(expected),
            },
            dedupe_key=f"SHADOW_COMPARE:{expected.device_id}:{sample_time.isoformat()}",
            created_at=created_at,
        )

    def _schedule_periodic(self, task_type: str, due_at: datetime) -> None:
        entity_id = "standards" if task_type == "SYNC_STANDARD" else "operations"
        self.task_repository.create_or_get(
            task_type=task_type,
            entity_type="RUNTIME",
            entity_id=entity_id,
            due_at=due_at,
            payload={"runtime_worker_id": self.worker_id},
            dedupe_key=f"RUNTIME:{task_type}:{due_at.isoformat()}",
            created_at=self.now_provider(),
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _expected_payload(expected: ExpectedAutomationState) -> dict[str, Any]:
    return {
        "device_id": expected.device_id,
        "alarm_state": expected.alarm_state,
        "operation_state": expected.operation_state,
        "event_exists": expected.event_exists,
        "overall_status": expected.overall_status,
        "standard_id": expected.standard_id,
        "standard_revision": expected.standard_revision,
        "active_event_count": expected.active_event_count,
        "expected_at": _iso(expected.expected_at),
        "applicability": expected.applicability,
        "data_quality": expected.data_quality,
        "temperature_status": expected.temperature_status,
        "humidity_status": expected.humidity_status,
        "active_event_ids": list(expected.active_event_ids),
        "operation_type": expected.operation_type,
    }


def _expected_from_payload(payload: Mapping[str, Any]) -> ExpectedAutomationState:
    expected_at = payload.get("expected_at")
    return ExpectedAutomationState(
        device_id=str(payload["device_id"]),
        alarm_state=str(payload["alarm_state"]),
        operation_state=str(payload["operation_state"]),
        event_exists=bool(payload["event_exists"]),
        overall_status=payload.get("overall_status"),
        standard_id=payload.get("standard_id"),
        standard_revision=payload.get("standard_revision"),
        active_event_count=payload.get("active_event_count"),
        expected_at=(datetime.fromisoformat(expected_at) if expected_at else None),
        applicability=payload.get("applicability"),
        data_quality=payload.get("data_quality"),
        temperature_status=payload.get("temperature_status"),
        humidity_status=payload.get("humidity_status"),
        active_event_ids=tuple(payload.get("active_event_ids") or ()),
        operation_type=payload.get("operation_type"),
    )
