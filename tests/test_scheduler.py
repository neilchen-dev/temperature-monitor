from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta

from repositories.automation_tasks import SQLiteAutomationTaskRepository, TaskStateError
from scheduler.worker import TaskScheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.repository = SQLiteAutomationTaskRepository(self.connection)
        self.now = datetime(2026, 8, 28, 13, 0)

    def tearDown(self) -> None:
        self.connection.close()

    def _create(self, key: str, *, due_at: datetime | None = None) -> str:
        task = self.repository.create_or_get(
            task_type="VERIFY_ALARM",
            entity_type="DEVICE",
            entity_id="TH-03",
            due_at=due_at or self.now,
            dedupe_key=key,
            created_at=self.now,
        )
        return task.task_id

    def test_scheduler_dispatches_and_finishes_task(self) -> None:
        task_id = self._create("task-1")
        handled: list[str] = []
        scheduler = TaskScheduler(
            repository=self.repository,
            handlers={"VERIFY_ALARM": lambda task: handled.append(task.task_id)},
            worker_id="worker-a",
        )
        report = scheduler.run_once(now=self.now)
        self.assertEqual(report.claimed, 1)
        self.assertEqual(report.succeeded, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(handled, [task_id])

    def test_handler_failure_is_persisted(self) -> None:
        task_id = self._create("task-2")
        scheduler = TaskScheduler(
            repository=self.repository,
            handlers={"VERIFY_ALARM": lambda task: (_ for _ in ()).throw(
                RuntimeError("verification failed")
            )},
            worker_id="worker-a",
        )
        report = scheduler.run_once(now=self.now)
        self.assertEqual(report.failed, 1)
        task = self.repository.get(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.last_error, "verification failed")

    def test_expired_lease_can_be_reclaimed_by_another_worker(self) -> None:
        task_id = self._create("task-3")
        claimed = self.repository.claim_due(
            now=self.now,
            worker_id="worker-a",
            lease_for=timedelta(minutes=1),
        )
        self.assertEqual(claimed[0].task_id, task_id)
        with self.assertRaises(TaskStateError):
            self.repository.mark_succeeded(
                task_id,
                finished_at=self.now + timedelta(minutes=2),
                worker_id="worker-a",
            )
        reclaimed = self.repository.claim_due(
            now=self.now + timedelta(minutes=1),
            worker_id="worker-b",
            lease_for=timedelta(minutes=1),
        )
        self.assertEqual(reclaimed[0].worker_id, "worker-b")


if __name__ == "__main__":
    unittest.main()
