"""Durable task repository for delayed and periodic automation actions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from domain.models import AutomationTask, AutomationTaskStatus
from repositories.sqlite import (
    SQLITE_WRITE_LOCK,
    retry_sqlite_write,
    run_sqlite_write_with_retry,
)


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
    last_error TEXT,
    created_mode TEXT,
    active_epoch TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_tasks_due
    ON automation_tasks(status, due_at);
CREATE INDEX IF NOT EXISTS idx_automation_tasks_entity
    ON automation_tasks(entity_type, entity_id, status);

CREATE TABLE IF NOT EXISTS automation_runtime_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    current_mode TEXT NOT NULL,
    active_epoch TEXT,
    active_cutover_at TEXT,
    updated_at TEXT NOT NULL
);
"""

_EXTERNAL_EFFECT_TASK_TYPES = frozenset(
    {
        "RECONCILE_ALARM_EVENT",
        "NOTIFY_ALARM",
        "NOTIFY_RECOVERY",
        "NOTIFY_PREWARNING",
        "NOTIFY_PREWARNING_RECOVERY",
        "PROJECT_DEVICE_STATUS",
    }
)


@dataclass(frozen=True)
class RuntimeActivation:
    """Persisted mode boundary used to authorize external-effect tasks."""

    mode: str
    active_epoch: str | None
    active_cutover_at: datetime | None
    updated_at: datetime | None = None


class TaskStateError(ValueError):
    """The requested task status transition is not valid."""


def _datetime_text(value: datetime) -> str:
    return value.isoformat()


def _datetime_value(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _at_or_before(value: datetime, reference: datetime) -> bool:
    """Compare task timestamps while tolerating legacy naive timestamps."""
    if value.tzinfo is None and reference.tzinfo is not None:
        value = value.replace(tzinfo=reference.tzinfo)
    elif value.tzinfo is not None and reference.tzinfo is None:
        reference = reference.replace(tzinfo=value.tzinfo)
    return value <= reference


class SQLiteAutomationTaskRepository:
    """SQLite-backed task store with durable deduplication and claiming."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        # All Runtime repositories share the process-wide SQLite write lock.
        # It protects only local transactions; task handlers perform external
        # I/O outside repository methods.
        self._lock = SQLITE_WRITE_LOCK
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.executescript(_SCHEMA)
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self._apply_migrations()

    def _apply_migrations(self) -> None:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(automation_tasks)")
        }
        for column, definition in (
            ("claimed_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("worker_id", "TEXT"),
            ("created_mode", "TEXT"),
            ("active_epoch", "TEXT"),
        ):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE automation_tasks ADD COLUMN {column} {definition}"
                )

    @retry_sqlite_write
    def set_runtime_context(
        self,
        *,
        mode: str,
        now: datetime | None = None,
        active_epoch: str | None = None,
    ) -> RuntimeActivation:
        """Record the process mode and open a new epoch on every re-activation.

        A restart while still Active reuses the persisted epoch.  A rollback to
        Shadow/disabled clears the current epoch, so the next Active startup
        gets a fresh cutover boundary.  This is metadata-only and never edits
        business events or historical task payloads.
        """
        mode_value = str(getattr(mode, "value", mode)).strip().lower()
        if mode_value not in {"disabled", "shadow", "active"}:
            raise ValueError(f"unsupported runtime mode: {mode!r}")
        current_time = now or datetime.now().astimezone()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                previous = self.connection.execute(
                    "SELECT * FROM automation_runtime_state WHERE singleton_id = 1"
                ).fetchone()
                if (
                    mode_value == "active"
                    and previous is not None
                    and previous["current_mode"] == "active"
                    and previous["active_epoch"]
                ):
                    epoch = previous["active_epoch"]
                    cutover_at = previous["active_cutover_at"]
                elif mode_value == "active":
                    epoch = active_epoch or uuid.uuid4().hex
                    cutover_at = _datetime_text(current_time)
                else:
                    epoch = None
                    cutover_at = None
                self.connection.execute(
                    """
                    INSERT INTO automation_runtime_state (
                        singleton_id, current_mode, active_epoch,
                        active_cutover_at, updated_at
                    ) VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(singleton_id) DO UPDATE SET
                        current_mode = excluded.current_mode,
                        active_epoch = excluded.active_epoch,
                        active_cutover_at = excluded.active_cutover_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        mode_value,
                        epoch,
                        cutover_at,
                        _datetime_text(current_time),
                    ),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.runtime_context()

    def runtime_context(self) -> RuntimeActivation:
        """Read the current persisted mode/epoch; missing state fails closed."""
        row = self.connection.execute(
            "SELECT * FROM automation_runtime_state WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            return RuntimeActivation("unknown", None, None, None)
        return RuntimeActivation(
            mode=str(row["current_mode"]),
            active_epoch=row["active_epoch"],
            active_cutover_at=_datetime_value(row["active_cutover_at"]),
            updated_at=_datetime_value(row["updated_at"]),
        )

    def health_summary(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=5),
    ) -> dict[str, Any]:
        """Return task health using the current runtime boundary.

        ``FAILED`` is a terminal audit state: ``claim_due`` never claims it.
        The cumulative terminal count is therefore intentionally separated
        from failures created during the current mode boundary/Active epoch.
        This keeps old Shadow history visible without letting it block a new
        Active Canary.
        """
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")

        current_time = now or datetime.now().astimezone()
        activation = self.runtime_context()
        current_boundary = activation.active_cutover_at or activation.updated_at

        def count(query: str, params: tuple[Any, ...] = ()) -> int:
            row = self.connection.execute(query, params).fetchone()
            return int(row[0] or 0) if row is not None else 0

        total_failed = count(
            "SELECT COUNT(*) FROM automation_tasks WHERE status = 'FAILED'"
        )
        current_failed = 0
        current_failed_by_type: dict[str, int] = {}
        current_failed_rows: list[sqlite3.Row] = []
        if current_boundary is not None:
            boundary_text = _datetime_text(current_boundary)
            current_failed = count(
                """
                SELECT COUNT(*) FROM automation_tasks
                WHERE status = 'FAILED' AND created_at >= ?
                """,
                (boundary_text,),
            )
            grouped = self.connection.execute(
                """
                SELECT task_type, COUNT(*) AS count
                FROM automation_tasks
                WHERE status = 'FAILED' AND created_at >= ?
                GROUP BY task_type
                ORDER BY task_type
                """,
                (boundary_text,),
            ).fetchall()
            current_failed_by_type = {
                str(row["task_type"]): int(row["count"])
                for row in grouped
            }
            current_failed_rows = self.connection.execute(
                """
                SELECT payload_json
                FROM automation_tasks
                WHERE status = 'FAILED' AND created_at >= ?
                """,
                (boundary_text,),
            ).fetchall()

        current_epoch_failed = 0
        if activation.active_epoch:
            current_epoch_failed = count(
                """
                SELECT COUNT(*) FROM automation_tasks
                WHERE status = 'FAILED' AND active_epoch = ?
                """,
                (activation.active_epoch,),
            )

        retryable_failed = sum(
            1
            for row in current_failed_rows
            if self._payload_has_retry_marker(row["payload_json"])
        )

        legacy_pending = count(
            "SELECT COUNT(*) FROM automation_tasks WHERE status = 'LEGACY_PENDING'"
        )
        placeholders = ",".join("?" for _ in _EXTERNAL_EFFECT_TASK_TYPES)
        external_params = tuple(_EXTERNAL_EFFECT_TASK_TYPES)
        pending_external = count(
            f"""
            SELECT COUNT(*) FROM automation_tasks
            WHERE task_type IN ({placeholders})
              AND status IN ('PENDING', 'RUNNING')
            """,
            external_params,
        )
        current_epoch_pending_external = 0
        if (
            activation.mode == "active"
            and activation.active_epoch
            and activation.active_cutover_at is not None
        ):
            # A pending effect created by the current Active epoch is normal
            # in-flight work.  It must remain observable, but it must not make
            # the runtime gate reject the task that is supposed to complete
            # it.  The timestamp check is an explicit second boundary guard
            # for malformed/legacy rows carrying a reused epoch.
            current_epoch_pending_external = count(
                f"""
                SELECT COUNT(*) FROM automation_tasks
                WHERE task_type IN ({placeholders})
                  AND status IN ('PENDING', 'RUNNING')
                  AND created_mode = 'active'
                  AND active_epoch = ?
                  AND created_at >= ?
                """,
                (
                    *external_params,
                    activation.active_epoch,
                    _datetime_text(activation.active_cutover_at),
                ),
            )

        # Anything pending/running outside the current Active epoch is still
        # historical or unisolated work.  In Shadow there is no current epoch,
        # so all pending external work belongs to this bucket.
        historical_pending_external = max(
            pending_external - current_epoch_pending_external,
            0,
        )
        unisolated_external = historical_pending_external

        stale_cutoff = current_time - stale_after
        stale = 0
        unfinished_rows = self.connection.execute(
            """
            SELECT status, due_at, lease_until
            FROM automation_tasks
            WHERE status IN ('PENDING', 'RUNNING')
            """
        ).fetchall()
        for row in unfinished_rows:
            if row["status"] == "RUNNING":
                lease_until = _datetime_value(row["lease_until"])
                if lease_until is not None and _at_or_before(lease_until, current_time):
                    stale += 1
                continue
            due_at = _datetime_value(row["due_at"])
            if due_at is not None and _at_or_before(due_at, stale_cutoff):
                stale += 1

        return {
            "current_mode": activation.mode,
            "active_epoch": activation.active_epoch,
            "active_cutover_at": _datetime_text(activation.active_cutover_at)
            if activation.active_cutover_at is not None
            else None,
            "current_boundary_at": _datetime_text(current_boundary)
            if current_boundary is not None
            else None,
            "failed_tasks_total": total_failed,
            "historical_failed_tasks": max(total_failed - current_failed, 0),
            "current_failed_tasks": current_failed,
            "current_failed_by_type": current_failed_by_type,
            "current_epoch_failed_tasks": current_epoch_failed,
            "retryable_failed_tasks": retryable_failed,
            "claimable_failed_tasks": 0,
            "legacy_pending_tasks": legacy_pending,
            "stale_tasks": stale,
            "pending_external_effects": pending_external,
            "historical_pending_external_effects": historical_pending_external,
            "current_epoch_pending_external_effects": current_epoch_pending_external,
            "unisolated_external_effects": unisolated_external,
        }

    def active_readiness(
        self,
        *,
        now: datetime | None = None,
        non_blocking_task_types: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Return whether task state is safe for a new Active Canary.

        Historical terminal failures and ``LEGACY_PENDING`` rows are audit
        evidence, not automatic blockers. Current failures, retryable failure
        lineage, stale unfinished work, and unisolated external-effect work
        remain blockers.
        """
        current_time = now or datetime.now().astimezone()
        summary = self.health_summary(now=current_time)
        non_blocking = frozenset(
            str(task_type).strip()
            for task_type in non_blocking_task_types
            if str(task_type).strip()
        )
        current_failed_rows: list[sqlite3.Row] = []
        if summary["current_boundary_at"] is not None:
            current_failed_rows = self.connection.execute(
                """
                SELECT task_type, entity_id, payload_json
                FROM automation_tasks
                WHERE status = 'FAILED' AND created_at >= ?
                """,
                (summary["current_boundary_at"],),
            ).fetchall()
        ignored_current_failed = sum(
            1
            for row in current_failed_rows
            if row["task_type"] in non_blocking
            or self._is_superseded_projection_failure(row)
        )
        readiness_current_failed = max(
            summary["current_failed_tasks"] - ignored_current_failed,
            0,
        )
        readiness_current_epoch_failed = summary["current_epoch_failed_tasks"]
        ignored_current_epoch_failed = 0
        if summary["active_epoch"]:
            current_epoch_failed_rows = self.connection.execute(
                """
                SELECT task_type, entity_id, payload_json
                FROM automation_tasks
                WHERE status = 'FAILED'
                  AND active_epoch = ?
                """,
                (summary["active_epoch"],),
            ).fetchall()
            ignored_current_epoch_failed = sum(
                1
                for row in current_epoch_failed_rows
                if row["task_type"] in non_blocking
                or self._is_superseded_projection_failure(row)
            )
            readiness_current_epoch_failed = max(
                readiness_current_epoch_failed - int(ignored_current_epoch_failed),
                0,
            )

        readiness_retryable_failed = summary["retryable_failed_tasks"]
        if summary["current_failed_tasks"]:
            boundary = summary.get("current_boundary_at")
            if boundary is not None:
                retry_rows = self.connection.execute(
                    """
                    SELECT task_type, entity_id, payload_json
                    FROM automation_tasks
                    WHERE status = 'FAILED' AND created_at >= ?
                    """,
                    (boundary,),
                ).fetchall()
                readiness_retryable_failed = sum(
                    1
                    for row in retry_rows
                    if row["task_type"] not in non_blocking
                    and not self._is_superseded_projection_failure(row)
                    and self._payload_has_retry_marker(row["payload_json"])
                )

        readiness_stale_tasks = summary["stale_tasks"]
        readiness_historical_pending_external = summary[
            "historical_pending_external_effects"
        ]
        readiness_unisolated_external = summary["unisolated_external_effects"]
        if non_blocking:
            placeholders = ",".join("?" for _ in non_blocking)
            stale_cutoff = current_time - timedelta(minutes=5)
            stale_rows = self.connection.execute(
                """
                SELECT task_type, status, due_at, lease_until
                FROM automation_tasks
                WHERE status IN ('PENDING', 'RUNNING')
                """
            ).fetchall()
            ignored_stale = 0
            for row in stale_rows:
                if row["task_type"] not in non_blocking:
                    continue
                if row["status"] == "RUNNING":
                    lease_until = _datetime_value(row["lease_until"])
                    if lease_until is not None and _at_or_before(
                        lease_until, current_time
                    ):
                        ignored_stale += 1
                else:
                    due_at = _datetime_value(row["due_at"])
                    if due_at is not None and _at_or_before(due_at, stale_cutoff):
                        ignored_stale += 1
            readiness_stale_tasks = max(summary["stale_tasks"] - ignored_stale, 0)

            external_params: tuple[Any, ...]
            historical_query = f"""
                SELECT COUNT(*)
                FROM automation_tasks
                WHERE task_type IN ({placeholders})
                  AND status IN ('PENDING', 'RUNNING')
            """
            activation = self.runtime_context()
            if (
                activation.mode == "active"
                and activation.active_epoch
                and activation.active_cutover_at is not None
            ):
                historical_query += """
                  AND NOT (
                      created_mode = 'active'
                      AND active_epoch = ?
                      AND created_at >= ?
                  )
                """
                external_params = (
                    *sorted(non_blocking),
                    activation.active_epoch,
                    _datetime_text(activation.active_cutover_at),
                )
            else:
                external_params = tuple(sorted(non_blocking))
            ignored_historical_pending = int(
                self.connection.execute(historical_query, external_params).fetchone()[0]
                or 0
            )
            readiness_historical_pending_external = max(
                summary["historical_pending_external_effects"]
                - ignored_historical_pending,
                0,
            )
            readiness_unisolated_external = max(
                summary["unisolated_external_effects"]
                - ignored_historical_pending,
                0,
            )

        summary["readiness_current_failed_tasks"] = readiness_current_failed
        summary["readiness_current_epoch_failed_tasks"] = (
            readiness_current_epoch_failed
        )
        summary["readiness_retryable_failed_tasks"] = readiness_retryable_failed
        summary["readiness_stale_tasks"] = readiness_stale_tasks
        summary["readiness_historical_pending_external_effects"] = (
            readiness_historical_pending_external
        )
        summary["readiness_unisolated_external_effects"] = (
            readiness_unisolated_external
        )
        summary["readiness_ignored_task_types"] = sorted(non_blocking)
        summary["readiness_ignored_current_failed_tasks"] = ignored_current_failed
        summary["readiness_ignored_current_epoch_failed_tasks"] = int(
            ignored_current_epoch_failed or 0
        )
        blockers: list[str] = []
        if readiness_current_failed:
            blockers.append(
                f"current_failed_tasks={readiness_current_failed}"
            )
        if readiness_current_epoch_failed:
            blockers.append(
                "current_epoch_failed_tasks="
                f"{readiness_current_epoch_failed}"
            )
        if readiness_retryable_failed:
            blockers.append(
                f"retryable_failed_tasks={readiness_retryable_failed}"
            )
        if summary["claimable_failed_tasks"]:
            blockers.append(
                f"claimable_failed_tasks={summary['claimable_failed_tasks']}"
            )
        if readiness_stale_tasks:
            blockers.append(f"stale_tasks={readiness_stale_tasks}")
        if readiness_historical_pending_external:
            blockers.append(
                "historical_pending_external_effects="
                f"{readiness_historical_pending_external}"
            )
        if readiness_unisolated_external:
            blockers.append(
                "unisolated_external_effects="
                f"{readiness_unisolated_external}"
            )
        summary["active_readiness"] = not blockers
        summary["blocker_reasons"] = blockers
        # Compatibility alias for callers of the pre-release field name.
        summary["active_readiness_blockers"] = blockers
        return summary

    @staticmethod
    def _payload_has_retry_marker(payload_json: str | None) -> bool:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, Mapping):
            return False
        if payload.get("retryable") is True:
            return True
        if any(payload.get(key) is not None for key in (
            "next_retry_at", "next_retry", "next_attempt_at", "retry_at",
        )):
            return True
        try:
            return int(payload.get("retry_attempt", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    def _is_superseded_projection_failure(self, row: Mapping[str, Any]) -> bool:
        """Treat an old projection failure as audit-only after newer state wins."""
        if row["task_type"] != "PROJECT_DEVICE_STATUS":
            return False
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, Mapping):
            return False
        failed_hash = str(payload.get("desired_hash") or "").strip()
        device_id = str(row["entity_id"] or "").strip().upper()
        if not failed_hash or not device_id:
            return False
        try:
            current = self.connection.execute(
                """
                SELECT desired_hash, status
                FROM device_status_projection
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Small/legacy repository fixtures may not have the projection
            # table; without replacement evidence, fail closed.
            return False
        if current is None:
            return False
        return bool(
            current["desired_hash"]
            and str(current["desired_hash"]) != failed_hash
            and current["status"] in {"SHADOW_ONLY", "SUCCEEDED"}
        )
    @retry_sqlite_write
    def quarantine_legacy_external_tasks(
        self,
        *,
        now: datetime | None = None,
        active_epoch: str | None = None,
    ) -> int:
        """Move pre-cutover external tasks to an unclaimable audit state."""
        activation = self.runtime_context()
        if activation.mode not in {"active", "shadow", "disabled"}:
            # Test/embedded callers that have not installed a runtime context
            # must not have their generic task repository silently rewritten.
            return 0
        epoch = active_epoch or activation.active_epoch
        if not epoch and activation.mode == "active":
            return 0
        current_time = now or datetime.now().astimezone()
        placeholders = ",".join("?" for _ in _EXTERNAL_EFFECT_TASK_TYPES)
        if activation.mode == "active":
            cutover_text = (
                _datetime_text(activation.active_cutover_at)
                if activation.active_cutover_at is not None
                else _datetime_text(current_time)
            )
            eligibility = (
                "created_mode IS NULL OR created_mode <> 'active' "
                "OR active_epoch IS NULL OR active_epoch <> ? "
                "OR created_at < ?"
            )
            eligibility_params: tuple[Any, ...] = (epoch, cutover_text)
            reason = "legacy_external_effect_before_active_cutover"
        else:
            eligibility = "1 = 1"
            eligibility_params = ()
            reason = "external_effect_blocked_while_runtime_not_active"
        params: tuple[Any, ...] = (
            AutomationTaskStatus.LEGACY_PENDING.value,
            _datetime_text(current_time),
            reason,
            *_EXTERNAL_EFFECT_TASK_TYPES,
            AutomationTaskStatus.PENDING.value,
            AutomationTaskStatus.RUNNING.value,
            *eligibility_params,
        )
        with self._lock:
            cursor = self.connection.execute(
                f"""
                UPDATE automation_tasks
                SET status = ?, updated_at = ?,
                    last_error = COALESCE(last_error, ?),
                    lease_until = NULL, worker_id = NULL
                WHERE task_type IN ({placeholders})
                  AND status IN (?, ?)
                  AND ({eligibility})
                """,
                params,
            )
            self.connection.commit()
        return max(cursor.rowcount, 0)

    @retry_sqlite_write
    def recover_stale_tasks(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=5),
    ) -> int:
        """Requeue locally recoverable work left behind by a stopped runtime.

        A process restart can leave a leased task RUNNING or a due task PENDING
        until its old lease/backoff is observed.  Requeueing preserves the same
        task id, dedupe key and attempt history; it only clears the transient
        lease and gives the scheduler a chance to claim the work again.  Legacy
        external-effect rows must be quarantined before this method is called.
        """
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        current_time = now or datetime.now().astimezone()
        now_text = _datetime_text(current_time)
        stale_cutoff = _datetime_text(current_time - stale_after)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self.connection.execute(
                    """
                    UPDATE automation_tasks
                    SET status = ?, due_at = ?, updated_at = ?,
                        lease_until = NULL, worker_id = NULL,
                        last_error = COALESCE(
                            last_error,
                            'requeued after stale runtime restart'
                        )
                    WHERE (
                        status = ? AND lease_until IS NOT NULL
                        AND lease_until <= ?
                    ) OR (
                        status = ? AND due_at <= ?
                    )
                    """,
                    (
                        AutomationTaskStatus.PENDING.value,
                        now_text,
                        now_text,
                        AutomationTaskStatus.RUNNING.value,
                        now_text,
                        AutomationTaskStatus.PENDING.value,
                        stale_cutoff,
                    ),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return max(cursor.rowcount, 0)

    def external_effect_allowed(self, task: AutomationTask) -> bool:
        """Return whether this task belongs to the currently active epoch."""
        if task.task_type not in _EXTERNAL_EFFECT_TASK_TYPES:
            return True
        activation = self.runtime_context()
        return bool(
            activation.mode == "active"
            and activation.active_epoch
            and activation.active_cutover_at is not None
            and task.created_mode == "active"
            and task.active_epoch == activation.active_epoch
            and task.created_at is not None
            and _at_or_before(activation.active_cutover_at, task.created_at)
            and task.status is not AutomationTaskStatus.LEGACY_PENDING
            and task.payload.get("external_effect_policy") != "SHADOW_ONLY"
        )

    def event_external_effect_allowed(
        self,
        *,
        created_mode: str | None,
        active_epoch: str | None,
    ) -> bool:
        """Authorize reconciliation only for an explicitly current-era event."""
        activation = self.runtime_context()
        return bool(
            activation.mode == "active"
            and activation.active_epoch
            and created_mode == "active"
            and active_epoch == activation.active_epoch
        )

    def _creation_metadata(self) -> tuple[str | None, str | None]:
        activation = self.runtime_context()
        if activation.mode not in {"disabled", "shadow", "active"}:
            return None, None
        return activation.mode, activation.active_epoch

    @retry_sqlite_write
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
            created_mode, active_epoch = self._creation_metadata()
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
                created_mode,
                active_epoch,
            )
            if dedupe_key is None:
                self.connection.execute(
                    """
                    INSERT INTO automation_tasks (
                        id, task_type, entity_type, entity_id, due_at, status,
                        payload_json, dedupe_key, created_at, updated_at,
                        created_mode, active_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO automation_tasks (
                        id, task_type, entity_type, entity_id, due_at, status,
                        payload_json, dedupe_key, created_at, updated_at,
                        created_mode, active_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    @retry_sqlite_write
    def create_or_get_unfinished(
        self,
        *,
        task_type: str,
        entity_type: str,
        entity_id: str,
        due_at: datetime,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: str,
        created_at: datetime,
    ) -> AutomationTask:
        """Create or reuse the unfinished task for a business identity.

        The stable ``dedupe_key`` belongs to the PENDING/RUNNING task only.
        When a new cycle starts, a terminal row that still owns that key is
        archived under a history-only key.  ``BEGIN IMMEDIATE`` serializes
        separate SQLite connections, so concurrent schedulers cannot both
        observe an empty active set and insert a task.

        Legacy duplicate PENDING rows are cancelled while adopting this
        invariant.  A RUNNING row always wins and is never modified or
        cancelled. An existing PENDING task may move earlier, but never later.
        Reconciliation tasks use their event-specific ``dedupe_key`` as the
        identity, allowing multiple alarm cycles for one device to be repaired
        independently.
        """
        if not task_type.strip():
            raise ValueError("task_type cannot be empty")
        if not entity_id.strip():
            raise ValueError("entity_id cannot be empty")
        if not dedupe_key.strip():
            raise ValueError("dedupe_key cannot be empty")

        payload_json = json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            created_mode, active_epoch = self._creation_metadata()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                if task_type == "RECONCILE_ALARM_EVENT":
                    # Reconciliation identity is one local alarm cycle, not
                    # one device. A device can have multiple recovered/CLOSED
                    # cycles waiting for external binding. Retry rows retain
                    # the same cycle prefix, so a live backoff retry is not
                    # replaced by a fresh task on every incoming sample.
                    active_rows = self.connection.execute(
                        """
                        SELECT * FROM automation_tasks
                        WHERE task_type = ?
                          AND (dedupe_key = ? OR substr(dedupe_key, 1, length(?)) = ?
                               OR (? IS NOT NULL AND json_extract(payload_json, '$.local_event_id') = ?))
                          AND status IN (?, ?)
                        ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END,
                                 due_at, id
                        """,
                        (
                            task_type,
                            dedupe_key,
                            f"{dedupe_key}:retry:",
                            f"{dedupe_key}:retry:",
                            (payload or {}).get("local_event_id"),
                            (payload or {}).get("local_event_id"),
                            AutomationTaskStatus.PENDING.value,
                            AutomationTaskStatus.RUNNING.value,
                            AutomationTaskStatus.RUNNING.value,
                        ),
                    ).fetchall()
                elif task_type == "PROJECT_DEVICE_STATUS":
                    # Projection identity is device + desired-state hash.
                    # A different desired hash for the same device must not
                    # be hidden behind an older unfinished projection task;
                    # the handler will safely no-op superseded tasks.
                    active_rows = self.connection.execute(
                        """
                        SELECT * FROM automation_tasks
                        WHERE task_type = ? AND dedupe_key = ?
                          AND status IN (?, ?)
                        ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END,
                                 due_at, id
                        """,
                        (
                            task_type,
                            dedupe_key,
                            AutomationTaskStatus.PENDING.value,
                            AutomationTaskStatus.RUNNING.value,
                            AutomationTaskStatus.RUNNING.value,
                        ),
                    ).fetchall()
                else:
                    active_rows = self.connection.execute(
                        """
                        SELECT * FROM automation_tasks
                        WHERE task_type = ? AND entity_id = ?
                          AND status IN (?, ?)
                        ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END,
                                 due_at, id
                        """,
                        (
                            task_type,
                            entity_id,
                            AutomationTaskStatus.PENDING.value,
                            AutomationTaskStatus.RUNNING.value,
                            AutomationTaskStatus.RUNNING.value,
                        ),
                    ).fetchall()

                winner = active_rows[0] if active_rows else None
                if winner is not None:
                    winner_is_retry = task_type == "RECONCILE_ALARM_EVENT"
                    duplicate_pending_ids = [
                        row["id"]
                        for row in active_rows[1:]
                        if row["status"] == AutomationTaskStatus.PENDING.value
                    ]
                    if duplicate_pending_ids:
                        placeholders = ",".join("?" for _ in duplicate_pending_ids)
                        self.connection.execute(
                            f"""
                            UPDATE automation_tasks
                            SET status = ?, updated_at = ?, finished_at = ?,
                                lease_until = NULL, worker_id = NULL
                            WHERE id IN ({placeholders}) AND status = ?
                            """,
                            (
                                AutomationTaskStatus.CANCELLED.value,
                                _datetime_text(created_at),
                                _datetime_text(created_at),
                                *duplicate_pending_ids,
                                AutomationTaskStatus.PENDING.value,
                            ),
                        )

                    if winner_is_retry:
                        # A retry row already represents this exact alarm
                        # cycle. Preserve its due_at so exponential backoff
                        # remains effective; the base key can be reclaimed
                        # after the retry reaches a terminal state.
                        self.connection.commit()
                        return self._require(winner["id"])

                    self._release_dedupe_key_from_other_row(
                        dedupe_key=dedupe_key,
                        winner_id=winner["id"],
                    )
                    if winner["dedupe_key"] != dedupe_key:
                        self.connection.execute(
                            "UPDATE automation_tasks SET dedupe_key = ? WHERE id = ?",
                            (dedupe_key, winner["id"]),
                        )
                    if (
                        winner["status"] == AutomationTaskStatus.PENDING.value
                        and due_at < datetime.fromisoformat(winner["due_at"])
                    ):
                        self.connection.execute(
                            """
                            UPDATE automation_tasks
                            SET due_at = ?, updated_at = ?
                            WHERE id = ? AND status = ? AND due_at > ?
                            """,
                            (
                                _datetime_text(due_at),
                                _datetime_text(created_at),
                                winner["id"],
                                AutomationTaskStatus.PENDING.value,
                                _datetime_text(due_at),
                            ),
                        )
                    self.connection.commit()
                    return self._require(winner["id"])

                self._release_dedupe_key_from_other_row(
                    dedupe_key=dedupe_key,
                    winner_id=None,
                )
                task_id = uuid.uuid4().hex
                self.connection.execute(
                    """
                    INSERT INTO automation_tasks (
                        id, task_type, entity_type, entity_id, due_at, status,
                        payload_json, dedupe_key, created_at, updated_at,
                        created_mode, active_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
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
                        created_mode,
                        active_epoch,
                    ),
                )
                self.connection.commit()
                return self._require(task_id)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def _release_dedupe_key_from_other_row(
        self,
        *,
        dedupe_key: str,
        winner_id: str | None,
    ) -> None:
        owner = self.connection.execute(
            "SELECT id FROM automation_tasks WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if owner is None or owner["id"] == winner_id:
            return
        self.connection.execute(
            "UPDATE automation_tasks SET dedupe_key = ? WHERE id = ?",
            (f"{dedupe_key}:HISTORY:{owner['id']}", owner["id"]),
        )

    def get(self, task_id: str) -> AutomationTask | None:
        row = self.connection.execute(
            "SELECT * FROM automation_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    @retry_sqlite_write
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
        # Re-check at claim time as a restart-safe last line of defence.  A
        # task inserted by a previous mode/epoch is moved to LEGACY_PENDING
        # before it can enter RUNNING, even if startup reconciliation raced
        # with the first scheduler poll.
        self.quarantine_legacy_external_tasks(now=now)
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

    @retry_sqlite_write
    def reschedule_running(self, task: AutomationTask, *, due_at: datetime,
                           updated_at: datetime, payload: Mapping[str, Any]) -> None:
        """Keep the cycle's task id/key and attempts across a durable backoff."""
        with self._lock:
            cursor = self.connection.execute(
                """UPDATE automation_tasks SET status = 'PENDING', due_at = ?,
                   updated_at = ?, payload_json = ?, lease_until = NULL, worker_id = NULL
                   WHERE id = ? AND status = 'RUNNING' AND worker_id = ?""",
                (_datetime_text(due_at), _datetime_text(updated_at),
                 json.dumps(dict(payload)), task.task_id, task.worker_id),
            )
            self.connection.commit()
            if cursor.rowcount != 1:
                raise TaskStateError("reconciliation task ownership was lost")

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

    @retry_sqlite_write
    def cancel(self, task_id: str, *, updated_at: datetime) -> AutomationTask:
        with self._lock:
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

    @retry_sqlite_write
    def _finish(
        self,
        task_id: str,
        *,
        status: AutomationTaskStatus,
        finished_at: datetime,
        last_error: str | None,
        worker_id: str | None,
    ) -> AutomationTask:
        with self._lock:
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
            created_mode=row["created_mode"],
            active_epoch=row["active_epoch"],
        )


def purge_finished_automation_tasks(
    connection: sqlite3.Connection,
    cutoff: datetime,
) -> int:
    """Delete terminal tasks finished before ``cutoff``; return count.

    SHADOW_COMPARE 每个采样建一条任务（dedupe=device+sample_time），不加
    清理会无限增长。只删 SUCCEEDED/FAILED/CANCELLED，运行中的不动。
    """
    def delete() -> int:
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

    return run_sqlite_write_with_retry(
        connection,
        "purge_finished_automation_tasks",
        delete,
    )
