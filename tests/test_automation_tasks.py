from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta

from domain.models import AutomationTaskStatus
from repositories.automation_tasks import SQLiteAutomationTaskRepository, TaskStateError


class AutomationTaskRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.repository = SQLiteAutomationTaskRepository(self.connection)
        self.created_at = datetime(2026, 8, 28, 13, 0)

    def tearDown(self) -> None:
        self.connection.close()

    def test_dedupe_key_returns_original_task(self) -> None:
        first = self.repository.create_or_get(
            task_type="VERIFY_ALARM",
            entity_type="DEVICE",
            entity_id="TH-03",
            due_at=self.created_at + timedelta(minutes=5),
            payload={"temperature": 27.0},
            dedupe_key="VERIFY_ALARM:TH-03:20260828130000",
            created_at=self.created_at,
        )
        second = self.repository.create_or_get(
            task_type="VERIFY_ALARM",
            entity_type="DEVICE",
            entity_id="TH-03",
            due_at=self.created_at + timedelta(minutes=6),
            payload={"temperature": 28.0},
            dedupe_key="VERIFY_ALARM:TH-03:20260828130000",
            created_at=self.created_at + timedelta(minutes=1),
        )
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(second.payload, {"temperature": 27.0})

    def test_claim_due_and_complete(self) -> None:
        task = self.repository.create_or_get(
            task_type="VERIFY_ALARM",
            entity_type="DEVICE",
            entity_id="TH-03",
            due_at=self.created_at + timedelta(minutes=5),
            dedupe_key="task-1",
            created_at=self.created_at,
        )
        self.assertEqual(self.repository.claim_due(now=self.created_at), ())
        claimed = self.repository.claim_due(
            now=self.created_at + timedelta(minutes=5)
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].task_id, task.task_id)
        self.assertEqual(claimed[0].status, AutomationTaskStatus.RUNNING)
        self.assertEqual(claimed[0].attempt_count, 1)

        finished = self.repository.mark_succeeded(
            task.task_id,
            finished_at=self.created_at + timedelta(minutes=5, seconds=1),
        )
        self.assertEqual(finished.status, AutomationTaskStatus.SUCCEEDED)

    def test_cancel_and_failure_require_active_task(self) -> None:
        task = self.repository.create_or_get(
            task_type="VERIFY_ALARM",
            entity_type="DEVICE",
            entity_id="TH-03",
            due_at=self.created_at,
            dedupe_key="task-2",
            created_at=self.created_at,
        )
        cancelled = self.repository.cancel(task.task_id, updated_at=self.created_at)
        self.assertEqual(cancelled.status, AutomationTaskStatus.CANCELLED)
        with self.assertRaises(TaskStateError):
            self.repository.mark_failed(
                task.task_id,
                finished_at=self.created_at,
                error="must not run",
            )


if __name__ == "__main__":
    unittest.main()
