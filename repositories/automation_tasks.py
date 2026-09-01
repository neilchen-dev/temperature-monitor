"""Durable task repository for delayed and periodic automation actions."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

from domain.models import AutomationTask, AutomationTaskStatus


_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    claimed_at TEXT,
    lease_until TEXT,
    worker_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_tasks_due
    ON automation_tasks(status, due_at);
CREATE INDEX IF NOT EXISTS idx_automation_tasks_entity
    ON automation_tasks(entity_type, entity_id, status);
"""


class TaskStateError(ValueError):
    """The requested task status transition is not valid."""


def _datetime_text(value: datetime) -> str:
    return value.isoformat()


def _datetime_value(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class SQLiteAutomationTaskRepository:
    """SQLite-backed task store with durable deduplication and claiming."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._lock = threading.RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)
        self._apply_migrations()
        self.connection.commit()

    def _apply_migrations(self) -> None:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(automation_tasks)")
        }
        for column, definition in (
            ("claimed_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("worker_id", "TEXT"),
        ):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE automation_tasks ADD COLUMN {column} {definition}"
                )

    def create_or_get(
        self,
        *,
        task_type: str,
        entity_type: str,
        entity_id: str,
        due_at: datetime,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: str | None = None,
        created_at: datetime,
    ) -> AutomationTask:
        """Create a pending task, or return the existing deduplicated task."""
        with self._lock:
            task_id = uuid.uuid4().hex
            payload_json = json.dumps(
                dict(payload or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            values = (
                task_id,
                task_type,
                entity_type,
                entity_id,
                _datetime_text(due_at),
                AutomationTaskStatus.PENDING.value,
                payload_json,
                dedupe_key,
                _datetime_text(created_at),
                _datetime_text(created_at),
            )
            if dedupe_key is None:
                self.connection.execute(
                    """
                    INSERT INTO automation_tasks (
                        id, task_type, entity_type, entity_id, due_at, status,
                        payload_json, dedupe_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO automation_tasks (
                        id, task_type, entity_type, entity_id, due_at, status,
                        payload_json, dedupe_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedupe_key) DO NOTHING
                    """,
                    values,
                )
            self.connection.commit()

            if dedupe_key is not None:
                row = self.connection.execute(
                    "SELECT * FROM automation_tasks WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT * FROM automation_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            if row is None:  # pragma: no cover - defensive database invariant
                raise RuntimeError("created task could not be read back")
            return self._from_row(row)

    def get(self, task_id: str) -> AutomationTask | None:
        row = self.connection.execute(
            "SELECT * FROM automation_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def claim_due(
        self,
        *,
        now: datetime,
        limit: int = 20,
        worker_id: str = "scheduler",
        lease_for: timedelta = timedelta(minutes=5),
    ) -> tuple[AutomationTask, ...]:
        """Atomically claim due tasks, including expired leases."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        now_text = _datetime_text(now)
        lease_until_text = _datetime_text(now + lease_for)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self.connection.execute(
                    """
                    SELECT id FROM automation_tasks
                    WHERE (
                        status = ? AND due_at <= ?
                    ) OR (
                        status = ? AND lease_until IS NOT NULL AND lease_until <= ?
                    )
                    ORDER BY due_at, id
                    LIMIT ?
                    """,
                    (
                        AutomationTaskStatus.PENDING.value,
                        now_text,
                        AutomationTaskStatus.RUNNING.value,
                        now_text,
                        limit,
                    ),
                ).fetchall()
                task_ids = [row["id"] for row in rows]
                for task_id in task_ids:
                    self.connection.execute(
                        """
                        UPDATE automation_tasks
                        SET status = ?, started_at = COALESCE(started_at, ?),
                            claimed_at = ?, lease_until = ?, worker_id = ?,
                            updated_at = ?, attempt_count = attempt_count + 1
                        WHERE id = ? AND (
                            (status = ? AND due_at <= ?)
                            OR (status = ? AND lease_until IS NOT NULL AND lease_until <= ?)
                        )
                        """,
                        (
                            AutomationTaskStatus.RUNNING.value,
                            now_text,
                            now_text,
                            lease_until_text,
                            worker_id,
                            now_text,
                            task_id,
                            AutomationTaskStatus.PENDING.value,
                            now_text,
                            AutomationTaskStatus.RUNNING.value,
                            now_text,
                        ),
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

            claimed = [self.get(task_id) for task_id in task_ids]
            return tuple(task for task in claimed if task is not None)

    def mark_succeeded(
        self,
        task_id: str,
        *,
        finished_at: datetime,
        worker_id: str | None = None,
    ) -> AutomationTask:
        return self._finish(
            task_id,
            status=AutomationTaskStatus.SUCCEEDED,
            finished_at=finished_at,
            last_error=None,
            worker_id=worker_id,
        )

    def mark_failed(
        self,
        task_id: str,
        *,
        finished_at: datetime,
        error: str,
        worker_id: str | None = None,
    ) -> AutomationTask:
        return self._finish(
            task_id,
            status=AutomationTaskStatus.FAILED,
            finished_at=finished_at,
            last_error=error,
            worker_id=worker_id,
        )

    def cancel(self, task_id: str, *, updated_at: datetime) -> AutomationTask:
        task = self._require(task_id)
        if task.status not in {
            AutomationTaskStatus.PENDING,
            AutomationTaskStatus.RUNNING,
        }:
            raise TaskStateError(f"cannot cancel task in state {task.status.value}")
        self.connection.execute(
            """
            UPDATE automation_tasks
            SET status = ?, updated_at = ?, finished_at = ?,
                lease_until = NULL, worker_id = NULL
            WHERE id = ?
            """,
            (
                AutomationTaskStatus.CANCELLED.value,
                _datetime_text(updated_at),
                _datetime_text(updated_at),
                task_id,
            ),
        )
        self.connection.commit()
        return self._require(task_id)

    def _finish(
        self,
        task_id: str,
        *,
        status: AutomationTaskStatus,
        finished_at: datetime,
        last_error: str | None,
        worker_id: str | None,
    ) -> AutomationTask:
        task = self._require(task_id)
        if task.status is not AutomationTaskStatus.RUNNING:
            raise TaskStateError(f"cannot finish task in state {task.status.value}")
        if worker_id is not None and task.worker_id != worker_id:
            raise TaskStateError(
                f"task is leased by worker {task.worker_id!r}, not {worker_id!r}"
            )
        if task.lease_until is not None and task.lease_until <= finished_at:
            raise TaskStateError("task lease has expired")
        self.connection.execute(
            """
            UPDATE automation_tasks
            SET status = ?, updated_at = ?, finished_at = ?,
                lease_until = NULL, worker_id = NULL, last_error = ?
            WHERE id = ?
            """,
            (
                status.value,
                _datetime_text(finished_at),
                _datetime_text(finished_at),
                last_error,
                task_id,
            ),
        )
        self.connection.commit()
        return self._require(task_id)

    def _require(self, task_id: str) -> AutomationTask:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"unknown automation task: {task_id}")
        return task

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AutomationTask:
        return AutomationTask(
            task_id=row["id"],
            task_type=row["task_type"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            due_at=datetime.fromisoformat(row["due_at"]),
            status=AutomationTaskStatus(row["status"]),
            payload=json.loads(row["payload_json"]),
            dedupe_key=row["dedupe_key"],
            created_at=_datetime_value(row["created_at"]),
            updated_at=_datetime_value(row["updated_at"]),
            started_at=_datetime_value(row["started_at"]),
            finished_at=_datetime_value(row["finished_at"]),
            claimed_at=_datetime_value(row["claimed_at"]),
            lease_until=_datetime_value(row["lease_until"]),
            worker_id=row["worker_id"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )


def purge_finished_automation_tasks(
    connection: sqlite3.Connection,
    cutoff: datetime,
) -> int:
    """Delete terminal tasks finished before ``cutoff``; return count.

    SHADOW_COMPARE 每个采样建一条任务（dedupe=device+sample_time），不加
    清理会无限增长。只删 SUCCEEDED/FAILED/CANCELLED，运行中的不动。
    """
    cursor = connection.execute(
        """
        DELETE FROM automation_tasks
        WHERE status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
          AND finished_at IS NOT NULL
          AND finished_at < ?
        """,
        (_datetime_text(cutoff),),
    )
    connection.commit()
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
