"""Pure domain models and rules for environmental monitoring.

The package deliberately has no Flask, database, Feishu, Modbus, or network
dependencies.  Application and integration layers may call into it, but the
domain layer must remain deterministic and side-effect free.
"""

from .alarm_state_machine import AlarmStateMachine
from .models import (
    AlarmAction,
    AlarmActionType,
    AlarmLifecycleState,
    AlarmState,
    ApplicabilityStatus,
    AutomationTask,
    AutomationTaskStatus,
    ControlType,
    DataQualityStatus,
    DeviceContext,
    EnvironmentStandard,
    MonitorResult,
    MonitorSample,
    OperationState,
    OperationStatus,
    OverallStatus,
    StateTransition,
    TemperatureStatus,
    MonitoringControlType,
    parse_control_type,
)
from .monitor_engine import MonitorEngine, evaluate_monitor_state
from .operation import OperationAction, OperationObservation, is_newer_operation
from .standard_resolver import (
    StandardConfigurationConflictError,
    StandardNotFoundError,
    StandardResolutionError,
    StandardResolver,
    StaticStandardResolver,
    select_standard,
)

__all__ = [
    "AlarmAction",
    "AlarmActionType",
    "AlarmLifecycleState",
    "AlarmState",
    "AlarmStateMachine",
    "ApplicabilityStatus",
    "AutomationTask",
    "AutomationTaskStatus",
    "ControlType",
    "DataQualityStatus",
    "DeviceContext",
    "EnvironmentStandard",
    "MonitorResult",
    "MonitorEngine",
    "MonitorSample",
    "OperationState",
    "OperationStatus",
    "OperationAction",
    "OperationObservation",
    "OverallStatus",
    "StateTransition",
    "StandardResolver",
    "StandardConfigurationConflictError",
    "StandardNotFoundError",
    "StandardResolutionError",
    "StaticStandardResolver",
    "TemperatureStatus",
    "MonitoringControlType",
    "parse_control_type",
    "is_newer_operation",
    "evaluate_monitor_state",
    "select_standard",
]
