"""Concurrency-safe local store for environmental monitoring cycles."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


class EnvironmentEventStatus(str):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


class ExternalCreateOutcome(str):
    """Durable outcome classification for one external CREATE attempt."""

    IN_FLIGHT = "IN_FLIGHT"
    RETRYABLE = "RETRYABLE"
    UNKNOWN = "UNKNOWN"
    NOT_RETRYABLE = "NOT_RETRYABLE"
    SUCCEEDED = "SUCCEEDED"


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
    payload_json TEXT NOT NULL,
    external_create_owner TEXT,
    external_create_lease_until TEXT
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
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self._apply_migrations()

    def _apply_migrations(self) -> None:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(environment_events)")
        }
        for column in ("external_create_owner", "external_create_lease_until"):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE environment_events ADD COLUMN {column} TEXT"
                )

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

    def mark_recovered(
        self,
        event_id: str,
        *,
        recovered_at: datetime,
    ) -> EnvironmentEventRecord:
        """Release the active-cycle constraint after physical recovery.

        ``CLOSED`` is the legacy SQLite value for a completed *monitoring
        cycle*.  It says nothing about the Feishu event's business closure.
        """
        return self.close(event_id, closed_at=recovered_at)

    def patch_external_projection(self, event_id: str, **values: Any) -> EnvironmentEventRecord:
        """Merge projection metadata under a database write transaction."""
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(event_id)
                payload = dict(record.payload)
                payload.update(values)
                self.connection.execute(
                    "UPDATE environment_events SET payload_json = ? WHERE event_id = ?",
                    (json.dumps(payload, ensure_ascii=False), event_id),
                )
                self.connection.commit()
                return self._require(event_id)
            except Exception:
                self.connection.rollback()
                raise

    def reserve_external_post(self, event_id: str) -> bool:
        """Reserve a POST unless a previous result is still unsafe to replay.

        Legacy rows with only ``feishu_create_attempted`` remain conservative:
        their outcome is unknown and reconciliation must lookup first.  A
        caller may explicitly classify a failed request as ``RETRYABLE``;
        that is the only state which re-opens the same local event identity for
        another POST.
        """
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(event_id)
                payload = dict(record.payload)
                if payload.get("feishu_record_id"):
                    self.connection.commit()
                    return False
                create_state = payload.get("feishu_create_state")
                if payload.get("feishu_create_attempted") and create_state != ExternalCreateOutcome.RETRYABLE:
                    self.connection.commit()
                    return False
                payload["feishu_create_attempted"] = True
                payload["feishu_create_state"] = ExternalCreateOutcome.IN_FLIGHT
                payload["feishu_create_attempt_count"] = int(
                    payload.get("feishu_create_attempt_count", 0)
                ) + 1
                self.connection.execute(
                    "UPDATE environment_events SET payload_json = ? WHERE event_id = ?",
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        event_id,
                    ),
                )
                self.connection.commit()
                return True
            except Exception:
                self.connection.rollback()
                raise

    def mark_external_create_outcome(
        self,
        event_id: str,
        *,
        outcome: str,
        error: str | None = None,
        observed_at: datetime | None = None,
    ) -> EnvironmentEventRecord:
        """Persist whether the last CREATE was retryable, unknown, or done."""
        allowed = {
            ExternalCreateOutcome.IN_FLIGHT,
            ExternalCreateOutcome.RETRYABLE,
            ExternalCreateOutcome.UNKNOWN,
            ExternalCreateOutcome.NOT_RETRYABLE,
            ExternalCreateOutcome.SUCCEEDED,
        }
        if outcome not in allowed:
            raise ValueError(f"unsupported external CREATE outcome: {outcome}")
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(event_id)
                payload = dict(record.payload)
                payload["feishu_create_state"] = outcome
                if error:
                    payload["feishu_create_last_error"] = str(error)
                else:
                    payload.pop("feishu_create_last_error", None)
                if observed_at is not None:
                    payload["feishu_create_outcome_at"] = _time_text(observed_at)
                self.connection.execute(
                    """
                    UPDATE environment_events
                    SET payload_json = ?, external_create_owner = NULL,
                        external_create_lease_until = NULL
                    WHERE event_id = ?
                    """,
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        event_id,
                    ),
                )
                self.connection.commit()
                return self._require(event_id)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def bind_external_record(
        self,
        event_id: str,
        *,
        record_id: str,
    ) -> EnvironmentEventRecord:
        """Persist the exact Feishu record created for this monitoring cycle."""
        if not record_id.strip():
            raise ValueError("record_id cannot be empty")
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(event_id)
                payload = dict(record.payload)
                existing = payload.get("feishu_record_id")
                if existing is not None and existing != record_id:
                    raise ValueError(
                        f"environment event {event_id} is already bound to {existing}"
                    )
                for row in self.connection.execute(
                    """
                    SELECT event_id, payload_json FROM environment_events
                    WHERE event_id <> ?
                    """,
                    (event_id,),
                ):
                    other_payload = json.loads(row["payload_json"])
                    if other_payload.get("feishu_record_id") == record_id:
                        raise ValueError(
                            f"Feishu record {record_id} is already bound to "
                            f"environment event {row['event_id']}"
                        )
                payload["feishu_record_id"] = record_id
                payload["feishu_binding_status"] = "BOUND"
                payload["feishu_create_state"] = ExternalCreateOutcome.SUCCEEDED
                payload.pop("feishu_create_last_error", None)
                if record.closed_at is not None:
                    payload.setdefault("feishu_recovered_at", record.closed_at.isoformat())
                    payload.setdefault("feishu_recovery_pending", True)
                self.connection.execute(
                    """
                    UPDATE environment_events
                    SET payload_json = ?, external_create_owner = NULL,
                        external_create_lease_until = NULL
                    WHERE event_id = ?
                    """,
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        event_id,
                    ),
                )
                self.connection.commit()
                return self._require(event_id)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def get_external_effect(
        self, event_id: str, effect_key: str,
    ) -> Mapping[str, Any] | None:
        """Return one durable external-effect marker for an event."""
        record = self.get(event_id)
        if record is None:
            raise KeyError(f"unknown environment event: {event_id}")
        effects = record.payload.get("feishu_external_effects", {})
        if not isinstance(effects, Mapping):
            return None
        marker = effects.get(effect_key)
        return dict(marker) if isinstance(marker, Mapping) else None

    def mark_external_effect_pending(
        self,
        event_id: str,
        *,
        effect_key: str,
        action_type: str,
        requested_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> EnvironmentEventRecord:
        """Record that a deterministic external effect is being attempted."""
        values: dict[str, Any] = {
            "action_type": action_type,
            "status": "PENDING",
            "requested_at": _time_text(requested_at),
        }
        if metadata:
            values.update(dict(metadata))
        return self._patch_external_effect(
            event_id,
            effect_key=effect_key,
            values=values,
        )

    def mark_external_effect_succeeded(
        self,
        event_id: str,
        *,
        effect_key: str,
        completed_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> EnvironmentEventRecord:
        """Persist proof that the external effect completed successfully."""
        values: dict[str, Any] = {
            "status": "SUCCEEDED",
            "completed_at": _time_text(completed_at),
            "error": None,
        }
        if metadata:
            values.update(dict(metadata))
        return self._patch_external_effect(
            event_id,
            effect_key=effect_key,
            values=values,
        )

    def mark_external_effect_failed(
        self,
        event_id: str,
        *,
        effect_key: str,
        failed_at: datetime,
        error: str,
        status: str = "FAILED",
        metadata: Mapping[str, Any] | None = None,
    ) -> EnvironmentEventRecord:
        """Persist a failed attempt without replacing a prior success."""
        values: dict[str, Any] = {
            "status": status,
            "failed_at": _time_text(failed_at),
            "error": str(error),
        }
        if metadata:
            values.update(dict(metadata))
        return self._patch_external_effect(
            event_id,
            effect_key=effect_key,
            values=values,
        )

    def _patch_external_effect(
        self,
        event_id: str,
        *,
        effect_key: str,
        values: Mapping[str, Any],
    ) -> EnvironmentEventRecord:
        if not effect_key.strip():
            raise ValueError("effect_key cannot be empty")
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(event_id)
                payload = dict(record.payload)
                effects = payload.get("feishu_external_effects", {})
                effects = dict(effects) if isinstance(effects, Mapping) else {}
                existing = effects.get(effect_key)
                if isinstance(existing, Mapping) and existing.get("status") == "SUCCEEDED":
                    self.connection.commit()
                    return record
                marker = dict(existing) if isinstance(existing, Mapping) else {}
                marker.update(values)
                effects[effect_key] = marker
                payload["feishu_external_effects"] = effects
                self.connection.execute(
                    "UPDATE environment_events SET payload_json = ? WHERE event_id = ?",
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        event_id,
                    ),
                )
                self.connection.commit()
                return self._require(event_id)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def mark_external_binding_pending(
        self,
        event_id: str,
        *,
        requested_at: datetime,
    ) -> EnvironmentEventRecord:
        """Persist that this local cycle entered an external CREATE path."""
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._require(event_id)
                payload = dict(record.payload)
                if payload.get("feishu_record_id"):
                    self.connection.commit()
                    return record
                payload["feishu_binding_status"] = "PENDING"
                payload.setdefault("feishu_create_requested_at", _time_text(requested_at))
                self.connection.execute(
                    "UPDATE environment_events SET payload_json = ? WHERE event_id = ?",
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        event_id,
                    ),
                )
                self.connection.commit()
                return self._require(event_id)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def list_pending_external_bindings(self) -> tuple[EnvironmentEventRecord, ...]:
        """Return unbound events independently of local lifecycle status.

        New events carry an explicit pending marker.  The audit fallback also
        recognizes events written by the pre-marker deployment by tracing
        CREATE_ALARM_EVENT's persisted transition context.
        """
        rows = self.connection.execute(
            "SELECT * FROM environment_events ORDER BY opened_at, event_id"
        ).fetchall()
        records = [self._from_row(row) for row in rows]
        candidates = {
            record.event_id
            for record in records
            if not record.payload.get("feishu_record_id")
            and record.payload.get("external_effect_policy") != "SHADOW_ONLY"
            and record.payload.get("feishu_binding_status") == "PENDING"
        }
        try:
            audit_rows = self.connection.execute(
                """
                SELECT alarm_id, python_alarm_transition_json, context_json
                FROM automation_runs
                WHERE action_type = 'CREATE_ALARM_EVENT' AND mode = 'active'
                  AND action_status IN ('FAILED', 'SUCCEEDED')
                """
            ).fetchall()
        except sqlite3.OperationalError:
            audit_rows = ()
        for row in audit_rows:
            ids: list[Any] = [row["alarm_id"]]
            for column in ("python_alarm_transition_json", "context_json"):
                try:
                    decoded = json.loads(row[column] or "{}")
                except (TypeError, ValueError):
                    decoded = {}
                if not isinstance(decoded, Mapping):
                    continue
                if column == "python_alarm_transition_json":
                    ids.append(decoded.get("active_alarm_id"))
                else:
                    transition = decoded.get("python_alarm_transition")
                    if isinstance(transition, Mapping):
                        ids.append(transition.get("active_alarm_id"))
            candidates.update(
                value.strip()
                for value in ids
                if isinstance(value, str) and value.strip()
            )
        return tuple(
            record
            for record in records
            if (
                record.event_id in candidates
                and not record.payload.get("feishu_record_id")
                and record.payload.get("external_effect_policy") != "SHADOW_ONLY"
            )
            or (
                record.payload.get("external_effect_policy") != "SHADOW_ONLY"
                and (
                    record.payload.get("feishu_recovery_pending")
                    or record.payload.get("feishu_update_pending")
                )
            )
        )

    def claim_external_create(
        self,
        event_id: str,
        *,
        owner: str,
        claimed_at: datetime,
        lease_seconds: float,
    ) -> bool:
        """Atomically elect one CREATE/reconciliation owner for a local cycle."""
        if not owner.strip():
            raise ValueError("owner cannot be empty")
        claimed_at = claimed_at.replace(tzinfo=timezone.utc) if claimed_at.tzinfo is None else claimed_at.astimezone(timezone.utc)
        lease_until = claimed_at + timedelta(seconds=max(1.0, lease_seconds))
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE environment_events
                SET external_create_owner = ?, external_create_lease_until = ?
                WHERE event_id = ?
                  AND (
                    external_create_owner IS NULL
                    OR external_create_lease_until IS NULL
                    OR julianday(external_create_lease_until) <= julianday(?)
                  )
                """,
                (
                    owner,
                    _time_text(lease_until),
                    event_id,
                    _time_text(claimed_at),
                ),
            )
            self.connection.commit()
            if cursor.rowcount:
                return True
            if self.get(event_id) is None:
                raise KeyError(f"unknown environment event: {event_id}")
            return False

    def get(self, event_id: str) -> EnvironmentEventRecord | None:
        with self._lock:
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
