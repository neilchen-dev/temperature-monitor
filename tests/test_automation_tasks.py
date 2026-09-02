from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import uuid

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


class GlobalSyncTaskDeduplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.repository = SQLiteAutomationTaskRepository(self.connection)
        self.now = datetime(2026, 9, 2, 2, 16)

    def tearDown(self) -> None:
        self.connection.close()

    def _schedule(
        self,
        *,
        task_type: str = "SYNC_STANDARD",
        entity_id: str = "standards",
        due_at: datetime | None = None,
        repository: SQLiteAutomationTaskRepository | None = None,
    ):
        return (repository or self.repository).create_or_get_unfinished(
            task_type=task_type,
            entity_type="RUNTIME",
            entity_id=entity_id,
            due_at=due_at or self.now,
            payload={"runtime_worker_id": "test-worker"},
            dedupe_key=f"RUNTIME:{task_type}:{entity_id}",
            created_at=self.now,
        )

    def _unfinished_count(self, task_type: str, entity_id: str) -> int:
        return self.connection.execute(
            """
            SELECT COUNT(*) FROM automation_tasks
            WHERE task_type = ? AND entity_id = ?
              AND status IN ('PENDING', 'RUNNING')
            """,
            (task_type, entity_id),
        ).fetchone()[0]

    def test_first_schedule_creates_and_second_reuses_despite_different_due_at(self) -> None:
        first = self._schedule(due_at=self.now + timedelta(minutes=10))
        second = self._schedule(due_at=self.now + timedelta(minutes=20))

        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(second.dedupe_key, "RUNTIME:SYNC_STANDARD:standards")
        self.assertEqual(self._unfinished_count("SYNC_STANDARD", "standards"), 1)

    def test_earlier_request_advances_pending_task(self) -> None:
        self._schedule(due_at=self.now + timedelta(minutes=20))
        task = self._schedule(due_at=self.now + timedelta(minutes=5))

        self.assertEqual(task.due_at, self.now + timedelta(minutes=5))

    def test_later_request_never_delays_pending_task(self) -> None:
        self._schedule(due_at=self.now + timedelta(minutes=5))
        task = self._schedule(due_at=self.now + timedelta(minutes=20))

        self.assertEqual(task.due_at, self.now + timedelta(minutes=5))

    def test_running_task_is_reused_without_parallel_pending_task(self) -> None:
        created = self._schedule()
        claimed = self.repository.claim_due(now=self.now, worker_id="worker-a")[0]
        reused = self._schedule(due_at=self.now + timedelta(minutes=5))

        self.assertEqual(created.task_id, claimed.task_id)
        self.assertEqual(reused.task_id, claimed.task_id)
        self.assertEqual(reused.status, AutomationTaskStatus.RUNNING)
        self.assertEqual(self._unfinished_count("SYNC_STANDARD", "standards"), 1)

    def test_terminal_history_does_not_block_later_cycles(self) -> None:
        first = self._schedule()
        self.repository.claim_due(now=self.now, worker_id="worker-a")
        self.repository.mark_succeeded(
            first.task_id,
            finished_at=self.now + timedelta(seconds=1),
            worker_id="worker-a",
        )

        second = self._schedule(due_at=self.now + timedelta(minutes=10))
        self.repository.cancel(
            second.task_id,
            updated_at=self.now + timedelta(seconds=2),
        )
        third = self._schedule()
        self.repository.claim_due(now=self.now, worker_id="worker-a")
        self.repository.mark_failed(
            third.task_id,
            finished_at=self.now + timedelta(seconds=3),
            error="test failure",
            worker_id="worker-a",
        )
        fourth = self._schedule(due_at=self.now + timedelta(minutes=20))

        self.assertNotEqual(first.task_id, second.task_id)
        self.assertNotEqual(second.task_id, third.task_id)
        self.assertNotEqual(third.task_id, fourth.task_id)
        self.assertEqual(fourth.status, AutomationTaskStatus.PENDING)
        self.assertEqual(fourth.dedupe_key, "RUNTIME:SYNC_STANDARD:standards")

    def test_standard_and_operation_sync_are_independent(self) -> None:
        standard = self._schedule()
        operations = self._schedule(
            task_type="SYNC_OPERATIONS",
            entity_id="operations",
        )

        self.assertNotEqual(standard.task_id, operations.task_id)
        self.assertEqual(self._unfinished_count("SYNC_STANDARD", "standards"), 1)
        self.assertEqual(self._unfinished_count("SYNC_OPERATIONS", "operations"), 1)

    def test_legacy_pending_duplicates_are_consolidated(self) -> None:
        for offset in (1, 2, 3):
            self.repository.create_or_get(
                task_type="SYNC_OPERATIONS",
                entity_type="RUNTIME",
                entity_id="operations",
                due_at=self.now + timedelta(seconds=offset),
                dedupe_key=(
                    "RUNTIME:SYNC_OPERATIONS:"
                    f"{(self.now + timedelta(seconds=offset)).isoformat()}"
                ),
                created_at=self.now,
            )

        winner = self._schedule(
            task_type="SYNC_OPERATIONS",
            entity_id="operations",
            due_at=self.now,
        )

        self.assertEqual(winner.status, AutomationTaskStatus.PENDING)
        self.assertEqual(winner.due_at, self.now)
        self.assertEqual(self._unfinished_count("SYNC_OPERATIONS", "operations"), 1)
        cancelled = self.connection.execute(
            "SELECT COUNT(*) FROM automation_tasks WHERE status = 'CANCELLED'"
        ).fetchone()[0]
        self.assertEqual(cancelled, 2)

    def test_scheduler_restart_reuses_persisted_unfinished_task(self) -> None:
        database_path = Path.cwd() / f"sync-restart-{uuid.uuid4().hex}.sqlite"
        first_connection = sqlite3.connect(str(database_path), timeout=5)
        first_repository = SQLiteAutomationTaskRepository(first_connection)
        first = self._schedule(repository=first_repository)
        first_connection.close()
        try:
            second_connection = sqlite3.connect(str(database_path), timeout=5)
            second_repository = SQLiteAutomationTaskRepository(second_connection)
            second = self._schedule(
                due_at=self.now + timedelta(minutes=5),
                repository=second_repository,
            )
            self.assertEqual(first.task_id, second.task_id)
            second_connection.close()
        finally:
            database_path.unlink(missing_ok=True)

    def test_two_threads_create_only_one_pending_for_each_global_sync(self) -> None:
        for task_type, entity_id in (
            ("SYNC_STANDARD", "standards"),
            ("SYNC_OPERATIONS", "operations"),
        ):
            with self.subTest(task_type=task_type):
                database_path = Path.cwd() / f"sync-race-{uuid.uuid4().hex}.sqlite"
                connections = [
                    sqlite3.connect(
                        str(database_path),
                        check_same_thread=False,
                        timeout=5,
                    )
                    for _ in range(2)
                ]
                repositories = [
                    SQLiteAutomationTaskRepository(connection)
                    for connection in connections
                ]
                barrier = threading.Barrier(2)
                results: list[str] = []
                errors: list[Exception] = []

                def schedule(repository: SQLiteAutomationTaskRepository) -> None:
                    try:
                        barrier.wait(timeout=5)
                        task = self._schedule(
                            task_type=task_type,
                            entity_id=entity_id,
                            repository=repository,
                        )
                        results.append(task.task_id)
                    except Exception as exc:  # pragma: no cover - diagnostic
                        errors.append(exc)

                threads = [
                    threading.Thread(target=schedule, args=(repository,))
                    for repository in repositories
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

                try:
                    self.assertEqual(errors, [])
                    self.assertEqual(len(set(results)), 1)
                    count = connections[0].execute(
                        """
                        SELECT COUNT(*) FROM automation_tasks
                        WHERE task_type = ? AND entity_id = ?
                          AND status IN ('PENDING', 'RUNNING')
                        """,
                        (task_type, entity_id),
                    ).fetchone()[0]
                    self.assertEqual(count, 1)
                finally:
                    for connection in connections:
                        connection.close()
                    database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
