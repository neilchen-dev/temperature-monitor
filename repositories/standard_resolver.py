"""SQLite persistence and resolution for versioned environmental standards."""

from __future__ import annotations

import sqlite3
import json
import uuid
from datetime import datetime

from domain.models import EnvironmentStandard
from domain.standard_resolver import select_standard


_SCHEMA = """
CREATE TABLE IF NOT EXISTS standard_versions (
    standard_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    area TEXT NOT NULL,
    device_id TEXT,
    operation_type TEXT,
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (standard_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_standard_versions_context
    ON standard_versions(area, operation_type, enabled, priority);

CREATE TABLE IF NOT EXISTS standard_sync_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    standard_count INTEGER NOT NULL,
    errors_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_standard_sync_runs_started
    ON standard_sync_runs(started_at);
"""


def _datetime_text(value: datetime) -> str:
    return value.isoformat()


def _datetime_value(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SQLiteStandardRepository:
    """CRUD repository for the local, versioned standards cache."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(standard_versions)"
            ).fetchall()
        }
        if "device_id" not in columns:
            self.connection.execute(
                "ALTER TABLE standard_versions ADD COLUMN device_id TEXT"
            )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_standard_versions_device_context
            ON standard_versions(device_id, area, operation_type, enabled, priority)
            """
        )
        self.connection.commit()

    def upsert(
        self,
        standard: EnvironmentStandard,
        *,
        updated_at: datetime,
    ) -> EnvironmentStandard:
        """Insert or replace one ``standard_id + revision`` snapshot."""
        self._upsert_no_commit(standard, updated_at=updated_at)
        self.connection.commit()
        return standard

    def apply_snapshot(
        self,
        standards: tuple[EnvironmentStandard, ...],
        *,
        source: str,
        synced_at: datetime,
    ) -> str:
        """Atomically activate a validated full snapshot.

        The source contract is a full snapshot. Existing rows are disabled
        inside the same transaction before incoming rows are upserted, so an
        invalid or interrupted sync cannot replace the previous active cache.
        """
        sync_id = uuid.uuid4().hex
        timestamp = _datetime_text(synced_at)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT INTO standard_sync_runs (
                    id, source, status, standard_count, errors_json, started_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sync_id,
                    source,
                    "RUNNING",
                    len(standards),
                    "[]",
                    timestamp,
                ),
            )
            self.connection.execute(
                """
                UPDATE standard_versions
                SET enabled = 0, updated_at = ?
                WHERE enabled = 1
                """,
                (timestamp,),
            )
            for standard in standards:
                self._upsert_no_commit(standard, updated_at=synced_at)
            self.connection.execute(
                """
                UPDATE standard_sync_runs
                SET status = ?, finished_at = ?
                WHERE id = ?
                """,
                ("SUCCEEDED", timestamp, sync_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
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
                started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
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

    def _upsert_no_commit(
        self,
        standard: EnvironmentStandard,
        *,
        updated_at: datetime,
    ) -> None:
        created_at = updated_at
        existing = self.connection.execute(
            """
            SELECT created_at FROM standard_versions
            WHERE standard_id = ? AND revision = ?
            """,
            (standard.standard_id, standard.revision),
        ).fetchone()
        if existing is not None:
            created_at = _datetime_value(existing["created_at"])

        self.connection.execute(
            """
            INSERT INTO standard_versions (
                standard_id, revision, area, device_id, operation_type,
                temperature_min, temperature_max, humidity_min, humidity_max,
                effective_from, effective_to, source_document, clause,
                priority, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(standard_id, revision) DO UPDATE SET
                area = excluded.area,
                device_id = excluded.device_id,
                operation_type = excluded.operation_type,
                temperature_min = excluded.temperature_min,
                temperature_max = excluded.temperature_max,
                humidity_min = excluded.humidity_min,
                humidity_max = excluded.humidity_max,
                effective_from = excluded.effective_from,
                effective_to = excluded.effective_to,
                source_document = excluded.source_document,
                clause = excluded.clause,
                priority = excluded.priority,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                standard.standard_id,
                standard.revision,
                standard.area,
                standard.device_id,
                standard.operation_type,
                standard.temperature_min,
                standard.temperature_max,
                standard.humidity_min,
                standard.humidity_max,
                _datetime_text(standard.effective_from),
                (
                    _datetime_text(standard.effective_to)
                    if standard.effective_to is not None
                    else None
                ),
                standard.source_document,
                standard.clause,
                standard.priority,
                int(standard.enabled),
                _datetime_text(created_at),
                _datetime_text(updated_at),
            ),
        )

    def list_all(self) -> tuple[EnvironmentStandard, ...]:
        rows = self.connection.execute(
            "SELECT * FROM standard_versions ORDER BY standard_id, revision"
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EnvironmentStandard:
        return EnvironmentStandard(
            standard_id=row["standard_id"],
            revision=row["revision"],
            area=row["area"],
            device_id=row["device_id"],
            operation_type=row["operation_type"],
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
        )


class SQLiteStandardResolver:
    """Resolve standards from the latest local SQLite cache."""

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
        return select_standard(
            self.repository.list_all(),
            area_id=area_id,
            operation_type=operation_type,
            timestamp=timestamp,
            device_id=device_id,
        )
