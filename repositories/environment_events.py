"""Concurrency-safe local store for active environmental events."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Mapping


class EnvironmentEventStatus(str):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class EnvironmentEventRecord:
    event_id: str
    device_id: str
    event_key: str
    status: str
    opened_at: datetime
    closed_at: datetime | None
    payload: Mapping[str, Any]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS environment_events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('OPEN', 'IN_PROGRESS', 'CLOSED')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_environment_events_device_status
    ON environment_events(device_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_environment_events_one_active_device
    ON environment_events(device_id)
    WHERE status <> 'CLOSED';
"""


def _time_text(value: datetime) -> str:
    return value.isoformat()


def _time_value(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class SQLiteEnvironmentEventRepository:
    """Use SQLite constraints and a write transaction for event idempotency."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def create_or_get_active(
        self,
        *,
        device_id: str,
        event_key: str,
        opened_at: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> EnvironmentEventRecord:
        """Return the idempotent event or the device's existing active event.

        ``BEGIN IMMEDIATE`` serializes writers on a SQLite database.  The
        partial unique index remains the final invariant if multiple process
        connections race to create different event keys for one device.
        """

        if not device_id.strip():
            raise ValueError("device_id cannot be empty")
        if not event_key.strip():
            raise ValueError("event_key cannot be empty")
        payload_json = json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._find_by_key(event_key)
                if existing is None:
                    existing = self._find_active(device_id)
                if existing is not None:
                    self.connection.commit()
                    return existing

                event_id = uuid.uuid4().hex
                try:
                    self.connection.execute(
                        """
                        INSERT INTO environment_events (
                            event_id, device_id, event_key, status,
                            opened_at, closed_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            event_id,
                            device_id,
                            event_key,
                            EnvironmentEventStatus.OPEN,
                            _time_text(opened_at),
                            payload_json,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # A separate connection may have won after this process
                    # began.  Roll back before reading the committed winner.
                    self.connection.rollback()
                    existing = self._find_by_key(event_key) or self._find_active(device_id)
                    if existing is None:
                        raise
                    return existing
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
            return self._require(event_id)

    def close(
        self,
        event_id: str,
        *,
        closed_at: datetime,
    ) -> EnvironmentEventRecord:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE environment_events
                SET status = ?, closed_at = ?
                WHERE event_id = ? AND status <> ?
                """,
                (
                    EnvironmentEventStatus.CLOSED,
                    _time_text(closed_at),
                    event_id,
                    EnvironmentEventStatus.CLOSED,
                ),
            )
            self.connection.commit()
            if cursor.rowcount == 0:
                record = self.get(event_id)
                if record is None:
                    raise KeyError(f"unknown environment event: {event_id}")
                return record
            return self._require(event_id)

    def get(self, event_id: str) -> EnvironmentEventRecord | None:
        row = self.connection.execute(
            "SELECT * FROM environment_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_active(self, *, device_id: str | None = None) -> tuple[EnvironmentEventRecord, ...]:
        if device_id is None:
            rows = self.connection.execute(
                "SELECT * FROM environment_events WHERE status <> 'CLOSED' ORDER BY opened_at, event_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM environment_events
                WHERE device_id = ? AND status <> 'CLOSED'
                ORDER BY opened_at, event_id
                """,
                (device_id,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def _find_by_key(self, event_key: str) -> EnvironmentEventRecord | None:
        row = self.connection.execute(
            "SELECT * FROM environment_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def _find_active(self, device_id: str) -> EnvironmentEventRecord | None:
        row = self.connection.execute(
            """
            SELECT * FROM environment_events
            WHERE device_id = ? AND status <> 'CLOSED'
            ORDER BY opened_at, event_id
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def _require(self, event_id: str) -> EnvironmentEventRecord:
        record = self.get(event_id)
        if record is None:
            raise KeyError(f"unknown environment event: {event_id}")
        return record

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EnvironmentEventRecord:
        return EnvironmentEventRecord(
            event_id=row["event_id"],
            device_id=row["device_id"],
            event_key=row["event_key"],
            status=row["status"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_at=_time_value(row["closed_at"]),
            payload=json.loads(row["payload_json"]),
        )
