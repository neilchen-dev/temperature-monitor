"""Continuous Shadow Runtime orchestration.

The runner is an application boundary: it receives normalized samples from the
existing acquisition path, persists Python-owned projections, and queues all
network observation work on the generic durable scheduler.  Feishu writes are
available only through explicitly injected Active-mode handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
import logging
import sqlite3
import threading
from typing import Any, Callable, Mapping

import config
from application.active_scope import active_scope_allows
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
from repositories.automation_runs import purge_automation_runs
from repositories.automation_tasks import (
    SQLiteAutomationTaskRepository,
    purge_finished_automation_tasks,
)
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.runtime_state import SQLiteLatestSampleRepository
from scheduler.worker import TaskScheduler


logger = logging.getLogger("temperature_monitor")

# SHADOW_COMPARE 观察失败时的重试上限：1 次原始执行 + 最多 2 次重试。
# 没有上限时，永久性错误（如设备不在飞书设备表中）会无限生成
# SHADOW_RETRY 任务，automation_tasks 无限膨胀。
_SHADOW_COMPARE_MAX_ATTEMPTS = 3
_PURGE_INTERVAL = timedelta(hours=1)


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
    active_device_ids: tuple[str, ...]
    active_device_count: int
    active_canary_enabled: bool
    active_epoch: str | None
    active_cutover_at: datetime | None
    scheduler_running: bool
    last_standard_sync_time: datetime | None
    enabled_standard_count: int
    active_snapshot_id: str | None
    last_known_good_snapshot_id: str | None
    validated_standard_count: int
    expected_standard_count: int
    latest_sync_status: str | None
    last_sync_attempt_at: datetime | None
    last_successful_sync_at: datetime | None
    standard_source: str | None
    standards_ready: bool
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
            "active_device_ids": list(self.active_device_ids),
            "active_device_count": self.active_device_count,
            "active_canary_enabled": self.active_canary_enabled,
            "active_epoch": self.active_epoch,
            "active_cutover_at": _iso(self.active_cutover_at),
            "scheduler_running": self.scheduler_running,
            "last_standard_sync_time": _iso(self.last_standard_sync_time),
            "enabled_standard_count": self.enabled_standard_count,
            "active_snapshot_id": self.active_snapshot_id,
            "last_known_good_snapshot_id": self.last_known_good_snapshot_id,
            "validated_standard_count": self.validated_standard_count,
            "expected_standard_count": self.expected_standard_count,
            "latest_sync_status": self.latest_sync_status,
            "last_sync_attempt_at": _iso(self.last_sync_attempt_at),
            "last_successful_sync_at": _iso(self.last_successful_sync_at),
            "standard_source": self.standard_source,
            "standards_ready": self.standards_ready,
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
        active_device_ids: tuple[str, ...],
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
        self.active_device_ids = tuple(active_device_ids)
        self.active_canary_enabled = bool(
            self.mode == "active"
            and self.feishu_write_enabled
            and self.active_device_ids
        )
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
        self._skipped_device_log: set[str] = set()
        self._last_purge_time: datetime | None = None
        self._last_operation_sync_time: datetime | None = None
        self._last_expected_state: dict[str, ExpectedAutomationState] = {}

    def standards_readiness(self) -> dict[str, Any]:
        """Return repository-backed readiness; any read error fails closed."""
        try:
            return self.standard_repository.readiness(
                expected_device_ids=self.devices.keys()
            )
        except Exception:  # noqa: BLE001 - observability must not enable writes
            logger.exception("读取 standards readiness 失败，按未就绪处理")
            return {
                "active_snapshot_id": None,
                "last_known_good_snapshot_id": None,
                "validated_standard_count": 0,
                "validated_device_count": 0,
                "expected_standard_count": len(self.devices),
                "expected_device_count": len(self.devices),
                "latest_sync_status": "UNAVAILABLE",
                "last_sync_attempt_at": None,
                "last_successful_sync_at": None,
                "standard_source": None,
                "standards_ready": False,
                "active_snapshot_status": None,
                "last_known_good_snapshot_status": None,
            }

    def _standards_ready(self) -> bool:
        return bool(self.standards_readiness()["standards_ready"])

    @staticmethod
    def _readiness_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

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
                self._ensure_periodic_tasks(now=now, immediate=True)
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
            #
            # Lock-order invariant (2026-09-02 AB-BA deadlock fix): this
            # thread is the ONLY code path that dispatches HA projections
            # into the Runtime (via _ensure_projection_tasks ->
            # recover_pending_dispatches). HTTP threads never dispatch, so
            # _execution_lock is the only runtime lock in the system and can
            # never be acquired in opposite orders by two threads.
            while not stop_event.is_set():
                with self._execution_lock:
                    self._maybe_purge()
                    try:
                        self._ensure_event_reconciliation_tasks(
                            now=self.now_provider()
                        )
                        self.scheduler.run_once()
                    except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                        # run_once 已捕获 handler 异常；走到这里的是
                        # claim/commit/lease 等基础层错误。以前会直接炸掉
                        # 整个调度线程（VERIFY_ALARM/RECOVERY、SYNC、COMPARE
                        # 全部停摆且无告警），现在记录后继续下一个 tick。
                        logger.exception(
                            "Shadow scheduler tick failed; continuing next poll"
                        )
                    finally:
                        # Handlers return while their task is still RUNNING;
                        # run_once marks it terminal before this point.  Only
                        # then may the next recurring cycle be created.
                        self._ensure_periodic_tasks(now=self.now_provider())
                        self._ensure_projection_tasks(now=self.now_provider())
                if stop_event.wait(self.scheduler.poll_interval):
                    return
        finally:
            with self._lifecycle_lock:
                if self._closed:
                    self._scheduler_thread = None
                    self._close_connection()

    def _maybe_purge(self) -> None:
        """Bound automation_runs/automation_tasks growth (hourly, best-effort)."""
        retention_days = config.AUTOMATION_RUN_RETENTION_DAYS
        if retention_days <= 0:
            return
        now = self.now_provider()
        if (
            self._last_purge_time is not None
            and now - self._last_purge_time < _PURGE_INTERVAL
        ):
            return
        self._last_purge_time = now
        cutoff = now - timedelta(days=retention_days)
        try:
            purged_runs = purge_automation_runs(self.connection, cutoff)
            purged_tasks = purge_finished_automation_tasks(self.connection, cutoff)
            if purged_runs or purged_tasks:
                logger.info(
                    "automation history purged | runs=%s | tasks=%s | retention_days=%s",
                    purged_runs,
                    purged_tasks,
                    retention_days,
                )
        except sqlite3.Error:
            logger.exception("automation history purge failed; retry next hour")

    def _close_connection(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            logger.exception("关闭 Shadow Runtime SQLite 连接失败")

    def handle_sample(self, sample: MonitorSample) -> MonitorHandlingResult | None:
        """Route one normalized acquisition sample into the Shadow pipeline."""
        device_id = sample.device_id.strip().upper()
        if not self._accepting_samples:
            return None
        if device_id not in self.devices:
            # 这里是"只有 TH-10 有 SHADOW_COMPARE"的直接根因路径：
            # 白名单外的设备直接丢弃。每台设备只提示一次，日志包含
            # device_id、reason 与当前已配置设备，便于线上排查盲区。
            if device_id not in self._skipped_device_log:
                self._skipped_device_log.add(device_id)
                logger.info(
                    "sample ignored | device=%s | reason=not_configured_in_shadow_whitelist "
                    "| configured_devices=%s | remediation=add device to SHADOW_DEVICE_IDS",
                    device_id,
                    ",".join(sorted(self.devices)) or "none",
                )
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
                standard_source=result.monitor_result.standard_source,
                active_event_count=len(active_events),
                active_event_ids=tuple(event.event_id for event in active_events),
                expected_at=self.now_provider(),
                applicability=result.monitor_result.applicability.value,
                data_quality=result.monitor_result.data_quality.value,
                temperature_status=result.monitor_result.temperature_status.value,
                humidity_status=result.monitor_result.humidity_status.value,
                operation_type=result.operation_state.operation_type,
                resolved_control_type=(
                    result.monitor_result.resolved_control_type.value
                    if result.monitor_result.resolved_control_type is not None
                    else None
                ),
                control_type_source=result.monitor_result.control_type_source,
                control_type_consistency=result.monitor_result.control_type_consistency,
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
            readiness = self.standards_readiness()
            standards_ready = bool(readiness["standards_ready"])
            runtime_context = getattr(self.task_repository, "runtime_context", None)
            activation = runtime_context() if callable(runtime_context) else None
            status = RuntimeStatus(
                mode=self.mode,
                available=self.available,
                degraded=not self.available,
                reason=self.unavailable_reason,
                worker_id=self.worker_id,
                feishu_readonly_available=self.feishu_readonly_available,
                feishu_write_enabled=self.feishu_write_enabled,
                configured_shadow_devices=tuple(self.devices),
                active_device_ids=self.active_device_ids,
                active_device_count=len(self.active_device_ids),
                active_canary_enabled=(
                    self.active_canary_enabled and standards_ready
                ),
                active_epoch=getattr(activation, "active_epoch", None),
                active_cutover_at=getattr(activation, "active_cutover_at", None),
                scheduler_running=scheduler_running,
                last_standard_sync_time=self._last_standard_sync_time,
                enabled_standard_count=self._enabled_standard_count,
                active_snapshot_id=readiness["active_snapshot_id"],
                last_known_good_snapshot_id=readiness[
                    "last_known_good_snapshot_id"
                ],
                validated_standard_count=readiness["validated_standard_count"],
                expected_standard_count=readiness["expected_standard_count"],
                latest_sync_status=readiness["latest_sync_status"],
                last_sync_attempt_at=self._readiness_datetime(
                    readiness["last_sync_attempt_at"]
                ),
                last_successful_sync_at=self._readiness_datetime(
                    readiness["last_successful_sync_at"]
                ),
                standard_source=readiness["standard_source"],
                standards_ready=standards_ready,
                last_processed_sample_time=self._last_processed_sample_time,
                last_shadow_compare_time=self._last_shadow_compare_time,
                last_shadow_diff=self._last_shadow_diff,
            ).as_dict()
            status["scheduler"] = {"running": scheduler_running}
            return status

    def shadow_summary(self, *, hours: int = 24, now: datetime | None = None) -> dict[str, Any]:
        """Aggregate recent SHADOW_COMPARE outcomes for production checks.

        Read-only view over automation_runs; never mutates business state.
        """
        current = now or self.now_provider()
        cutoff_text = (current - timedelta(hours=hours)).isoformat()
        rows = self.connection.execute(
            """
            SELECT device_id, matched, difference_type, context_json, created_at
            FROM automation_runs
            WHERE action_type = 'SHADOW_COMPARE' AND created_at >= ?
            """,
            (cutoff_text,),
        ).fetchall()
        total = len(rows)
        matched = sum(1 for row in rows if row["matched"] == 1)
        observation_errors = sum(
            1 for row in rows if row["difference_type"] == "OBSERVATION_ERROR"
        )
        mismatch = total - matched - observation_errors
        by_difference_type: dict[str, int] = {}
        by_device: dict[str, dict[str, int]] = {}
        control_type_diagnostics = {
            "standard_equals_legacy": 0,
            "standard_differs_legacy": 0,
            "standard_missing": 0,
            "legacy_missing": 0,
            "configuration_error": 0,
        }
        control_type_mismatch_devices: set[str] = set()
        last_compare_time: str | None = None
        for row in rows:
            key = row["difference_type"] or "MATCH"
            by_difference_type[key] = by_difference_type.get(key, 0) + 1
            device_stats = by_device.setdefault(
                row["device_id"],
                {"total": 0, "matched": 0, "mismatch": 0, "observation_error": 0},
            )
            device_stats["total"] += 1
            if row["matched"] == 1:
                device_stats["matched"] += 1
            elif row["difference_type"] == "OBSERVATION_ERROR":
                device_stats["observation_error"] += 1
            else:
                device_stats["mismatch"] += 1
            try:
                expected_context = json.loads(row["context_json"]).get("expected", {})
            except (TypeError, ValueError):
                expected_context = {}
            consistency = expected_context.get("control_type_consistency")
            source = expected_context.get("control_type_source")
            diagnostic_key = {
                "match": "standard_equals_legacy",
                "mismatch": "standard_differs_legacy",
                "standard_missing": "standard_missing",
                "legacy_missing": "legacy_missing",
            }.get(consistency)
            if diagnostic_key is not None:
                control_type_diagnostics[diagnostic_key] += 1
            if source == "configuration_error":
                control_type_diagnostics["configuration_error"] += 1
            if consistency == "mismatch":
                control_type_mismatch_devices.add(row["device_id"])
            if last_compare_time is None or row["created_at"] > last_compare_time:
                last_compare_time = row["created_at"]
        standard_age = (
            (current - self._last_standard_sync_time).total_seconds()
            if self._last_standard_sync_time is not None
            else None
        )
        operation_age = (
            (current - self._last_operation_sync_time).total_seconds()
            if self._last_operation_sync_time is not None
            else None
        )
        return {
            "hours": hours,
            "total_compare": total,
            "matched": matched,
            "mismatch": mismatch,
            "observation_error_count": observation_errors,
            "match_rate": round(matched / total, 4) if total else None,
            "by_difference_type": by_difference_type,
            "by_device": by_device,
            "control_type_diagnostics": control_type_diagnostics,
            "control_type_mismatch_devices": sorted(control_type_mismatch_devices),
            "last_compare_time": last_compare_time,
            "devices_with_no_compare": sorted(set(self.devices) - set(by_device)),
            "scheduler_running": bool(
                self._scheduler_thread is not None
                and self._scheduler_thread.is_alive()
            ),
            "standard_sync_age_seconds": standard_age,
            "operation_sync_age_seconds": operation_age,
        }

    # The following methods are handlers for TaskScheduler.  They deliberately
    # contain orchestration only; business decisions stay in existing services.
    def handle_verify_alarm(self, task: Any) -> None:
        self._handle_verification_task(task)

    def handle_verify_recovery(self, task: Any) -> None:
        self._handle_verification_task(task)

    def handle_reconcile_alarm_event(self, task: Any) -> None:
        """Run one durable Active Feishu alarm-event reconciliation attempt."""
        device_id = str(task.entity_id).strip().upper()
        device = self.devices.get(device_id)
        if device is None:
            raise RuntimeError(
                f"alarm event reconciliation device is not configured: {device_id}"
            )
        with self._execution_lock:
            self.monitor_service.reconcile_alarm_event_task(
                task=task,
                device=device,
                now=self.now_provider(),
            )

    def handle_notification_task(self, task: Any) -> None:
        """Run one independently retryable Feishu IM notification task."""
        with self._execution_lock:
            self.monitor_service.execute_notification_task(
                task=task,
                now=self.now_provider(),
            )

    def handle_feishu_projection(self, task: Any) -> None:
        """Retry a deferred Feishu projection for one device.

        Business logic lives in ``services.projection``: project the latest
        persisted sample and advance the projected watermark. The dispatch
        belongs to the same tick's ``recover_pending_dispatches`` hook
        (single dispatch owner — see the 2026-09-02 deadlock fix notes in
        ``services/projection.py``). Raises on failure so the task is
        recorded FAILED (visible evidence) while the pending state drives
        the next backoff retry.
        """
        from services import projection

        projection.retry_device_projection(
            str(task.entity_id), now=self.now_provider()
        )

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
                    "shadow sample %s %s -> %s | alarm_lifecycle_owner=%s "
                    "| standard_projection_differences=%s",
                    expected.device_id,
                    expected.alarm_state,
                    self._last_shadow_diff,
                    "python" if self.active_canary_enabled and self._standards_ready() and active_scope_allows(
                        expected.device_id, active_device_ids=self.active_device_ids
                    ) else "feishu",
                    ",".join(value for value in diff.difference_type if value not in {
                        "ALARM_STATE_MISMATCH", "EVENT_STATE_MISMATCH", "EVENT_MISSING", "EVENT_DUPLICATED"
                    }) or "none",
                )
        except Exception as exc:
            # Keep a durable retry task for temporary read failures while the
            # original task records its failed attempt and error context.
            # 注意：重试是"新任务"，attempt_count 会归零，所以累计次数必须
            # 放在 payload 里传递，否则上限永远不会触发。
            compare_attempt = int(task.payload.get("compare_attempt", 0)) + 1
            if compare_attempt < _SHADOW_COMPARE_MAX_ATTEMPTS:
                retry_at = self.now_provider() + timedelta(seconds=30)
                retry_payload = dict(task.payload)
                retry_payload["compare_attempt"] = compare_attempt
                self.task_repository.create_or_get(
                    task_type="SHADOW_COMPARE",
                    entity_type="DEVICE",
                    entity_id=expected.device_id,
                    due_at=retry_at,
                    payload=retry_payload,
                    dedupe_key=f"SHADOW_RETRY:{task.task_id}:{compare_attempt}",
                    created_at=self.now_provider(),
                )
            # 失败的比对以前不会出现在 automation_runs（run 行只在成功路径
            # 里记录），导致"设备没有 SHADOW_COMPARE"无法与"比对一直失败"
            # 区分开。这里把失败也落一条 run，供线上排查。
            try:
                self.shadow_comparison.record_failure(
                    device_id=expected.device_id,
                    sample_time=datetime.fromisoformat(task.payload["sample_time"]),
                    expected=_expected_payload(expected),
                    error=f"{type(exc).__name__}: {exc}",
                    created_at=self.now_provider(),
                )
            except Exception:  # noqa: BLE001 - observability must not mask the cause
                logger.exception("failed to record shadow compare failure run")
            raise

    def handle_standard_sync(self, task: Any) -> None:
        with self._execution_lock:
            sync_time = self.now_provider()
            report = self.standard_sync.sync(now=sync_time)
            self._last_standard_sync_time = sync_time
            # Read the cache after both success and failure so status keeps
            # reporting the previous active version when a remote sync fails.
            self._enabled_standard_count = sum(
                1 for standard in self.standard_repository.list_active() if standard.enabled
            )
            if report.errors:
                logger.error(
                    "Shadow 标准同步失败，保留上一版标准 | errors=%s",
                    "; ".join(report.errors),
                )
            else:
                logger.info("Shadow 标准同步完成 | enabled=%s", self._enabled_standard_count)

    def trigger_standard_sync(self, *, now: datetime | None = None) -> Any:
        """Queue an immediate sync; designed as the future Feishu event hook.

        Periodic scheduling remains the safety net.  A webhook/event adapter
        can call this method without knowing the scheduler or SQLite details.
        """
        current_time = now or self.now_provider()
        with self._execution_lock:
            return self.task_repository.create_or_get_unfinished(
                task_type="SYNC_STANDARD",
                entity_type="RUNTIME",
                entity_id="standards",
                due_at=current_time,
                payload={
                    "runtime_worker_id": self.worker_id,
                    "trigger": "external_event",
                },
                dedupe_key="RUNTIME:SYNC_STANDARD:standards",
                created_at=current_time,
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
                self._last_operation_sync_time = self.now_provider()
            except Exception:
                logger.exception("Shadow 作业状态同步失败，保留上一版状态")

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
                standard_source=result.monitor_result.standard_source,
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
        previous = self._last_expected_state.get(expected.device_id)
        if previous is not None:
            expected = replace(
                expected,
                previous_state=_expected_comparison_values(previous),
            )
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
        self._last_expected_state[expected.device_id] = expected

    def _schedule_periodic(self, task_type: str, due_at: datetime) -> None:
        entity_id = "standards" if task_type == "SYNC_STANDARD" else "operations"
        self.task_repository.create_or_get_unfinished(
            task_type=task_type,
            entity_type="RUNTIME",
            entity_id=entity_id,
            due_at=due_at,
            payload={"runtime_worker_id": self.worker_id},
            dedupe_key=f"RUNTIME:{task_type}:{entity_id}",
            created_at=self.now_provider(),
        )

    def _ensure_periodic_tasks(self, *, now: datetime, immediate: bool = False) -> None:
        self._schedule_periodic(
            "SYNC_STANDARD",
            now if immediate else now + timedelta(seconds=self.standard_sync_interval),
        )
        self._schedule_periodic(
            "SYNC_OPERATIONS",
            now if immediate else now + timedelta(seconds=self.operation_sync_interval),
        )

    def _ensure_event_reconciliation_tasks(self, *, now: datetime) -> None:
        """Re-arm unbound Feishu CREATEs independently of alarm lifecycle state."""
        standards_ready = getattr(self, "_standards_ready", lambda: True)
        if not self.active_canary_enabled or not standards_ready():
            return
        for event in self.event_repository.list_pending_external_bindings():
            device_id = event.device_id.strip().upper()
            if device_id not in self.devices or not active_scope_allows(
                device_id,
                active_device_ids=self.active_device_ids,
            ):
                continue
            payload = dict(event.payload)
            event_effect_allowed = getattr(
                self.task_repository, "event_external_effect_allowed", None
            )
            if callable(event_effect_allowed) and not event_effect_allowed(
                created_mode=payload.get("created_mode"),
                active_epoch=payload.get("active_epoch"),
            ):
                # A pre-cutover event may remain locally useful for audit and
                # manual reconciliation, but it is never upgraded to an
                # Active CREATE/UPDATE/NOTIFY merely because the process mode
                # changed.  Changing the binding marker keeps the scanner from
                # repeatedly re-arming the same historical event.
                self.event_repository.patch_external_projection(
                    event.event_id,
                    feishu_binding_status="LEGACY_PENDING",
                    external_effect_policy="SHADOW_ONLY",
                    external_effect_blocked_at=now.isoformat(),
                    external_effect_block_reason="created_before_active_cutover",
                )
                logger.warning(
                    "historical event held for manual review at Active cutover | "
                    "device_id=%s | event_id=%s",
                    device_id,
                    event.event_id,
                )
                continue
            if not payload.get("feishu_record_id") and "feishu_create_attempted" not in payload:
                self.event_repository.patch_external_projection(
                    event.event_id, feishu_create_attempted=True
                )
            if not payload.get("feishu_record_id") and payload.get("feishu_binding_status") != "PENDING":
                # Pre-marker CREATE audit is evidence of a possibly accepted
                # POST, never permission to recreate a manually deleted row.
                self.event_repository.patch_external_projection(
                    event.event_id, feishu_create_attempted=True
                )
                self.event_repository.mark_external_binding_pending(
                    event.event_id,
                    requested_at=now,
                )
            started_at = _parse_event_start(payload.get("violation_started_at"))
            if started_at is None:
                started_at = _parse_event_start(event.event_key)
            device = self.devices[device_id]
            task_payload = {
                "device_id": device_id,
                "local_event_id": event.event_id,
                # Older local events did not project ``area`` into payload_json.
                # The configured device context is the authoritative fallback
                # needed to recreate those events after a response loss.
                "area": str(payload.get("area") or getattr(device, "area", "") or "").strip(),
                "sample_time": str(payload.get("sample_time") or event.opened_at.isoformat()),
                "temperature": payload.get("temperature"),
                "humidity": payload.get("humidity"),
                "online_status": payload.get("online_status"),
                "data_quality": payload.get("data_quality"),
                "temperature_status": payload.get("temperature_status") or "",
                "humidity_status": payload.get("humidity_status") or "",
                "violation_started_at": started_at.isoformat() if started_at is not None else None,
                "alarm_started_at": payload.get("alarm_started_at"),
                "retry_attempt": int(payload.get("retry_attempt", 0) or 0),
            }
            self.task_repository.create_or_get_unfinished(
                task_type="RECONCILE_ALARM_EVENT",
                entity_type="DEVICE",
                entity_id=device_id,
                due_at=now,
                payload=task_payload,
                dedupe_key=(
                    f"RECONCILE_ALARM_EVENT:{event.event_id}"
                ),
                created_at=now,
            )

    def _ensure_projection_tasks(self, *, now: datetime) -> None:
        """Per-tick projection maintenance — the dispatch single owner.

        1. ``recover_pending_dispatches`` dispatches every sample with
           projected > dispatched: both the normal flow (HTTP projection
           succeeded; dispatch is deferred to here by design) and crash
           recovery. This scheduler thread is the ONLY dispatch owner for
           HA projections → Runtime, which removed the 2026-09-02 AB-BA
           deadlock (HTTP threads no longer touch any runtime lock).
        2. ``ensure_projection_tasks`` (re)creates durable, staggered
           FEISHU_PROJECTION retry tasks for pending devices.

        Business logic lives in ``services.projection`` (mirror-DB state
        machine); this keeps the runtime a thin orchestrator. Both are
        best-effort: failures are logged and retried next tick.
        """
        from services import projection

        projection.recover_pending_dispatches(now=now)
        projection.ensure_projection_tasks(self.task_repository, now=now)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_event_start(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("ENV:"):
        text = text.split(":", 2)[-1]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _expected_payload(expected: ExpectedAutomationState) -> dict[str, Any]:
    return {
        "device_id": expected.device_id,
        "alarm_state": expected.alarm_state,
        "operation_state": expected.operation_state,
        "event_exists": expected.event_exists,
        "overall_status": expected.overall_status,
        "standard_id": expected.standard_id,
        "standard_revision": expected.standard_revision,
        "standard_source": expected.standard_source,
        "active_event_count": expected.active_event_count,
        "expected_at": _iso(expected.expected_at),
        "applicability": expected.applicability,
        "data_quality": expected.data_quality,
        "temperature_status": expected.temperature_status,
        "humidity_status": expected.humidity_status,
        "active_event_ids": list(expected.active_event_ids),
        "operation_type": expected.operation_type,
        "resolved_control_type": expected.resolved_control_type,
        "control_type_source": expected.control_type_source,
        "control_type_consistency": expected.control_type_consistency,
        "previous_state": (
            dict(expected.previous_state) if expected.previous_state is not None else None
        ),
    }


def _expected_comparison_values(expected: ExpectedAutomationState) -> dict[str, Any]:
    return {
        "alarm_state": expected.alarm_state,
        "operation_state": expected.operation_state,
        "event_exists": expected.event_exists,
        "overall_status": expected.overall_status,
        "standard_id": expected.standard_id,
        "standard_revision": expected.standard_revision,
        "standard_source": expected.standard_source,
        "applicability": expected.applicability,
        "data_quality": expected.data_quality,
        "temperature_status": expected.temperature_status,
        "humidity_status": expected.humidity_status,
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
        standard_source=payload.get("standard_source"),
        active_event_count=payload.get("active_event_count"),
        expected_at=(datetime.fromisoformat(expected_at) if expected_at else None),
        applicability=payload.get("applicability"),
        data_quality=payload.get("data_quality"),
        temperature_status=payload.get("temperature_status"),
        humidity_status=payload.get("humidity_status"),
        active_event_ids=tuple(payload.get("active_event_ids") or ()),
        operation_type=payload.get("operation_type"),
        resolved_control_type=payload.get("resolved_control_type"),
        control_type_source=payload.get("control_type_source"),
        control_type_consistency=payload.get("control_type_consistency"),
        previous_state=payload.get("previous_state"),
    )
