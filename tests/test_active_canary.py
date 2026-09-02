from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest

from application.action_executor import ActionExecutionStatus, ActionExecutor
from application.active_scope import active_scope_allows
from domain.models import AlarmAction, AlarmActionType
from repositories.automation_runs import SQLiteAutomationRunRepository
import sqlite3


class ActiveCanaryTests(unittest.TestCase):
    def _action(self, action_type: AlarmActionType, device_id: str = "TH-10") -> AlarmAction:
        return AlarmAction(action_type=action_type, device_id=device_id)

    def test_shared_active_scope_policy_normalizes_and_fails_closed(self) -> None:
        self.assertTrue(active_scope_allows(" th-10 ", active_device_ids="TH-10"))
        self.assertFalse(active_scope_allows("TH-09", active_device_ids="TH-10"))
        self.assertFalse(active_scope_allows("TH-10", active_device_ids=""))

    def test_allowlisted_device_executes_handler_using_context_scope(self) -> None:
        calls: list[str] = []

        def handler(action: AlarmAction, context) -> None:
            calls.append(context["device_id"])

        executor = ActionExecutor(
            mode="active",
            active_device_ids=" th-10 ",
            context_handlers={AlarmActionType.CREATE_ALARM_EVENT: handler},
        )

        execution = executor.execute(
            (self._action(AlarmActionType.CREATE_ALARM_EVENT),),
            context={"device_id": " th-10 "},
        )[0]

        self.assertEqual(execution.status, ActionExecutionStatus.SUCCEEDED)
        self.assertEqual(calls, [" th-10 "])

    def test_non_allowlisted_device_remains_planned_and_is_audited(self) -> None:
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-10",),
            context_handlers={
                AlarmActionType.CREATE_ALARM_EVENT: lambda action, context: calls.append(
                    context["device_id"]
                )
            },
        )

        execution = executor.execute(
            (self._action(AlarmActionType.CREATE_ALARM_EVENT, "TH-09"),),
            context={"device_id": "TH-09"},
        )[0]

        self.assertEqual(execution.status, ActionExecutionStatus.PLANNED)
        self.assertIn("not in ACTIVE_DEVICE_IDS", execution.error or "")
        self.assertEqual(calls, [])

    def test_allowlisted_context_mismatch_fails_closed(self) -> None:
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-10",),
            handlers={
                AlarmActionType.CREATE_VERIFY_TASK: lambda action: calls.append("called")
            },
        )

        execution = executor.execute(
            (self._action(AlarmActionType.CREATE_VERIFY_TASK, "TH-09"),),
            context={"device_id": "TH-10"},
        )[0]

        self.assertEqual(execution.status, ActionExecutionStatus.FAILED)
        self.assertIn("scope mismatch", execution.error or "")
        self.assertIn("refusing external write", execution.error or "")
        self.assertEqual(calls, [])

    def test_empty_allowlist_fails_closed_without_handler_call(self) -> None:
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=(),
            handlers={AlarmActionType.CREATE_VERIFY_TASK: lambda action: calls.append("called")},
        )

        execution = executor.execute(
            (self._action(AlarmActionType.CREATE_VERIFY_TASK),),
            context={"device_id": "TH-10"},
        )[0]

        self.assertEqual(execution.status, ActionExecutionStatus.PLANNED)
        self.assertIn("ACTIVE_DEVICE_IDS is empty", execution.error or "")
        self.assertEqual(calls, [])

    def test_active_action_without_context_device_id_fails_closed_and_is_recorded(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        recorder = SQLiteAutomationRunRepository(connection)
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-10",),
            handlers={AlarmActionType.CREATE_VERIFY_TASK: lambda action: calls.append("called")},
            recorder=recorder,
        )

        execution = executor.execute(
            (self._action(AlarmActionType.CREATE_VERIFY_TASK),),
            context={},
        )[0]

        self.assertEqual(execution.status, ActionExecutionStatus.FAILED)
        self.assertIn("context.device_id", execution.error or "")
        self.assertIn("refusing external write", execution.error or "")
        self.assertEqual(calls, [])
        row = connection.execute(
            "SELECT action_status, error FROM automation_runs"
        ).fetchone()
        self.assertEqual(tuple(row), ("FAILED", execution.error))

    def test_all_business_actions_share_the_same_gate(self) -> None:
        action_types = (
            AlarmActionType.CREATE_ALARM_EVENT,
            AlarmActionType.UPDATE_ALARM_EVENT,
            AlarmActionType.START_RECOVERY,
            AlarmActionType.CLOSE_ALARM_EVENT,
        )
        calls: list[AlarmActionType] = []

        def handler(action: AlarmAction, context) -> None:
            calls.append(action.action_type)

        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-10",),
            context_handlers={action_type: handler for action_type in action_types},
        )

        executions = executor.execute(
            tuple(self._action(action_type) for action_type in action_types),
            context={"device_id": "TH-10"},
        )

        self.assertEqual(
            [execution.status for execution in executions],
            [ActionExecutionStatus.SUCCEEDED] * len(action_types),
        )
        self.assertEqual(calls, list(action_types))

    def test_shadow_mode_does_not_apply_active_gate(self) -> None:
        calls: list[str] = []
        executor = ActionExecutor(
            mode="shadow",
            active_device_ids=(),
            handlers={AlarmActionType.CREATE_VERIFY_TASK: lambda action: calls.append("called")},
        )

        execution = executor.execute(
            (self._action(AlarmActionType.CREATE_VERIFY_TASK),),
            context={"device_id": "TH-09"},
        )[0]

        self.assertEqual(execution.status, ActionExecutionStatus.PLANNED)
        self.assertIsNone(execution.error)
        self.assertEqual(calls, [])

    def test_two_concurrent_device_scopes_do_not_cross(self) -> None:
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-09", "TH-10"),
            handlers={
                AlarmActionType.CREATE_VERIFY_TASK: lambda action: calls.append(
                    action.device_id
                )
            },
        )

        def run(device_id: str):
            return executor.execute(
                (self._action(AlarmActionType.CREATE_VERIFY_TASK, device_id),),
                context={"device_id": device_id},
            )[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            executions = list(pool.map(run, ("TH-09", "TH-10")))

        self.assertEqual(
            [execution.status for execution in executions],
            [ActionExecutionStatus.SUCCEEDED, ActionExecutionStatus.SUCCEEDED],
        )
        self.assertEqual(sorted(calls), ["TH-09", "TH-10"])


if __name__ == "__main__":
    unittest.main()
