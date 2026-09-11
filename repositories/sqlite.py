"""Small SQLite connection factory for application-owned repositories."""

from __future__ import annotations

from functools import wraps
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar


logger = logging.getLogger("temperature_monitor")

# Runtime and the legacy/projection mirror intentionally use separate SQLite
# connections.  SQLite serializes writers per database file, so a process-wide
# re-entrant lock is the smallest coordination boundary for this deployment
# model.  It covers only local SQLite work; callers must perform network I/O
# before/after repository methods, never while this lock is held.
SQLITE_WRITE_LOCK = threading.RLock()
_SQLITE_LOCK_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2)
_T = TypeVar("_T")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a repository connection; callers own its lifecycle."""
    path_value = str(path)
    if path_value != ":memory:":
        Path(path_value).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        path_value,
        check_same_thread=False,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row

    def configure() -> None:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        if path_value != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")

    run_sqlite_write_with_retry(connection, "connection.configure", configure)
    return connection


def is_sqlite_lock_error(error: BaseException) -> bool:
    """Return whether an SQLite error represents a bounded lock conflict."""
    return isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower()


def run_sqlite_write_with_retry(
    connection: sqlite3.Connection,
    operation: str,
    callback: Callable[[], _T],
) -> _T:
    """Run one local write operation with short, bounded lock retries.

    The callback must contain only SQLite work.  A failed operation-owned
    transaction is rolled back before the next attempt so the connection never
    carries a partial transaction forward.  Non-lock errors and an exhausted
    retry budget are propagated to the caller for normal error handling.
    """
    max_attempts = len(_SQLITE_LOCK_RETRY_DELAYS) + 1
    caller_transaction_open = connection.in_transaction
    for attempt in range(max_attempts):
        retry = False
        with SQLITE_WRITE_LOCK:
            try:
                return callback()
            except sqlite3.OperationalError as error:
                if not is_sqlite_lock_error(error) or attempt >= max_attempts - 1:
                    raise
                retry = True
                # A repository may be called inside a caller-owned savepoint
                # (standard snapshot code supports this).  Only roll back a
                # transaction opened by this operation; the callback owns the
                # savepoint cleanup for an already-open outer transaction.
                if not caller_transaction_open:
                    try:
                        if connection.in_transaction:
                            connection.rollback()
                    except sqlite3.Error:
                        logger.warning(
                            "SQLite lock retry rollback failed | operation=%s",
                            operation,
                            exc_info=True,
                        )
        if retry:
            delay = _SQLITE_LOCK_RETRY_DELAYS[attempt]
            logger.warning(
                "SQLite write busy; retrying bounded operation | operation=%s "
                "| attempt=%s/%s | backoff_seconds=%.3f",
                operation,
                attempt + 1,
                max_attempts,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable SQLite retry state")


def retry_sqlite_write(method: Callable[..., _T]) -> Callable[..., _T]:
    """Retry a repository write method without holding a lock across callers."""
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> _T:
        connection = getattr(self, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("retry_sqlite_write requires self.connection")
        return run_sqlite_write_with_retry(
            connection,
            f"{type(self).__name__}.{method.__name__}",
            lambda: method(self, *args, **kwargs),
        )

    return wrapped


# Runtime 所需的每张表的关键列。各仓储 __init__ 的 CREATE TABLE IF NOT
# EXISTS / 增量迁移执行完之后，如果这里仍有缺列，说明数据库是半旧 schema
# 且没有对应迁移路径——必须显式报错，禁止静默运行。
_EXPECTED_SCHEMA_COLUMNS: dict[str, tuple[str, ...]] = {
    "automation_tasks": (
        "id", "task_type", "entity_type", "entity_id", "due_at", "status",
        "payload_json", "dedupe_key", "created_at", "updated_at", "started_at",
        "finished_at", "claimed_at", "lease_until", "worker_id",
        "attempt_count", "last_error", "created_mode", "active_epoch",
    ),
    "automation_runs": (
        "id", "device_id", "sample_time", "mode", "action_type", "action_status",
        "alarm_id", "event_id", "automation_task_id", "dedupe_key",
        "recipient", "message_id", "result", "error_code", "sent_at",
        "planned_run_at", "python_monitor_result_json",
        "python_alarm_transition_json", "feishu_observed_state_json", "matched",
        "difference_type", "details_json", "context_json", "error", "created_at",
        "standard_id", "standard_revision", "standard_source",
    ),
    "environment_events": (
        "event_id", "device_id", "event_key", "status", "opened_at",
        "closed_at", "payload_json", "external_create_owner",
        "external_create_lease_until",
    ),
    "standard_versions": (
        "standard_id", "revision", "area", "device_id", "operation_type",
        "control_type",
        "temperature_min", "temperature_max", "humidity_min", "humidity_max",
        "effective_from", "effective_to", "source_document", "clause",
        "priority", "enabled", "created_at", "updated_at",
        "standard_source", "validation_status", "validated_at",
    ),
    "standard_sync_runs": (
        "id", "source", "status", "standard_count", "errors_json",
        "started_at", "finished_at", "snapshot_id",
    ),
    "standard_snapshots": (
        "snapshot_id", "source", "synced_at", "standard_count", "status",
        "created_at",
    ),
    "standard_snapshot_members": (
        "snapshot_id", "standard_id", "revision",
    ),
    "standard_runtime_state": (
        "singleton_id", "active_snapshot_id", "last_known_good_snapshot_id",
    ),
    "alarm_states": (
        "device_id", "state", "violation_started_at", "alarm_started_at",
        "recovery_started_at", "active_alarm_id", "pending_task_id",
        "prewarning_active", "prewarning_started_at", "prewarning_episode_id",
        "prewarning_reasons_json", "prewarning_details_json",
        "prewarning_standard_id", "prewarning_standard_revision",
        "prewarning_notify_task_id", "prewarning_message_id",
        "prewarning_recovered_at", "prewarning_recovery_task_id",
        "prewarning_recovery_message_id", "updated_at",
    ),
    "prewarning_external_effects": (
        "effect_key", "device_id", "action_type", "status", "requested_at",
        "completed_at", "failed_at", "recipient", "receive_id_type",
        "message_id", "error", "metadata_json",
    ),
    "latest_monitor_samples": (
        "device_id", "sample_time", "temperature", "humidity",
        "online_status", "data_quality", "record_type", "measurement_time",
        "heartbeat_time", "availability",
    ),
    "operation_observations_current": (
        "device_id", "area_id", "action", "operation_type", "work_order",
        "source_record_id", "source_created_at", "observed_at",
    ),
    "operation_states": (
        "device_id", "area_id", "state", "operation_type", "work_order",
        "started_at", "ended_at", "updated_at",
    ),
    "operation_observation_audit": (
        "id", "device_id", "source_record_id", "source_created_at",
        "observed_at", "action", "accepted", "reason", "created_at",
    ),
    "device_status_projection": (
        "device_id", "record_id", "desired_hash", "desired_fields_json",
        "observed_fields_json", "changed_fields_json", "status", "pending", "failed",
        "last_success_at", "last_attempt_at", "last_error", "last_task_id",
        "updated_at",
    ),
    "automation_runtime_state": (
        "singleton_id", "current_mode", "active_epoch", "active_cutover_at",
        "updated_at",
    ),
}


def verify_runtime_schema(connection: sqlite3.Connection) -> list[str]:
    """Return a list of ``table.column`` entries missing from the schema.

    Empty list = schema complete. Only called after repositories ran their
    CREATE TABLE IF NOT EXISTS / additive migrations, so any missing column
    here has no migration path and must block runtime startup.
    """
    missing: list[str] = []
    for table, columns in _EXPECTED_SCHEMA_COLUMNS.items():
        try:
            present = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
        except sqlite3.Error:
            missing.append(f"{table}: table missing")
            continue
        for column in columns:
            if column not in present:
                missing.append(f"{table}.{column}")
    return missing
