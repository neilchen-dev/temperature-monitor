"""SQLite audit sink for disabled, shadow, and active action outcomes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Mapping

from application.action_executor import ActionExecution
from application.shadow import AutomationDiff


_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_runs (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    sample_time TEXT,
    mode TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_status TEXT NOT NULL,
    alarm_id TEXT,
    planned_run_at TEXT,
    python_monitor_result_json TEXT,
    python_alarm_transition_json TEXT,
    feishu_observed_state_json TEXT,
    matched INTEGER,
    difference_type TEXT,
    details_json TEXT,
    context_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_device_time
    ON automation_runs(device_id, created_at);
CREATE INDEX IF NOT EXISTS idx_automation_runs_diff
    ON automation_runs(matched, difference_type);
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
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def record(self, execution: ActionExecution) -> str:
        context = dict(execution.context)
        created_at = execution.created_at or datetime.now().astimezone()
        run_id = uuid.uuid4().hex
        self.connection.execute(
            """
            INSERT INTO automation_runs (
                id, device_id, sample_time, mode, action_type, action_status,
                alarm_id, planned_run_at, python_monitor_result_json,
                python_alarm_transition_json, feishu_observed_state_json,
                matched, difference_type, details_json, context_json, error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                execution.action.device_id,
                context.get("sample_time"),
                execution.mode.value,
                execution.action.action_type.value,
                execution.status.value,
                execution.action.alarm_id,
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
            ),
        )
        self.connection.commit()
        return run_id

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
                details_json, context_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        self.connection.commit()
        return run_id
