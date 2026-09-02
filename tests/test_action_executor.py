from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime

from application.action_executor import (
    ActionExecutionStatus,
    ActionExecutor,
    AutomationMode,
)
from domain.models import AlarmAction, AlarmActionType
from repositories.automation_runs import SQLiteAutomationRunRepository


class ActionExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.action = AlarmAction(
            action_type=AlarmActionType.CREATE_VERIFY_TASK,
            device_id="TH-03",
        )
        self.created_at = datetime(2026, 8, 28, 13, 0)
        self.calls: list[str] = []
        self.connection = sqlite3.connect(":memory:")
        self.recorder = SQLiteAutomationRunRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def _handler(self, action: AlarmAction) -> None:
        self.calls.append(action.action_type.value)

    def test_disabled_skips_and_records_without_handler_call(self) -> None:
        executor = ActionExecutor(
            mode=AutomationMode.DISABLED,
            handlers={AlarmActionType.CREATE_VERIFY_TASK: self._handler},
            recorder=self.recorder,
        )
        executions = executor.execute((self.action,), created_at=self.created_at)
        self.assertEqual(executions[0].status, ActionExecutionStatus.SKIPPED)
        self.assertEqual(self.calls, [])
        row = self.connection.execute(
            "SELECT mode, action_status FROM automation_runs"
        ).fetchone()
        self.assertEqual(tuple(row), ("disabled", "SKIPPED"))

    def test_shadow_plans_without_calling_handler(self) -> None:
        executor = ActionExecutor(
            mode="shadow",
            handlers={AlarmActionType.CREATE_VERIFY_TASK: self._handler},
            recorder=self.recorder,
        )
        executions = executor.execute(
            (self.action,),
            context={
                "sample_time": "2026-08-28T13:00:00",
                "matched": False,
                "difference_type": "STANDARD",
                "details": {"expected": "PENDING"},
            },
            created_at=self.created_at,
        )
        self.assertEqual(executions[0].status, ActionExecutionStatus.PLANNED)
        self.assertEqual(self.calls, [])
        row = self.connection.execute(
            "SELECT matched, difference_type FROM automation_runs"
        ).fetchone()
        self.assertEqual(tuple(row), (0, "STANDARD"))

    def test_active_calls_injected_handler(self) -> None:
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-03",),
            handlers={AlarmActionType.CREATE_VERIFY_TASK: self._handler},
            recorder=self.recorder,
        )
        executions = executor.execute(
            (self.action,),
            context={"device_id": "TH-03"},
            created_at=self.created_at,
        )
        self.assertEqual(executions[0].status, ActionExecutionStatus.SUCCEEDED)
        self.assertEqual(self.calls, ["CREATE_VERIFY_TASK"])

    def test_active_missing_handler_is_audited_as_failure(self) -> None:
        executor = ActionExecutor(
            mode=AutomationMode.ACTIVE,
            active_device_ids=("TH-03",),
            recorder=self.recorder,
        )
        executions = executor.execute(
            (self.action,),
            context={"device_id": "TH-03"},
            created_at=self.created_at,
        )
        self.assertEqual(executions[0].status, ActionExecutionStatus.FAILED)
        self.assertIn("no handler", executions[0].error or "")

    def test_active_context_handler_receives_runtime_context(self) -> None:
        received: list[dict[str, object]] = []

        def context_handler(action, context) -> None:
            self.assertEqual(action.action_type, AlarmActionType.CREATE_VERIFY_TASK)
            received.append(dict(context))

        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-03",),
            context_handlers={
                AlarmActionType.CREATE_VERIFY_TASK: context_handler,
            },
        )
        executions = executor.execute(
            (self.action,),
            context={
                "device_id": "TH-03",
                "sample_time": "2026-08-28T13:00:00",
            },
            created_at=self.created_at,
        )

        self.assertEqual(executions[0].status, ActionExecutionStatus.SUCCEEDED)
        self.assertEqual(
            received,
            [{"device_id": "TH-03", "sample_time": "2026-08-28T13:00:00"}],
        )


if __name__ == "__main__":
    unittest.main()
