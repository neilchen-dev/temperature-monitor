"""Value objects used by the environmental-monitoring domain layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class OperationStatus(str, Enum):
    """The operation context in which a monitor point is being evaluated."""

    IDLE = "IDLE"
    OPERATING = "OPERATING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ControlType(str, Enum):
    """How a monitor point's environmental rule is applied."""

    ALL_DAY = "ALL_DAY"
    OPERATION_PERIOD = "OPERATION_PERIOD"
    MONITOR_ONLY = "MONITOR_ONLY"


_CONTROL_TYPE_ALIASES = {
    "全天控制": ControlType.ALL_DAY,
    "作业期间控制": ControlType.OPERATION_PERIOD,
    "仅监测": ControlType.MONITOR_ONLY,
}


def parse_control_type(value: ControlType | str | None) -> ControlType | None:
    """Normalize standard/device control types; reject unknown non-empty values."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, ControlType):
        return value
    normalized = str(value).strip()
    try:
        return ControlType(normalized)
    except ValueError:
        parsed = _CONTROL_TYPE_ALIASES.get(normalized)
        if parsed is None:
            raise ValueError(f"unsupported control_type: {value!r}") from None
        return parsed


# A descriptive alias for callers that prefer the longer name.
MonitoringControlType = ControlType


class TemperatureStatus(str, Enum):
    NORMAL = "NORMAL"
    LOW = "LOW"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class OverallStatus(str, Enum):
    NORMAL = "NORMAL"
    VIOLATION = "VIOLATION"
    UNKNOWN = "UNKNOWN"


class ApplicabilityStatus(str, Enum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NO_STANDARD = "NO_STANDARD"


class DataQualityStatus(str, Enum):
    GOOD = "GOOD"
    OFFLINE = "OFFLINE"
    MISSING = "MISSING"
    ERROR = "ERROR"


class AlarmLifecycleState(str, Enum):
    NORMAL = "NORMAL"
    PENDING = "PENDING"
    ALARM = "ALARM"
    RECOVERY = "RECOVERY"


class AlarmActionType(str, Enum):
    CREATE_VERIFY_TASK = "CREATE_VERIFY_TASK"
    CANCEL_VERIFY_TASK = "CANCEL_VERIFY_TASK"
    COMPLETE_VERIFY_TASK = "COMPLETE_VERIFY_TASK"
    CREATE_ALARM_EVENT = "CREATE_ALARM_EVENT"
    UPDATE_ALARM_EVENT = "UPDATE_ALARM_EVENT"
    START_RECOVERY = "START_RECOVERY"
    MARK_ALARM_RECOVERED = "MARK_ALARM_RECOVERED"


class AutomationTaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class DeviceContext:
    """Stable device metadata needed by the standard resolver and evaluator."""

    device_id: str
    area: str
    name: str | None = None
    control_type: ControlType | str | None = None


@dataclass(frozen=True)
class MonitorSample:
    """One observation from HA, Modbus, or another acquisition adapter."""

    device_id: str
    sample_time: datetime
    temperature: float | None
    humidity: float | None
    online_status: str | None = None
    data_quality: DataQualityStatus | str | None = None


@dataclass(frozen=True)
class EnvironmentStandard:
    """A versioned definition of what is considered environmentally compliant."""

    standard_id: str
    revision: str
    area: str
    operation_type: str | None
    temperature_min: float | None
    temperature_max: float | None
    humidity_min: float | None
    humidity_max: float | None
    effective_from: datetime
    effective_to: datetime | None
    source_document: str
    clause: str | None
    priority: int = 0
    enabled: bool = True
    device_id: str | None = None
    control_type: ControlType | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "control_type", parse_control_type(self.control_type))
        if not self.standard_id.strip():
            raise ValueError("standard_id cannot be empty")
        if not self.revision.strip():
            raise ValueError("revision cannot be empty")
        if not self.area.strip():
            raise ValueError("area cannot be empty")
        if self.device_id is not None and not self.device_id.strip():
            raise ValueError("device_id cannot be blank when provided")
        if not self.source_document.strip():
            raise ValueError("source_document cannot be empty")
        if self.effective_from.tzinfo is None or self.effective_from.utcoffset() is None:
            raise ValueError("effective_from must be timezone-aware")
        if (
            self.effective_to is not None
            and (
                self.effective_to.tzinfo is None
                or self.effective_to.utcoffset() is None
            )
        ):
            raise ValueError("effective_to must be timezone-aware")
        if (self.temperature_min is None) != (self.temperature_max is None):
            raise ValueError("temperature bounds must be provided as a pair")
        if (self.humidity_min is None) != (self.humidity_max is None):
            raise ValueError("humidity bounds must be provided as a pair")
        if (
            self.temperature_min is not None
            and self.temperature_max is not None
            and self.temperature_min > self.temperature_max
        ):
            raise ValueError("temperature_min cannot exceed temperature_max")
        if (
            self.humidity_min is not None
            and self.humidity_max is not None
            and self.humidity_min > self.humidity_max
        ):
            raise ValueError("humidity_min cannot exceed humidity_max")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot precede effective_from")


@dataclass(frozen=True)
class OperationState:
    """The business context that selects the applicable environmental standard."""

    area_id: str
    state: OperationStatus
    operation_type: str | None
    work_order: str | None
    started_at: datetime | None
    ended_at: datetime | None

    def __post_init__(self) -> None:
        if not self.area_id.strip():
            raise ValueError("area_id cannot be empty")
        try:
            OperationStatus(self.state)
        except ValueError as exc:
            raise ValueError(f"unsupported operation status: {self.state!r}") from exc
        if self.ended_at is not None and self.started_at is None:
            raise ValueError("ended_at requires started_at")
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.ended_at < self.started_at
        ):
            raise ValueError("ended_at cannot precede started_at")


@dataclass(frozen=True)
class MonitorResult:
    """The deterministic result of evaluating one sample against one standard."""

    device_id: str
    sample_time: datetime
    temperature: float | None
    humidity: float | None
    temperature_status: TemperatureStatus
    humidity_status: TemperatureStatus
    overall_status: OverallStatus
    standard_id: str | None
    standard_revision: str | None
    reasons: tuple[str, ...]
    applicability: ApplicabilityStatus = ApplicabilityStatus.APPLICABLE
    data_quality: DataQualityStatus = DataQualityStatus.GOOD
    resolved_control_type: ControlType | None = None
    control_type_source: str = "configuration_error"
    control_type_consistency: str = "standard_missing"


@dataclass(frozen=True)
class AlarmState:
    """Persistable alarm lifecycle state; it is separate from alarm events."""

    device_id: str
    state: AlarmLifecycleState
    violation_started_at: datetime | None = None
    alarm_started_at: datetime | None = None
    recovery_started_at: datetime | None = None
    active_alarm_id: str | None = None
    pending_task_id: str | None = None

    @classmethod
    def normal(cls, device_id: str) -> "AlarmState":
        return cls(device_id=device_id, state=AlarmLifecycleState.NORMAL)


@dataclass(frozen=True)
class AlarmAction:
    """A side-effect instruction for the application layer to execute."""

    action_type: AlarmActionType
    device_id: str
    run_at: datetime | None = None
    alarm_id: str | None = None


@dataclass(frozen=True)
class StateTransition:
    """A pure state-machine result; it performs no persistence or I/O."""

    previous: AlarmState
    next: AlarmState
    actions: tuple[AlarmAction, ...] = ()
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.previous != self.next


@dataclass(frozen=True)
class AutomationTask:
    """A durable task value object; storage concerns stay in repositories."""

    task_id: str
    task_type: str
    entity_type: str
    entity_id: str
    due_at: datetime
    status: AutomationTaskStatus = AutomationTaskStatus.PENDING
    payload: Mapping[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    claimed_at: datetime | None = None
    lease_until: datetime | None = None
    worker_id: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
