"""SQLite state stores used by the long-running Shadow Runtime.

These stores contain only Python-owned projections.  They never call Feishu and
are deliberately separate from the legacy ``services.db`` mirror.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from domain.models import (
    AlarmLifecycleState,
    AlarmState,
    DataQualityStatus,
    DeviceContext,
    MonitorSample,
    OperationState,
    OperationStatus,
)
from domain.operation import OperationAction, OperationObservation
from repositories.sqlite import SQLITE_WRITE_LOCK, retry_sqlite_write


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alarm_states (
    device_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    violation_started_at TEXT,
    alarm_started_at TEXT,
    recovery_started_at TEXT,
    active_alarm_id TEXT,
    pending_task_id TEXT,
    prewarning_active INTEGER NOT NULL DEFAULT 0,
    prewarning_started_at TEXT,
    prewarning_episode_id TEXT,
    prewarning_reasons_json TEXT NOT NULL DEFAULT '[]',
    prewarning_details_json TEXT NOT NULL DEFAULT '{}',
    prewarning_standard_id TEXT,
    prewarning_standard_revision TEXT,
    prewarning_notify_task_id TEXT,
    prewarning_message_id TEXT,
    prewarning_recovered_at TEXT,
    prewarning_recovery_task_id TEXT,
    prewarning_recovery_message_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prewarning_external_effects (
    effect_key TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    recipient TEXT,
    receive_id_type TEXT,
    message_id TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_prewarning_effects_device
    ON prewarning_external_effects(device_id, status);

CREATE TABLE IF NOT EXISTS latest_monitor_samples (
    device_id TEXT PRIMARY KEY,
    sample_time TEXT NOT NULL,
    temperature REAL,
    humidity REAL,
    online_status TEXT,
    data_quality TEXT,
    record_type TEXT NOT NULL DEFAULT 'MEASUREMENT',
    measurement_time TEXT,
    heartbeat_time TEXT,
    availability TEXT
);

CREATE TABLE IF NOT EXISTS operation_observations_current (
    device_id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL,
    action TEXT NOT NULL,
    operation_type TEXT,
    work_order TEXT,
    source_record_id TEXT NOT NULL,
    source_created_at TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_states (
    device_id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL,
    state TEXT NOT NULL,
    operation_type TEXT,
    work_order TEXT,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_observation_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_created_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    action TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_audit_device_time
    ON operation_observation_audit(device_id, created_at);

-- Durable state for the device-status dashboard projection.  This is an
-- additive, Python-owned table; it never replaces or rewrites business
-- events, samples, standards, or operation history.
CREATE TABLE IF NOT EXISTS device_status_projection (
    device_id TEXT PRIMARY KEY,
    record_id TEXT,
    desired_hash TEXT,
    desired_fields_json TEXT NOT NULL DEFAULT '{}',
    observed_fields_json TEXT NOT NULL DEFAULT '{}',
    changed_fields_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PENDING',
    pending INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_attempt_at TEXT,
    last_error TEXT,
    last_task_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_status_projection_status
    ON device_status_projection(status, pending, failed);
"""


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _json_load(value: str | None, default: Any) -> Any:
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return decoded


class SQLiteAlarmStateRepository:
    """Persist the alarm state machine state across process restarts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = SQLITE_WRITE_LOCK
        with self._lock:
            self.connection.executescript(_SCHEMA)
            columns = {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(alarm_states)")
            }
            for column, definition in (
                ("prewarning_active", "INTEGER NOT NULL DEFAULT 0"),
                ("prewarning_started_at", "TEXT"),
                ("prewarning_episode_id", "TEXT"),
                ("prewarning_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("prewarning_details_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("prewarning_standard_id", "TEXT"),
                ("prewarning_standard_revision", "TEXT"),
                ("prewarning_notify_task_id", "TEXT"),
                ("prewarning_message_id", "TEXT"),
                ("prewarning_recovered_at", "TEXT"),
                ("prewarning_recovery_task_id", "TEXT"),
                ("prewarning_recovery_message_id", "TEXT"),
            ):
                if column not in columns:
                    self.connection.execute(
                        f"ALTER TABLE alarm_states ADD COLUMN {column} {definition}"
                    )
            self.connection.commit()

    def get(self, device_id: str) -> AlarmState | None:
        row = self.connection.execute(
            "SELECT * FROM alarm_states WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    @retry_sqlite_write
    def save(self, state: AlarmState) -> None:
        now = datetime.now().astimezone()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO alarm_states (
                    device_id, state, violation_started_at, alarm_started_at,
                    recovery_started_at, active_alarm_id, pending_task_id,
                    prewarning_active, prewarning_started_at, prewarning_episode_id,
                    prewarning_reasons_json, prewarning_details_json,
                    prewarning_standard_id, prewarning_standard_revision,
                    prewarning_notify_task_id, prewarning_message_id,
                    prewarning_recovered_at, prewarning_recovery_task_id,
                    prewarning_recovery_message_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    state = excluded.state,
                    violation_started_at = excluded.violation_started_at,
                    alarm_started_at = excluded.alarm_started_at,
                    recovery_started_at = excluded.recovery_started_at,
                    active_alarm_id = excluded.active_alarm_id,
                    pending_task_id = excluded.pending_task_id,
                    prewarning_active = excluded.prewarning_active,
                    prewarning_started_at = excluded.prewarning_started_at,
                    prewarning_episode_id = excluded.prewarning_episode_id,
                    prewarning_reasons_json = excluded.prewarning_reasons_json,
                    prewarning_details_json = excluded.prewarning_details_json,
                    prewarning_standard_id = excluded.prewarning_standard_id,
                    prewarning_standard_revision = excluded.prewarning_standard_revision,
                    prewarning_notify_task_id = excluded.prewarning_notify_task_id,
                    prewarning_message_id = excluded.prewarning_message_id,
                    prewarning_recovered_at = excluded.prewarning_recovered_at,
                    prewarning_recovery_task_id = excluded.prewarning_recovery_task_id,
                    prewarning_recovery_message_id = excluded.prewarning_recovery_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    state.device_id,
                    AlarmLifecycleState(state.state).value,
                    _time(state.violation_started_at),
                    _time(state.alarm_started_at),
                    _time(state.recovery_started_at),
                    state.active_alarm_id,
                    state.pending_task_id,
                    int(state.prewarning_active),
                    _time(state.prewarning_started_at),
                    state.prewarning_episode_id,
                    json.dumps(list(state.prewarning_reasons), ensure_ascii=False),
                    json.dumps(dict(state.prewarning_details), ensure_ascii=False, sort_keys=True),
                    state.prewarning_standard_id,
                    state.prewarning_standard_revision,
                    state.prewarning_notify_task_id,
                    state.prewarning_message_id,
                    _time(state.prewarning_recovered_at),
                    state.prewarning_recovery_task_id,
                    state.prewarning_recovery_message_id,
                    now.isoformat(),
                ),
            )
            self.connection.commit()

    def list_prewarning_states(self) -> tuple[AlarmState, ...]:
        """Return active/recent warning state without exposing recipients."""
        rows = self.connection.execute(
            "SELECT * FROM alarm_states WHERE prewarning_active = 1 "
            "ORDER BY device_id"
        ).fetchall()
        return tuple(
            state
            for row in rows
            if (state := self._from_row(row)) is not None
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AlarmState:
        reasons = _json_load(row["prewarning_reasons_json"], [])
        details = _json_load(row["prewarning_details_json"], {})
        return AlarmState(
            device_id=row["device_id"],
            state=AlarmLifecycleState(row["state"]),
            violation_started_at=_parse(row["violation_started_at"]),
            alarm_started_at=_parse(row["alarm_started_at"]),
            recovery_started_at=_parse(row["recovery_started_at"]),
            active_alarm_id=row["active_alarm_id"],
            pending_task_id=row["pending_task_id"],
            prewarning_active=bool(row["prewarning_active"]),
            prewarning_started_at=_parse(row["prewarning_started_at"]),
            prewarning_episode_id=row["prewarning_episode_id"],
            prewarning_reasons=(
                tuple(str(item) for item in reasons) if isinstance(reasons, list) else ()
            ),
            prewarning_details=details if isinstance(details, Mapping) else {},
            prewarning_standard_id=row["prewarning_standard_id"],
            prewarning_standard_revision=row["prewarning_standard_revision"],
            prewarning_notify_task_id=row["prewarning_notify_task_id"],
            prewarning_message_id=row["prewarning_message_id"],
            prewarning_recovered_at=_parse(row["prewarning_recovered_at"]),
            prewarning_recovery_task_id=row["prewarning_recovery_task_id"],
            prewarning_recovery_message_id=row["prewarning_recovery_message_id"],
        )


class SQLitePrewarningEffectRepository:
    """Durable idempotency markers for warning messages without formal events."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = SQLITE_WRITE_LOCK
        with self._lock:
            self.connection.executescript(_SCHEMA)
            self.connection.commit()

    def get(self, effect_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM prewarning_external_effects WHERE effect_key = ?",
            (effect_key,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = _json_load(result.pop("metadata_json", "{}"), {})
        return result

    @retry_sqlite_write
    def mark_pending(
        self,
        *,
        effect_key: str,
        device_id: str,
        action_type: str,
        requested_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._upsert(
            effect_key=effect_key,
            device_id=device_id,
            action_type=action_type,
            status="PENDING",
            requested_at=_time(requested_at),
            metadata=metadata,
        )

    @retry_sqlite_write
    def mark_succeeded(
        self,
        *,
        effect_key: str,
        completed_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        existing = self.get(effect_key)
        if existing is None:
            raise KeyError(f"unknown prewarning effect: {effect_key}")
        self._upsert(
            effect_key=effect_key,
            device_id=str(existing["device_id"]),
            action_type=str(existing["action_type"]),
            status="SUCCEEDED",
            completed_at=_time(completed_at),
            metadata={**(existing.get("metadata") or {}), **dict(metadata or {})},
        )

    @retry_sqlite_write
    def mark_failed(
        self,
        *,
        effect_key: str,
        failed_at: datetime,
        error: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        existing = self.get(effect_key)
        if existing is None:
            raise KeyError(f"unknown prewarning effect: {effect_key}")
        self._upsert(
            effect_key=effect_key,
            device_id=str(existing["device_id"]),
            action_type=str(existing["action_type"]),
            status="FAILED",
            failed_at=_time(failed_at),
            error=str(error),
            metadata={**(existing.get("metadata") or {}), **dict(metadata or {})},
        )

    def _upsert(self, **values: Any) -> None:
        metadata = values.pop("metadata", {}) or {}
        values.setdefault("requested_at", None)
        values.setdefault("completed_at", None)
        values.setdefault("failed_at", None)
        values.setdefault("recipient", None)
        values.setdefault("receive_id_type", None)
        values.setdefault("message_id", None)
        values.setdefault("error", None)
        values.setdefault("recipient", metadata.get("recipient"))
        values.setdefault("receive_id_type", metadata.get("receive_id_type"))
        values.setdefault("message_id", metadata.get("message_id"))
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO prewarning_external_effects (
                    effect_key, device_id, action_type, status, requested_at,
                    completed_at, failed_at, recipient, receive_id_type,
                    message_id, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(effect_key) DO UPDATE SET
                    status = excluded.status,
                    requested_at = COALESCE(excluded.requested_at, prewarning_external_effects.requested_at),
                    completed_at = COALESCE(excluded.completed_at, prewarning_external_effects.completed_at),
                    failed_at = excluded.failed_at,
                    recipient = COALESCE(excluded.recipient, prewarning_external_effects.recipient),
                    receive_id_type = COALESCE(excluded.receive_id_type, prewarning_external_effects.receive_id_type),
                    message_id = COALESCE(excluded.message_id, prewarning_external_effects.message_id),
                    error = excluded.error,
                    metadata_json = excluded.metadata_json
                """,
                (
                    values["effect_key"], values["device_id"], values["action_type"],
                    values["status"], values["requested_at"], values["completed_at"],
                    values["failed_at"], values["recipient"], values["receive_id_type"],
                    values["message_id"], values["error"],
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.connection.commit()


class SQLiteLatestSampleRepository:
    """Keep the latest normalized sample for durable delayed verification."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = SQLITE_WRITE_LOCK
        with self._lock:
            self.connection.executescript(_SCHEMA)
            columns = {
                row[1]
                for row in self.connection.execute(
                    "PRAGMA table_info(latest_monitor_samples)"
                )
            }
            for column, definition in (
                ("record_type", "TEXT NOT NULL DEFAULT 'MEASUREMENT'"),
                ("measurement_time", "TEXT"),
                ("heartbeat_time", "TEXT"),
                ("availability", "TEXT"),
            ):
                if column not in columns:
                    self.connection.execute(
                        f"ALTER TABLE latest_monitor_samples ADD COLUMN {column} {definition}"
                    )
            self.connection.commit()

    @retry_sqlite_write
    def save(self, sample: MonitorSample) -> None:
        quality = sample.data_quality
        quality_value = quality.value if hasattr(quality, "value") else quality
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO latest_monitor_samples (
                    device_id, sample_time, temperature, humidity,
                    online_status, data_quality, record_type,
                    measurement_time, heartbeat_time, availability
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    sample_time = excluded.sample_time,
                    temperature = excluded.temperature,
                    humidity = excluded.humidity,
                    online_status = excluded.online_status,
                    data_quality = excluded.data_quality,
                    record_type = excluded.record_type,
                    measurement_time = excluded.measurement_time,
                    heartbeat_time = excluded.heartbeat_time,
                    availability = excluded.availability
                """,
                (
                    sample.device_id,
                    sample.sample_time.isoformat(),
                    sample.temperature,
                    sample.humidity,
                    sample.online_status,
                    quality_value,
                    sample.record_type or "MEASUREMENT",
                    _time(sample.measurement_time),
                    _time(sample.heartbeat_time),
                    sample.availability or sample.online_status,
                ),
            )
            self.connection.commit()

    def get(self, device_id: str) -> MonitorSample | None:
        row = self.connection.execute(
            "SELECT * FROM latest_monitor_samples WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            return None
        quality = row["data_quality"]
        return MonitorSample(
            device_id=row["device_id"],
            sample_time=datetime.fromisoformat(row["sample_time"]),
            temperature=row["temperature"],
            humidity=row["humidity"],
            online_status=row["online_status"],
            data_quality=(DataQualityStatus(quality) if quality else None),
            record_type=row["record_type"] or "MEASUREMENT",
            measurement_time=(
                _parse(row["measurement_time"])
                if row["measurement_time"]
                else datetime.fromisoformat(row["sample_time"])
            ),
            heartbeat_time=(
                _parse(row["heartbeat_time"])
                if row["heartbeat_time"]
                else None
            ),
            availability=row["availability"] or row["online_status"],
        )


class SQLiteOperationRepository:
    """Current operation state plus source-ordering audit trail."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = SQLITE_WRITE_LOCK
        with self._lock:
            self.connection.executescript(_SCHEMA)
            self.connection.commit()

    def get_current(self, device_id: str) -> OperationObservation | None:
        row = self.connection.execute(
            "SELECT * FROM operation_observations_current WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None:
            return None
        return OperationObservation(
            device_id=row["device_id"],
            area_id=row["area_id"],
            action=OperationAction(row["action"]),
            operation_type=row["operation_type"],
            work_order=row["work_order"],
            source_record_id=row["source_record_id"],
            source_created_at=datetime.fromisoformat(row["source_created_at"]),
            observed_at=datetime.fromisoformat(row["observed_at"]),
        )

    @retry_sqlite_write
    def save_current(self, observation: OperationObservation) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO operation_observations_current (
                    device_id, area_id, action, operation_type, work_order,
                    source_record_id, source_created_at, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    area_id = excluded.area_id,
                    action = excluded.action,
                    operation_type = excluded.operation_type,
                    work_order = excluded.work_order,
                    source_record_id = excluded.source_record_id,
                    source_created_at = excluded.source_created_at,
                    observed_at = excluded.observed_at
                """,
                (
                    observation.device_id,
                    observation.area_id,
                    observation.action.value,
                    observation.operation_type,
                    observation.work_order,
                    observation.source_record_id,
                    observation.source_created_at.isoformat(),
                    observation.observed_at.isoformat(),
                ),
            )
            previous = self.connection.execute(
                "SELECT * FROM operation_states WHERE device_id = ?",
                (observation.device_id,),
            ).fetchone()
            previous_started = _parse(previous["started_at"]) if previous else None
            if observation.action in {OperationAction.START, OperationAction.SWITCH}:
                state = OperationStatus.OPERATING
                operation_type = observation.operation_type
                work_order = observation.work_order
                started_at = observation.source_created_at
                ended_at = None
            else:
                state = OperationStatus.IDLE
                operation_type = None
                work_order = None
                started_at = previous_started or observation.source_created_at
                ended_at = observation.source_created_at
            self.connection.execute(
                """
                INSERT INTO operation_states (
                    device_id, area_id, state, operation_type, work_order,
                    started_at, ended_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    area_id = excluded.area_id,
                    state = excluded.state,
                    operation_type = excluded.operation_type,
                    work_order = excluded.work_order,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    updated_at = excluded.updated_at
                """,
                (
                    observation.device_id,
                    observation.area_id,
                    state.value,
                    operation_type,
                    work_order,
                    _time(started_at),
                    _time(ended_at),
                    observation.observed_at.isoformat(),
                ),
            )
            self._audit_no_commit(observation, accepted=True, reason="accepted_newer_source_record")
            self.connection.commit()

    @retry_sqlite_write
    def record_stale(self, observation: OperationObservation) -> None:
        with self._lock:
            self._audit_no_commit(
                observation,
                accepted=False,
                reason="stale_or_duplicate_source_record",
            )
            self.connection.commit()

    def get(self, device: DeviceContext) -> OperationState:
        row = self.connection.execute(
            "SELECT * FROM operation_states WHERE device_id = ?", (device.device_id,)
        ).fetchone()
        if row is None:
            return OperationState(
                area_id=device.area,
                # Operation state is independent runtime context.  The
                # environmental control mode is resolved from Feishu only by
                # the monitor engine and is never inferred here.
                state=OperationStatus.NOT_APPLICABLE,
                operation_type=None,
                work_order=None,
                started_at=None,
                ended_at=None,
            )
        return OperationState(
            area_id=row["area_id"],
            state=OperationStatus(row["state"]),
            operation_type=row["operation_type"],
            work_order=row["work_order"],
            started_at=_parse(row["started_at"]),
            ended_at=_parse(row["ended_at"]),
        )

    def _audit_no_commit(
        self,
        observation: OperationObservation,
        *,
        accepted: bool,
        reason: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO operation_observation_audit (
                device_id, source_record_id, source_created_at, observed_at,
                action, accepted, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.device_id,
                observation.source_record_id,
                observation.source_created_at.isoformat(),
                observation.observed_at.isoformat(),
                observation.action.value,
                int(accepted),
                reason,
                datetime.now().astimezone().isoformat(),
            ),
        )


class SQLiteDeviceStatusProjectionRepository:
    """Persist desired/observed projection state without calling Feishu.

    The desired state is local truth for the asynchronous task.  The
    observed state is updated only after the remote update has succeeded (or
    a no-op comparison proved that the remote row already matched).  This
    makes a worker crash after a successful Feishu PUT safe to replay.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = SQLITE_WRITE_LOCK
        with self._lock:
            self.connection.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in self.connection.execute(
                    "PRAGMA table_info(device_status_projection)"
                )
            }
            if "changed_fields_json" not in columns:
                self.connection.execute(
                    """
                    ALTER TABLE device_status_projection
                    ADD COLUMN changed_fields_json TEXT NOT NULL DEFAULT '[]'
                    """
                )
            self.connection.commit()

    def get(self, device_id: str) -> dict[str, Any] | None:
        normalized = str(device_id).strip().upper()
        row = self.connection.execute(
            "SELECT * FROM device_status_projection WHERE device_id = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in (
            "desired_fields_json",
            "observed_fields_json",
            "changed_fields_json",
        ):
            result[key.removesuffix("_json")] = _json_load(result.pop(key), {})
        if not isinstance(result["changed_fields"], list):
            result["changed_fields"] = []
        result["pending"] = bool(result.get("pending"))
        result["failed"] = bool(result.get("failed"))
        return result

    @retry_sqlite_write
    def save_desired(
        self,
        *,
        device_id: str,
        record_id: str | None,
        desired_hash: str,
        desired_fields: Mapping[str, Any],
        updated_at: datetime,
    ) -> bool:
        """Store desired state and return whether its hash changed."""
        normalized = str(device_id).strip().upper()
        existing = self.get(normalized)
        changed = existing is None or existing.get("desired_hash") != desired_hash
        payload = json.dumps(
            dict(desired_fields), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO device_status_projection (
                    device_id, record_id, desired_hash, desired_fields_json,
                    observed_fields_json, changed_fields_json, status, pending, failed,
                    last_success_at, last_attempt_at, last_error, last_task_id,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    record_id = COALESCE(excluded.record_id, device_status_projection.record_id),
                    desired_hash = excluded.desired_hash,
                    desired_fields_json = excluded.desired_fields_json,
                    changed_fields_json = CASE
                        WHEN excluded.desired_hash <> device_status_projection.desired_hash
                        THEN '[]' ELSE device_status_projection.changed_fields_json END,
                    status = CASE WHEN excluded.desired_hash <> device_status_projection.desired_hash
                                  THEN 'PENDING' ELSE device_status_projection.status END,
                    pending = CASE WHEN excluded.desired_hash <> device_status_projection.desired_hash
                                   THEN 1 ELSE device_status_projection.pending END,
                    failed = CASE WHEN excluded.desired_hash <> device_status_projection.desired_hash
                                  THEN 0 ELSE device_status_projection.failed END,
                    last_error = CASE WHEN excluded.desired_hash <> device_status_projection.desired_hash
                                      THEN NULL ELSE device_status_projection.last_error END,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized,
                    record_id,
                    desired_hash,
                    payload,
                    json.dumps({}, ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    "PENDING",
                    1,
                    0,
                    None,
                    None,
                    None,
                    None,
                    _time(updated_at),
                ),
            )
            self.connection.commit()
        return changed

    @retry_sqlite_write
    def mark_pending(
        self,
        *,
        device_id: str,
        task_id: str,
        attempted_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE device_status_projection
                SET status = 'PENDING', pending = 1, failed = 0,
                    last_task_id = ?, last_attempt_at = COALESCE(?, last_attempt_at),
                    last_error = ?, updated_at = COALESCE(?, updated_at)
                WHERE device_id = ?
                """,
                (
                    task_id,
                    _time(attempted_at) if attempted_at is not None else None,
                    error,
                    _time(attempted_at) if attempted_at is not None else None,
                    str(device_id).strip().upper(),
                ),
            )
            self.connection.commit()

    @retry_sqlite_write
    def mark_success(
        self,
        *,
        device_id: str,
        record_id: str,
        observed_fields: Mapping[str, Any],
        completed_at: datetime,
    ) -> None:
        encoded = json.dumps(
            dict(observed_fields), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock:
            self.connection.execute(
                """
                UPDATE device_status_projection
                SET record_id = ?, observed_fields_json = ?, status = 'SUCCEEDED',
                    changed_fields_json = '[]',
                    pending = 0, failed = 0, last_success_at = ?,
                    last_attempt_at = ?, last_error = NULL, updated_at = ?
                WHERE device_id = ?
                """,
                (
                    record_id,
                    encoded,
                    _time(completed_at),
                    _time(completed_at),
                    _time(completed_at),
                    str(device_id).strip().upper(),
                ),
            )
            self.connection.commit()

    @retry_sqlite_write
    def mark_gated(
        self,
        *,
        device_id: str,
        record_id: str,
        observed_fields: Mapping[str, Any],
        changed_fields: list[str],
        reconciled_at: datetime,
        task_id: str,
    ) -> None:
        """Persist a read-only reconcile without claiming remote success.

        ``SHADOW_ONLY`` is intentionally distinct from ``SUCCEEDED``: the
        observed row is recorded for drift visibility, while
        ``last_success_at`` remains the timestamp of an actual Feishu update
        (or a confirmed remote no-op when the write gate was enabled).
        """
        observed = json.dumps(
            dict(observed_fields), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        changed = json.dumps(
            sorted({str(field) for field in changed_fields}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self.connection.execute(
                """
                UPDATE device_status_projection
                SET record_id = ?, observed_fields_json = ?, changed_fields_json = ?,
                    status = 'SHADOW_ONLY', pending = 0, failed = 0,
                    last_attempt_at = ?, last_task_id = ?, last_error = NULL,
                    updated_at = ?
                WHERE device_id = ?
                """,
                (
                    record_id,
                    observed,
                    changed,
                    _time(reconciled_at),
                    task_id,
                    _time(reconciled_at),
                    str(device_id).strip().upper(),
                ),
            )
            self.connection.commit()

    @retry_sqlite_write
    def mark_failure(
        self,
        *,
        device_id: str,
        error: str,
        attempted_at: datetime,
        terminal: bool,
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE device_status_projection
                SET status = ?, pending = ?, failed = ?,
                    last_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE device_id = ?
                """,
                (
                    "FAILED" if terminal else "PENDING",
                    int(not terminal),
                    int(terminal),
                    _time(attempted_at),
                    str(error)[:500],
                    _time(attempted_at),
                    str(device_id).strip().upper(),
                ),
            )
            self.connection.commit()

    @retry_sqlite_write
    def mark_shadow_only(self, *, device_id: str, updated_at: datetime) -> None:
        """Record that the desired row is outside the current Active scope."""
        with self._lock:
            self.connection.execute(
                """
                UPDATE device_status_projection
                SET status = 'SHADOW_ONLY', pending = 0, failed = 0,
                    changed_fields_json = '[]',
                    last_error = NULL, updated_at = ?
                WHERE device_id = ?
                """,
                (_time(updated_at), str(device_id).strip().upper()),
            )
            self.connection.commit()

    def summary(self) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM device_status_projection ORDER BY device_id"
        ).fetchall()
        pending = sum(1 for row in rows if row["pending"])
        failed = sum(1 for row in rows if row["failed"])
        mismatched: list[str] = []
        devices: list[dict[str, Any]] = []
        gated_count = 0
        planned_count = 0
        last_success: str | None = None
        last_error: str | None = None
        last_error_updated_at: str | None = None
        last_reconcile_at: str | None = None
        for row in rows:
            desired = _json_load(row["desired_fields_json"], {})
            observed = _json_load(row["observed_fields_json"], {})
            changed_fields = _json_load(row["changed_fields_json"], [])
            if not isinstance(changed_fields, list):
                changed_fields = []
            if row["pending"] or row["failed"] or changed_fields or desired != observed:
                mismatched.append(str(row["device_id"]))
            if row["status"] == "SHADOW_ONLY":
                gated_count += 1
            if row["status"] in {"PENDING", "SHADOW_ONLY"} and changed_fields:
                planned_count += 1
            if row["updated_at"] and (
                last_reconcile_at is None or row["updated_at"] > last_reconcile_at
            ):
                last_reconcile_at = row["updated_at"]
            devices.append(
                {
                    "device_id": str(row["device_id"]),
                    "record_id": row["record_id"],
                    "desired_hash": row["desired_hash"],
                    "desired_fields": desired,
                    "observed_fields": observed,
                    "changed_fields": sorted({str(field) for field in changed_fields}),
                    "status": str(row["status"]),
                    "pending": bool(row["pending"]),
                    "failed": bool(row["failed"]),
                    "last_success_at": row["last_success_at"],
                    "last_attempt_at": row["last_attempt_at"],
                    "last_error": row["last_error"],
                    "last_task_id": row["last_task_id"],
                }
            )
            if row["last_success_at"] and (
                last_success is None or row["last_success_at"] > last_success
            ):
                last_success = row["last_success_at"]
            if row["last_error"] and (
                last_error_updated_at is None
                or str(row["updated_at"]) >= last_error_updated_at
            ):
                last_error = str(row["last_error"])
                last_error_updated_at = str(row["updated_at"])
        return {
            "pending": pending,
            "failed": failed,
            "last_success_at": last_success,
            "last_error": last_error,
            "mismatched_devices": mismatched,
            "devices": devices,
            "gated_count": gated_count,
            "planned_count": planned_count,
            "last_reconcile_at": last_reconcile_at,
        }


__all__ = [
    "SQLiteAlarmStateRepository",
    "SQLiteLatestSampleRepository",
    "SQLiteOperationRepository",
    "SQLiteDeviceStatusProjectionRepository",
]
