"""Immutable SQLite cache and resolver for validated Feishu standards.

``standard_versions`` is an audit/history table.  A successful Feishu sync
creates a validated snapshot and moves the active/last-known-good pointers in
one transaction.  A failed sync only writes a ``standard_sync_runs`` row, so
the production decision basis cannot change accidentally.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable

from domain.models import EnvironmentStandard, parse_control_type
from domain.standard_resolver import StandardNotFoundError, select_standard


_SCHEMA = """
CREATE TABLE IF NOT EXISTS standard_versions (
    standard_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    area TEXT NOT NULL,
    device_id TEXT,
    operation_type TEXT,
    control_type TEXT,
    temperature_min REAL,
    temperature_max REAL,
    humidity_min REAL,
    humidity_max REAL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    source_document TEXT NOT NULL,
    clause TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    standard_source TEXT NOT NULL DEFAULT 'legacy',
    validation_status TEXT NOT NULL DEFAULT 'UNVALIDATED',
    validated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (standard_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_standard_versions_context
    ON standard_versions(area, operation_type, enabled, priority);

CREATE TABLE IF NOT EXISTS standard_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    standard_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standard_snapshot_members (
    snapshot_id TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, standard_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_standard_snapshot_members_version
    ON standard_snapshot_members(standard_id, revision);

CREATE TABLE IF NOT EXISTS standard_runtime_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    active_snapshot_id TEXT,
    last_known_good_snapshot_id TEXT
);

CREATE TABLE IF NOT EXISTS standard_sync_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    standard_count INTEGER NOT NULL,
    errors_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    snapshot_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_standard_sync_runs_started
    ON standard_sync_runs(started_at);
"""


def _datetime_text(value: datetime) -> str:
    return value.isoformat()


def _datetime_value(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _is_feishu_source(value: str | None) -> bool:
    """Return whether provenance represents a Feishu-backed sync."""
    return bool(value and value.strip().lower().startswith("feishu:"))


def _standard_signature(standard: EnvironmentStandard) -> tuple[object, ...]:
    """Return immutable business content; provenance is intentionally excluded."""
    return (
        standard.standard_id,
        standard.revision,
        standard.area,
        standard.device_id,
        standard.operation_type,
        standard.control_type.value if standard.control_type is not None else None,
        standard.temperature_min,
        standard.temperature_max,
        standard.humidity_min,
        standard.humidity_max,
        _datetime_text(standard.effective_from),
        _datetime_text(standard.effective_to) if standard.effective_to else None,
        standard.source_document,
        standard.clause,
        standard.priority,
        standard.enabled,
    )


class SQLiteStandardRepository:
    """CRUD repository for immutable validated standard versions and snapshots."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        # Do not use executescript here: it implicitly commits before running
        # DDL, which could leave a half-applied migration after an ALTER TABLE
        # error. A savepoint makes the additive schema migration atomic and
        # preserves any caller-owned outer transaction.
        savepoint = "standard_schema_migration"
        self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            for statement in _SCHEMA.split(";"):
                statement = statement.strip()
                if statement:
                    self.connection.execute(statement)

            columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(standard_versions)"
                ).fetchall()
            }
            migrations = (
                ("device_id", "TEXT"),
                ("control_type", "TEXT"),
                ("standard_source", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("validation_status", "TEXT NOT NULL DEFAULT 'UNVALIDATED'"),
                ("validated_at", "TEXT"),
            )
            for column, definition in migrations:
                if column not in columns:
                    self.connection.execute(
                        f"ALTER TABLE standard_versions ADD COLUMN {column} {definition}"
                    )
            sync_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(standard_sync_runs)"
                ).fetchall()
            }
            if "snapshot_id" not in sync_columns:
                self.connection.execute(
                    "ALTER TABLE standard_sync_runs ADD COLUMN snapshot_id TEXT"
                )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_standard_versions_device_context
                ON standard_versions(device_id, area, operation_type, enabled, priority)
                """
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO standard_runtime_state (
                    singleton_id, active_snapshot_id, last_known_good_snapshot_id
                ) VALUES (1, NULL, NULL)
                """
            )
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def apply_snapshot(
        self,
        standards: tuple[EnvironmentStandard, ...],
        *,
        source: str,
        synced_at: datetime,
    ) -> str:
        """Atomically validate/record and activate a complete source snapshot.

        Existing ``(standard_id, revision)`` rows are never updated.  Re-reading
        the exact same revision is idempotent; changing its business content
        raises and leaves the active pointer untouched.
        """
        if not source.strip():
            raise ValueError("standard source cannot be empty")
        if not _is_feishu_source(source):
            raise ValueError("validated standard source must be feishu:*")
        sync_id = uuid.uuid4().hex
        timestamp = _datetime_text(synced_at)
        owns_transaction = not self.connection.in_transaction
        savepoint = f"standard_apply_{sync_id}"
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        else:
            self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            self.connection.execute(
                """
                INSERT INTO standard_sync_runs (
                    id, source, status, standard_count, errors_json, started_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sync_id, source, "RUNNING", len(standards), "[]", timestamp),
            )
            for standard in standards:
                self._insert_or_validate_no_commit(
                    standard,
                    source=source,
                    validated_at=synced_at,
                )

            if self._snapshot_matches_active_no_commit(standards):
                snapshot_id = self._active_snapshot_id_no_commit()
            else:
                snapshot_id = self._create_snapshot_no_commit(
                    standards,
                    source=source,
                    synced_at=synced_at,
                )
            self.connection.execute(
                """
                UPDATE standard_sync_runs
                SET status = ?, finished_at = ?, snapshot_id = ?
                WHERE id = ?
                """,
                ("SUCCEEDED", timestamp, snapshot_id, sync_id),
            )
            if owns_transaction:
                self.connection.commit()
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            if owns_transaction:
                self.connection.rollback()
            else:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return sync_id

    def record_sync_failure(
        self,
        *,
        source: str,
        standard_count: int,
        errors: tuple[str, ...],
        started_at: datetime,
        finished_at: datetime,
    ) -> str:
        sync_id = uuid.uuid4().hex
        self.connection.execute(
            """
            INSERT INTO standard_sync_runs (
                id, source, status, standard_count, errors_json,
                started_at, finished_at, snapshot_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                sync_id,
                source,
                "FAILED",
                standard_count,
                json.dumps(errors, ensure_ascii=False),
                _datetime_text(started_at),
                _datetime_text(finished_at),
            ),
        )
        self.connection.commit()
        return sync_id

    def _insert_or_validate_no_commit(
        self,
        standard: EnvironmentStandard,
        *,
        source: str,
        validated_at: datetime,
    ) -> None:
        existing = self.connection.execute(
            "SELECT * FROM standard_versions WHERE standard_id = ? AND revision = ?",
            (standard.standard_id, standard.revision),
        ).fetchone()
        if existing is not None:
            existing_standard = self._from_row(existing)
            if _standard_signature(existing_standard) != _standard_signature(standard):
                raise ValueError(
                    "immutable standard revision changed: "
                    f"{standard.standard_id}/{standard.revision}"
                )
            # Updating provenance/validation metadata is not a history overwrite;
            # all business columns remain immutable.
            self.connection.execute(
                """
                UPDATE standard_versions
                SET standard_source = ?, validation_status = 'VALIDATED',
                    validated_at = ?, updated_at = ?
                WHERE standard_id = ? AND revision = ?
                """,
                (
                    source,
                    _datetime_text(validated_at),
                    _datetime_text(validated_at),
                    standard.standard_id,
                    standard.revision,
                ),
            )
            return

        self.connection.execute(
            """
            INSERT INTO standard_versions (
                standard_id, revision, area, device_id, operation_type, control_type,
                temperature_min, temperature_max, humidity_min, humidity_max,
                effective_from, effective_to, source_document, clause,
                priority, enabled, standard_source, validation_status, validated_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                standard.standard_id,
                standard.revision,
                standard.area,
                standard.device_id,
                standard.operation_type,
                standard.control_type.value if standard.control_type is not None else None,
                standard.temperature_min,
                standard.temperature_max,
                standard.humidity_min,
                standard.humidity_max,
                _datetime_text(standard.effective_from),
                _datetime_text(standard.effective_to) if standard.effective_to else None,
                standard.source_document,
                standard.clause,
                standard.priority,
                int(standard.enabled),
                source,
                "VALIDATED",
                _datetime_text(validated_at),
                _datetime_text(validated_at),
                _datetime_text(validated_at),
            ),
        )

    def _create_snapshot_no_commit(
        self,
        standards: Iterable[EnvironmentStandard],
        *,
        source: str,
        synced_at: datetime,
    ) -> str:
        snapshot_id = uuid.uuid4().hex
        timestamp = _datetime_text(synced_at)
        materialized = tuple(standards)
        self.connection.execute(
            """
            INSERT INTO standard_snapshots (
                snapshot_id, source, synced_at, standard_count, status, created_at
            ) VALUES (?, ?, ?, ?, 'VALIDATED', ?)
            """,
            (snapshot_id, source, timestamp, len(materialized), timestamp),
        )
        self.connection.executemany(
            """
            INSERT INTO standard_snapshot_members (snapshot_id, standard_id, revision)
            VALUES (?, ?, ?)
            """,
            (
                (snapshot_id, standard.standard_id, standard.revision)
                for standard in materialized
            ),
        )
        self.connection.execute(
            """
            UPDATE standard_runtime_state
            SET active_snapshot_id = ?, last_known_good_snapshot_id = ?
            WHERE singleton_id = 1
            """,
            (snapshot_id, snapshot_id),
        )
        return snapshot_id

    def _active_snapshot_id_no_commit(self) -> str | None:
        row = self.connection.execute(
            "SELECT active_snapshot_id FROM standard_runtime_state WHERE singleton_id = 1"
        ).fetchone()
        return row["active_snapshot_id"] if row else None

    def active_snapshot_id(self) -> str | None:
        """Return the current active snapshot identity for sync observability."""
        return self._active_snapshot_id_no_commit()

    def _last_known_good_snapshot_id_no_commit(self) -> str | None:
        row = self.connection.execute(
            """
            SELECT last_known_good_snapshot_id
            FROM standard_runtime_state WHERE singleton_id = 1
            """
        ).fetchone()
        return row["last_known_good_snapshot_id"] if row else None

    def _snapshot_matches_active_no_commit(
        self, standards: tuple[EnvironmentStandard, ...]
    ) -> bool:
        snapshot_id = self._active_snapshot_id_no_commit()
        if snapshot_id is None:
            return False
        members = self.connection.execute(
            """
            SELECT sv.*
            FROM standard_snapshot_members AS sm
            JOIN standard_versions AS sv
              ON sv.standard_id = sm.standard_id AND sv.revision = sm.revision
            WHERE sm.snapshot_id = ?
            ORDER BY sv.standard_id, sv.revision
            """,
            (snapshot_id,),
        ).fetchall()
        current = tuple(self._from_row(row) for row in members)
        return sorted(
            (_standard_signature(item) for item in current), key=repr
        ) == sorted(
            (_standard_signature(item) for item in standards), key=repr
        )

    def _standards_for_snapshot_no_commit(
        self, snapshot_id: str | None
    ) -> tuple[EnvironmentStandard, ...]:
        if snapshot_id is None:
            return ()
        rows = self.connection.execute(
            """
            SELECT sv.*
            FROM standard_snapshot_members AS sm
            JOIN standard_versions AS sv
              ON sv.standard_id = sm.standard_id AND sv.revision = sm.revision
            WHERE sm.snapshot_id = ? AND sv.validation_status = 'VALIDATED'
            ORDER BY sv.standard_id, sv.revision
            """,
            (snapshot_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def _active_standards_no_commit(self) -> tuple[EnvironmentStandard, ...]:
        return self._standards_for_snapshot_no_commit(self._active_snapshot_id_no_commit())

    def list_all(self) -> tuple[EnvironmentStandard, ...]:
        rows = self.connection.execute(
            "SELECT * FROM standard_versions ORDER BY standard_id, revision"
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_active(self) -> tuple[EnvironmentStandard, ...]:
        """Return only versions in the active validated source snapshot."""
        return self._active_standards_no_commit()

    def list_last_known_good(self) -> tuple[EnvironmentStandard, ...]:
        """Return only versions in the last successful validated snapshot."""
        return self._standards_for_snapshot_no_commit(
            self._last_known_good_snapshot_id_no_commit()
        )

    def readiness(self, *, expected_device_ids: Iterable[str]) -> dict[str, Any]:
        """Return the persisted readiness gate for production writes.

        Legacy ``standard_versions`` rows deliberately do not participate in
        readiness.  Only rows reachable from the active snapshot and a
        successful Feishu sync can make the gate true.  This method is
        read-only so it is safe for health/status endpoints and action gates.
        """
        expected_ids = {
            str(device_id).strip().upper()
            for device_id in expected_device_ids
            if str(device_id).strip()
        }
        state = self.connection.execute(
            """
            SELECT active_snapshot_id, last_known_good_snapshot_id
            FROM standard_runtime_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        active_snapshot_id = state["active_snapshot_id"] if state else None
        last_known_good_snapshot_id = (
            state["last_known_good_snapshot_id"] if state else None
        )

        active_snapshot = None
        if active_snapshot_id is not None:
            active_snapshot = self.connection.execute(
                """
                SELECT snapshot_id, source, status, synced_at, created_at
                FROM standard_snapshots
                WHERE snapshot_id = ?
                """,
                (active_snapshot_id,),
            ).fetchone()
        lkg_snapshot = None
        if last_known_good_snapshot_id is not None:
            lkg_snapshot = self.connection.execute(
                """
                SELECT snapshot_id, source, status, synced_at, created_at
                FROM standard_snapshots
                WHERE snapshot_id = ?
                """,
                (last_known_good_snapshot_id,),
            ).fetchone()

        validated_rows = ()
        if active_snapshot_id is not None:
            validated_rows = self.connection.execute(
                """
                SELECT sv.device_id, sv.standard_id, sv.revision
                FROM standard_snapshot_members AS sm
                JOIN standard_versions AS sv
                  ON sv.standard_id = sm.standard_id AND sv.revision = sm.revision
                JOIN standard_snapshots AS ss
                  ON ss.snapshot_id = sm.snapshot_id
                WHERE sm.snapshot_id = ?
                  AND ss.status = 'VALIDATED'
                  AND sv.validation_status = 'VALIDATED'
                ORDER BY sv.standard_id, sv.revision
                """,
                (active_snapshot_id,),
            ).fetchall()
        validated_device_ids = {
            str(row["device_id"]).strip().upper()
            for row in validated_rows
            if row["device_id"] is not None and str(row["device_id"]).strip()
        }

        latest_sync = self.connection.execute(
            """
            SELECT source, status, started_at, finished_at
            FROM standard_sync_runs
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        latest_success = self.connection.execute(
            """
            SELECT source, status, started_at, finished_at
            FROM standard_sync_runs
            WHERE status = 'SUCCEEDED'
              AND lower(source) LIKE 'feishu:%'
              AND snapshot_id IS NOT NULL
            ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

        active_source = active_snapshot["source"] if active_snapshot else None
        active_validated = bool(
            active_snapshot is not None
            and active_snapshot["status"] == "VALIDATED"
        )
        lkg_validated = bool(
            lkg_snapshot is not None and lkg_snapshot["status"] == "VALIDATED"
        )
        ready = bool(
            active_snapshot_id
            and last_known_good_snapshot_id
            and active_validated
            and lkg_validated
            and expected_ids
            and validated_device_ids == expected_ids
            and latest_success is not None
            and _is_feishu_source(active_source)
        )

        return {
            "active_snapshot_id": active_snapshot_id,
            "last_known_good_snapshot_id": last_known_good_snapshot_id,
            "validated_standard_count": len(validated_rows),
            "validated_device_count": len(validated_device_ids),
            "expected_standard_count": len(expected_ids),
            "expected_device_count": len(expected_ids),
            "latest_sync_status": latest_sync["status"] if latest_sync else None,
            "last_sync_attempt_at": (
                latest_sync["started_at"] if latest_sync else None
            ),
            "last_successful_sync_at": (
                (latest_success["finished_at"] or latest_success["started_at"])
                if latest_success
                else None
            ),
            "standard_source": active_source,
            "standards_ready": ready,
            "active_snapshot_status": (
                active_snapshot["status"] if active_snapshot else None
            ),
            "last_known_good_snapshot_status": (
                lkg_snapshot["status"] if lkg_snapshot else None
            ),
        }

    def standards_ready(self, *, expected_device_ids: Iterable[str]) -> bool:
        """Return the fail-closed production action gate."""
        return bool(
            self.readiness(expected_device_ids=expected_device_ids)["standards_ready"]
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EnvironmentStandard:
        return EnvironmentStandard(
            standard_id=row["standard_id"],
            revision=row["revision"],
            area=row["area"],
            device_id=row["device_id"],
            operation_type=row["operation_type"],
            control_type=parse_control_type(row["control_type"]),
            temperature_min=row["temperature_min"],
            temperature_max=row["temperature_max"],
            humidity_min=row["humidity_min"],
            humidity_max=row["humidity_max"],
            effective_from=_datetime_value(row["effective_from"]),
            effective_to=(
                _datetime_value(row["effective_to"])
                if row["effective_to"] is not None
                else None
            ),
            source_document=row["source_document"],
            clause=row["clause"],
            priority=row["priority"],
            enabled=bool(row["enabled"]),
            standard_source=row["standard_source"] or "legacy",
        )


class SQLiteStandardResolver:
    """Resolve current validated standards, then last-known-good standards."""

    def __init__(self, repository: SQLiteStandardRepository) -> None:
        self.repository = repository

    def resolve(
        self,
        *,
        area_id: str,
        operation_type: str | None,
        timestamp: datetime,
        device_id: str | None = None,
    ) -> EnvironmentStandard:
        try:
            return select_standard(
                self.repository.list_active(),
                area_id=area_id,
                operation_type=operation_type,
                timestamp=timestamp,
                device_id=device_id,
            )
        except StandardNotFoundError as active_error:
            try:
                return select_standard(
                    self.repository.list_last_known_good(),
                    area_id=area_id,
                    operation_type=operation_type,
                    timestamp=timestamp,
                    device_id=device_id,
                )
            except StandardNotFoundError:
                raise active_error
