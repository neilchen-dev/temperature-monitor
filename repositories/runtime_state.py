"""SQLite state stores used by the long-running Shadow Runtime.

These stores contain only Python-owned projections.  They never call Feishu and
are deliberately separate from the legacy ``services.db`` mirror.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

from domain.models import (
    AlarmLifecycleState,
    AlarmState,
    ControlType,
    DataQualityStatus,
    DeviceContext,
    MonitorSample,
    OperationState,
    OperationStatus,
    parse_control_type,
)
from domain.operation import OperationAction, OperationObservation


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alarm_states (
    device_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    violation_started_at TEXT,
    alarm_started_at TEXT,
    recovery_started_at TEXT,
    active_alarm_id TEXT,
    pending_task_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS latest_monitor_samples (
    device_id TEXT PRIMARY KEY,
    sample_time TEXT NOT NULL,
    temperature REAL,
    humidity REAL,
    online_status TEXT,
    data_quality TEXT
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
"""


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class SQLiteAlarmStateRepository:
    """Persist the alarm state machine state across process restarts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def get(self, device_id: str) -> AlarmState | None:
        row = self.connection.execute(
            "SELECT * FROM alarm_states WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            return None
        return AlarmState(
            device_id=row["device_id"],
            state=AlarmLifecycleState(row["state"]),
            violation_started_at=_parse(row["violation_started_at"]),
            alarm_started_at=_parse(row["alarm_started_at"]),
            recovery_started_at=_parse(row["recovery_started_at"]),
            active_alarm_id=row["active_alarm_id"],
            pending_task_id=row["pending_task_id"],
        )

    def save(self, state: AlarmState) -> None:
        now = datetime.now().astimezone()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO alarm_states (
                    device_id, state, violation_started_at, alarm_started_at,
                    recovery_started_at, active_alarm_id, pending_task_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    state = excluded.state,
                    violation_started_at = excluded.violation_started_at,
                    alarm_started_at = excluded.alarm_started_at,
                    recovery_started_at = excluded.recovery_started_at,
                    active_alarm_id = excluded.active_alarm_id,
                    pending_task_id = excluded.pending_task_id,
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
                    now.isoformat(),
                ),
            )
            self.connection.commit()


class SQLiteLatestSampleRepository:
    """Keep the latest normalized sample for durable delayed verification."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def save(self, sample: MonitorSample) -> None:
        quality = sample.data_quality
        quality_value = quality.value if hasattr(quality, "value") else quality
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO latest_monitor_samples (
                    device_id, sample_time, temperature, humidity,
                    online_status, data_quality
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    sample_time = excluded.sample_time,
                    temperature = excluded.temperature,
                    humidity = excluded.humidity,
                    online_status = excluded.online_status,
                    data_quality = excluded.data_quality
                """,
                (
                    sample.device_id,
                    sample.sample_time.isoformat(),
                    sample.temperature,
                    sample.humidity,
                    sample.online_status,
                    quality_value,
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
        )


class SQLiteOperationRepository:
    """Current operation state plus source-ordering audit trail."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
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
            control_type = parse_control_type(device.control_type)
            default_status = (
                OperationStatus.NOT_APPLICABLE
                # ALL_DAY has no operation context; MONITOR_ONLY is outside
                # operation-gated control.  Only OPERATION_PERIOD can be IDLE.
                if control_type in {ControlType.ALL_DAY, ControlType.MONITOR_ONLY}
                else OperationStatus.IDLE
            )
            return OperationState(
                area_id=device.area,
                state=default_status,
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


__all__ = [
    "SQLiteAlarmStateRepository",
    "SQLiteLatestSampleRepository",
    "SQLiteOperationRepository",
]
