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

    def test_health_summary_excludes_historical_terminal_failures_from_readiness(self) -> None:
        historical = self.repository.create_or_get(
            task_type="SHADOW_COMPARE",
            entity_type="DEVICE",
            entity_id="TH-03",
            due_at=self.created_at,
            payload={"expected": {"device_id": "TH-03"}},
            dedupe_key="SHADOW_COMPARE:historical",
            created_at=self.created_at,
        )
        self.repository.claim_due(now=self.created_at, worker_id="worker-a")
        self.repository.mark_failed(
            historical.task_id,
            finished_at=self.created_at + timedelta(seconds=1),
            error="historical Shadow comparison failure",
            worker_id="worker-a",
        )
        boundary = self.created_at + timedelta(minutes=10)
        self.repository.set_runtime_context(mode="shadow", now=boundary)

        summary = self.repository.active_readiness(now=boundary)

        self.assertEqual(summary["failed_tasks_total"], 1)
        self.assertEqual(summary["historical_failed_tasks"], 1)
        self.assertEqual(summary["current_failed_tasks"], 0)
        self.assertEqual(summary["current_epoch_failed_tasks"], 0)
        self.assertEqual(summary["retryable_failed_tasks"], 0)
        self.assertEqual(summary["claimable_failed_tasks"], 0)
        self.assertEqual(summary["blocker_reasons"], [])
        self.assertTrue(summary["active_readiness"])

    def test_health_summary_blocks_current_epoch_retryable_failure(self) -> None:
        activation = self.repository.set_runtime_context(
            mode="active", now=self.created_at
        )
        task = self.repository.create_or_get(
            task_type="NOTIFY_ALARM",
            entity_type="EVENT",
            entity_id="event-current",
            due_at=self.created_at,
            payload={"event_id": "event-current", "retry_attempt": 1},
            dedupe_key="NOTIFY_ALARM:event-current",
            created_at=self.created_at,
        )
        self.repository.claim_due(now=self.created_at, worker_id="worker-a")
        self.repository.mark_failed(
            task.task_id,
            finished_at=self.created_at + timedelta(seconds=1),
            error="temporary Feishu failure exhausted retries",
            worker_id="worker-a",
        )

        summary = self.repository.active_readiness(
            now=self.created_at + timedelta(seconds=2)
        )

        self.assertEqual(summary["active_epoch"], activation.active_epoch)
        self.assertEqual(summary["current_failed_tasks"], 1)
        self.assertEqual(summary["current_epoch_failed_tasks"], 1)
        self.assertEqual(summary["retryable_failed_tasks"], 1)
        self.assertEqual(summary["claimable_failed_tasks"], 0)
        self.assertIn("current_failed_tasks=1", summary["blocker_reasons"])
        self.assertFalse(summary["active_readiness"])

    def test_health_summary_treats_legacy_pending_as_audit_only(self) -> None:
        self.repository.set_runtime_context(mode="shadow", now=self.created_at)
        task = self.repository.create_or_get(
            task_type="NOTIFY_ALARM",
            entity_type="EVENT",
            entity_id="event-legacy",
            due_at=self.created_at,
            payload={"event_id": "event-legacy"},
            dedupe_key="NOTIFY_ALARM:event-legacy",
            created_at=self.created_at,
        )
        activation = self.repository.set_runtime_context(
            mode="active", now=self.created_at + timedelta(minutes=1)
        )
        self.assertEqual(
            self.repository.quarantine_legacy_external_tasks(
                now=self.created_at + timedelta(minutes=1),
                active_epoch=activation.active_epoch,
            ),
            1,
        )

        summary = self.repository.active_readiness(
            now=self.created_at + timedelta(minutes=1)
        )

        self.assertEqual(summary["legacy_pending_tasks"], 1)
        self.assertEqual(summary["pending_external_effects"], 0)
        self.assertEqual(summary["claimable_failed_tasks"], 0)
        self.assertEqual(summary["blocker_reasons"], [])
        self.assertTrue(summary["active_readiness"])
        self.assertEqual(self.repository.claim_due(now=self.created_at + timedelta(minutes=1)), ())
        self.assertEqual(
            self.repository.get(task.task_id).status,
            AutomationTaskStatus.LEGACY_PENDING,
        )

    def test_health_summary_blocks_stale_running_external_task(self) -> None:
        self.repository.set_runtime_context(mode="active", now=self.created_at)
        task = self.repository.create_or_get(
            task_type="NOTIFY_ALARM",
            entity_type="EVENT",
            entity_id="event-stale",
            due_at=self.created_at,
            payload={"event_id": "event-stale"},
            dedupe_key="NOTIFY_ALARM:event-stale",
            created_at=self.created_at,
        )
        self.repository.claim_due(
            now=self.created_at,
            worker_id="worker-a",
            lease_for=timedelta(seconds=1),
        )

        summary = self.repository.active_readiness(
            now=self.created_at + timedelta(seconds=2)
        )

        self.assertEqual(summary["pending_external_effects"], 1)
        self.assertEqual(summary["current_epoch_pending_external_effects"], 1)
        self.assertEqual(summary["historical_pending_external_effects"], 0)
        self.assertEqual(summary["unisolated_external_effects"], 0)
        self.assertEqual(summary["stale_tasks"], 1)
        self.assertNotIn("historical_pending_external_effects=1", summary["blocker_reasons"])
        self.assertIn("stale_tasks=1", summary["blocker_reasons"])
        self.assertFalse(summary["active_readiness"])
        self.assertEqual(self.repository.get(task.task_id).status, AutomationTaskStatus.RUNNING)

    def test_current_epoch_pending_external_task_does_not_block_readiness(self) -> None:
        activation = self.repository.set_runtime_context(
            mode="active", now=self.created_at
        )
        task = self.repository.create_or_get(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="EVENT",
            entity_id="event-current",
            due_at=self.created_at,
            payload={"event_id": "event-current"},
            dedupe_key="RECONCILE_ALARM_EVENT:event-current",
            created_at=self.created_at,
        )

        summary = self.repository.active_readiness(
            now=self.created_at + timedelta(seconds=1)
        )

        self.assertEqual(task.active_epoch, activation.active_epoch)
        self.assertEqual(summary["pending_external_effects"], 1)
        self.assertEqual(summary["current_epoch_pending_external_effects"], 1)
        self.assertEqual(summary["historical_pending_external_effects"], 0)
        self.assertEqual(summary["unisolated_external_effects"], 0)
        self.assertEqual(summary["blocker_reasons"], [])
        self.assertTrue(summary["active_readiness"])

    def test_historical_pending_external_task_blocks_active_readiness(self) -> None:
        self.repository.set_runtime_context(mode="shadow", now=self.created_at)
        task = self.repository.create_or_get(
            task_type="NOTIFY_ALARM",
            entity_type="EVENT",
            entity_id="event-historical",
            due_at=self.created_at,
            payload={"event_id": "event-historical"},
            dedupe_key="NOTIFY_ALARM:event-historical",
            created_at=self.created_at,
        )
        activation = self.repository.set_runtime_context(
            mode="active", now=self.created_at + timedelta(minutes=1)
        )

        summary = self.repository.active_readiness(
            now=self.created_at + timedelta(minutes=1)
        )

        self.assertNotEqual(task.active_epoch, activation.active_epoch)
        self.assertEqual(summary["pending_external_effects"], 1)
        self.assertEqual(summary["current_epoch_pending_external_effects"], 0)
        self.assertEqual(summary["historical_pending_external_effects"], 1)
        self.assertEqual(summary["unisolated_external_effects"], 1)
        self.assertIn(
            "historical_pending_external_effects=1", summary["blocker_reasons"]
        )
        self.assertFalse(summary["active_readiness"])

    def test_same_epoch_task_before_cutover_is_not_allowed(self) -> None:
        activation = self.repository.set_runtime_context(
            mode="active", now=self.created_at + timedelta(minutes=1)
        )
        task = self.repository.create_or_get(
            task_type="NOTIFY_ALARM",
            entity_type="EVENT",
            entity_id="event-before-cutover",
            due_at=self.created_at,
            payload={"event_id": "event-before-cutover"},
            dedupe_key="NOTIFY_ALARM:event-before-cutover",
            # Simulate a malformed/imported row that reuses the current epoch.
            created_at=self.created_at,
        )

        self.assertEqual(task.active_epoch, activation.active_epoch)
        self.assertFalse(self.repository.external_effect_allowed(task))
        self.assertEqual(
            self.repository.quarantine_legacy_external_tasks(
                now=self.created_at + timedelta(minutes=1),
                active_epoch=activation.active_epoch,
            ),
            1,
        )
        self.assertEqual(
            self.repository.get(task.task_id).status,
            AutomationTaskStatus.LEGACY_PENDING,
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

    def test_reconcile_tasks_are_deduped_per_alarm_cycle_not_per_device(self) -> None:
        first = self.repository.create_or_get_unfinished(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id="TH-01",
            due_at=self.now,
            payload={"local_event_id": "event-A"},
            dedupe_key="RECONCILE_ALARM_EVENT:TH-01:cycle-A",
            created_at=self.now,
        )
        second = self.repository.create_or_get_unfinished(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id="TH-01",
            due_at=self.now,
            payload={"local_event_id": "event-B"},
            dedupe_key="RECONCILE_ALARM_EVENT:TH-01:cycle-B",
            created_at=self.now,
        )

        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(
            {
                row[0]
                for row in self.connection.execute(
                    """
                    SELECT dedupe_key FROM automation_tasks
                    WHERE task_type = 'RECONCILE_ALARM_EVENT'
                      AND status = 'PENDING'
                    """
                )
            },
            {
                "RECONCILE_ALARM_EVENT:TH-01:cycle-A",
                "RECONCILE_ALARM_EVENT:TH-01:cycle-B",
            },
        )

    def test_reconcile_retry_backoff_is_not_reset_by_rearm(self) -> None:
        base_key = "RECONCILE_ALARM_EVENT:TH-01:cycle-A"
        base = self.repository.create_or_get_unfinished(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id="TH-01",
            due_at=self.now,
            payload={"local_event_id": "event-A", "retry_attempt": 0},
            dedupe_key=base_key,
            created_at=self.now,
        )
        self.repository.claim_due(now=self.now, worker_id="worker-a")
        self.repository.mark_failed(
            base.task_id,
            finished_at=self.now + timedelta(seconds=1),
            error="temporary Feishu failure",
            worker_id="worker-a",
        )
        retry_at = self.now + timedelta(minutes=10)
        retry = self.repository.create_or_get(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id="TH-01",
            due_at=retry_at,
            payload={"local_event_id": "event-A", "retry_attempt": 1},
            dedupe_key=f"{base_key}:retry:1",
            created_at=self.now + timedelta(seconds=1),
        )

        rearmed = self.repository.create_or_get_unfinished(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id="TH-01",
            due_at=self.now + timedelta(seconds=2),
            payload={"local_event_id": "event-A", "retry_attempt": 0},
            dedupe_key=base_key,
            created_at=self.now + timedelta(seconds=2),
        )

        self.assertEqual(rearmed.task_id, retry.task_id)
        self.assertEqual(rearmed.due_at, retry_at)
        self.assertEqual(
            self._unfinished_count("RECONCILE_ALARM_EVENT", "TH-01"),
            1,
        )

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
