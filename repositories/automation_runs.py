"""SQLite audit sink for disabled, shadow, and active action outcomes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Mapping

from application.action_executor import ActionExecution
from application.shadow import AutomationDiff
from repositories.sqlite import SQLITE_WRITE_LOCK, retry_sqlite_write, run_sqlite_write_with_retry


_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_runs (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    sample_time TEXT,
    mode TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_status TEXT NOT NULL,
    alarm_id TEXT,
    event_id TEXT,
    automation_task_id TEXT,
    dedupe_key TEXT,
    recipient TEXT,
    message_id TEXT,
    result TEXT,
    error_code TEXT,
    sent_at TEXT,
    planned_run_at TEXT,
    python_monitor_result_json TEXT,
    python_alarm_transition_json TEXT,
    feishu_observed_state_json TEXT,
    matched INTEGER,
    difference_type TEXT,
    details_json TEXT,
    context_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    standard_id TEXT,
    standard_revision TEXT,
    standard_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_device_time
    ON automation_runs(device_id, created_at);
CREATE INDEX IF NOT EXISTS idx_automation_runs_diff
    ON automation_runs(matched, difference_type);
CREATE INDEX IF NOT EXISTS idx_automation_runs_type_time
    ON automation_runs(action_type, created_at);
"""


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class SQLiteAutomationRunRepository:
    """Persist action outcomes and optional Python/Feishu comparison data."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._lock = SQLITE_WRITE_LOCK
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.executescript(_SCHEMA)
            columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(automation_runs)"
                ).fetchall()
            }
            for column in (
                "event_id",
                "automation_task_id",
                "dedupe_key",
                "standard_id",
                "standard_revision",
                "standard_source",
                "recipient",
                "message_id",
                "result",
                "error_code",
                "sent_at",
            ):
                if column not in columns:
                    self.connection.execute(
                        f"ALTER TABLE automation_runs ADD COLUMN {column} TEXT"
                    )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_runs_task "
                "ON automation_runs(automation_task_id)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_runs_event "
                "ON automation_runs(event_id)"
            )
            self.connection.commit()

    @retry_sqlite_write
    def record(self, execution: ActionExecution) -> str:
        context = dict(execution.context)
        created_at = execution.created_at or datetime.now().astimezone()
        run_id = uuid.uuid4().hex
        monitor_result = context.get("python_monitor_result")
        if not isinstance(monitor_result, Mapping):
            monitor_result = {}
        transition = context.get("python_alarm_transition")
        if not isinstance(transition, Mapping):
            transition = {}
        event_id = (
            context.get("event_id")
            or execution.action.alarm_id
            or transition.get("active_alarm_id")
        )
        automation_task_id = (
            context.get("automation_task_id")
            or getattr(execution.action, "task_id", None)
        )
        dedupe_key = (
            context.get("dedupe_key")
            or getattr(execution.action, "dedupe_key", None)
        )
        recipient = context.get("recipient")
        message_id = context.get("message_id")
        result = context.get("result") or execution.status.value
        error_code = context.get("error_code")
        sent_at = context.get("sent_at")
        self.connection.execute(
            """
            INSERT INTO automation_runs (
                id, device_id, sample_time, mode, action_type, action_status,
                alarm_id, event_id, automation_task_id, dedupe_key,
                recipient, message_id, result, error_code, sent_at,
                planned_run_at, python_monitor_result_json,
                python_alarm_transition_json, feishu_observed_state_json,
                matched, difference_type, details_json, context_json, error,
                created_at, standard_id, standard_revision, standard_source
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                run_id,
                execution.action.device_id,
                context.get("sample_time"),
                execution.mode.value,
                execution.action.action_type.value,
                execution.status.value,
                event_id,
                event_id,
                automation_task_id,
                dedupe_key,
                recipient,
                message_id,
                result,
                error_code,
                sent_at,
                _time_text(execution.action.run_at),
                _json_text(context.get("python_monitor_result")),
                _json_text(context.get("python_alarm_transition")),
                _json_text(context.get("feishu_observed_state")),
                (
                    int(context["matched"])
                    if context.get("matched") is not None
                    else None
                ),
                context.get("difference_type"),
                _json_text(context.get("details")),
                _json_text(context),
                execution.error,
                _time_text(created_at),
                monitor_result.get("standard_id"),
                monitor_result.get("standard_revision"),
                monitor_result.get("standard_source"),
            ),
        )
        self.connection.commit()
        return run_id

    @retry_sqlite_write
    def record_comparison(
        self,
        *,
        device_id: str,
        sample_time: datetime,
        expected: Mapping[str, Any],
        observed: Mapping[str, Any],
        diff: AutomationDiff,
        created_at: datetime,
    ) -> str:
        """Persist an expected/observed comparison even when no action fired."""
        run_id = uuid.uuid4().hex
        self.connection.execute(
            """
            INSERT INTO automation_runs (
                id, device_id, sample_time, mode, action_type, action_status,
                feishu_observed_state_json, matched, difference_type,
                details_json, context_json, created_at,
                standard_id, standard_revision, standard_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                device_id,
                sample_time.isoformat(),
                "shadow",
                "SHADOW_COMPARE",
                "COMPARED",
                _json_text(dict(observed)),
                int(diff.matched),
                ",".join(diff.difference_type) or None,
                _json_text(dict(diff.details)),
                _json_text({"expected": dict(expected), "observed": dict(observed)}),
                created_at.isoformat(),
                expected.get("standard_id"),
                expected.get("standard_revision"),
                expected.get("standard_source"),
            ),
        )
        self.connection.commit()
        return run_id


def purge_automation_runs(connection: sqlite3.Connection, cutoff: datetime) -> int:
    """Delete comparison/action runs created before ``cutoff``; return count."""

    def delete() -> int:
        cursor = connection.execute(
            "DELETE FROM automation_runs WHERE created_at < ?",
            (cutoff.isoformat(),),
        )
        connection.commit()
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    return run_sqlite_write_with_retry(connection, "purge_automation_runs", delete)
