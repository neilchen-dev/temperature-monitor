"""Persistence adapters for the domain layer.

Repositories depend on domain value objects; domain code never imports this
package.
"""

from .automation_tasks import SQLiteAutomationTaskRepository, TaskStateError
from .automation_runs import SQLiteAutomationRunRepository
from .environment_events import (
    EnvironmentEventRecord,
    EnvironmentEventStatus,
    SQLiteEnvironmentEventRepository,
)
from .standard_resolver import SQLiteStandardRepository, SQLiteStandardResolver
from .runtime_state import (
    SQLiteAlarmStateRepository,
    SQLiteLatestSampleRepository,
    SQLiteOperationRepository,
)
from .sqlite import connect

__all__ = [
    "SQLiteAutomationTaskRepository",
    "SQLiteAutomationRunRepository",
    "EnvironmentEventRecord",
    "EnvironmentEventStatus",
    "SQLiteEnvironmentEventRepository",
    "SQLiteStandardRepository",
    "SQLiteStandardResolver",
    "SQLiteAlarmStateRepository",
    "SQLiteLatestSampleRepository",
    "SQLiteOperationRepository",
    "TaskStateError",
    "connect",
]
