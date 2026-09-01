"""Small SQLite connection factory for application-owned repositories."""

from __future__ import annotations

import sqlite3
from pathlib import Path


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
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    if path_value != ":memory:":
        connection.execute("PRAGMA journal_mode=WAL")
    return connection


# Runtime 所需的每张表的关键列。各仓储 __init__ 的 CREATE TABLE IF NOT
# EXISTS / 增量迁移执行完之后，如果这里仍有缺列，说明数据库是半旧 schema
# 且没有对应迁移路径——必须显式报错，禁止静默运行。
_EXPECTED_SCHEMA_COLUMNS: dict[str, tuple[str, ...]] = {
    "automation_tasks": (
        "id", "task_type", "entity_type", "entity_id", "due_at", "status",
        "payload_json", "dedupe_key", "created_at", "updated_at", "started_at",
        "finished_at", "claimed_at", "lease_until", "worker_id",
        "attempt_count", "last_error",
    ),
    "automation_runs": (
        "id", "device_id", "sample_time", "mode", "action_type", "action_status",
        "alarm_id", "planned_run_at", "python_monitor_result_json",
        "python_alarm_transition_json", "feishu_observed_state_json", "matched",
        "difference_type", "details_json", "context_json", "error", "created_at",
    ),
    "environment_events": (
        "event_id", "device_id", "event_key", "status", "opened_at",
        "closed_at", "payload_json",
    ),
    "standard_versions": (
        "standard_id", "revision", "area", "device_id", "operation_type",
        "temperature_min", "temperature_max", "humidity_min", "humidity_max",
        "effective_from", "effective_to", "source_document", "clause",
        "priority", "enabled", "created_at", "updated_at",
    ),
    "standard_sync_runs": (
        "id", "source", "status", "standard_count", "errors_json",
        "started_at", "finished_at",
    ),
    "alarm_states": (
        "device_id", "state", "violation_started_at", "alarm_started_at",
        "recovery_started_at", "active_alarm_id", "pending_task_id", "updated_at",
    ),
    "latest_monitor_samples": (
        "device_id", "sample_time", "temperature", "humidity",
        "online_status", "data_quality",
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
