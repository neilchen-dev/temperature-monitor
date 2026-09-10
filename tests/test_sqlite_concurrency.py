from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from application.shadow import AutomationDiff
from domain.models import EnvironmentStandard
from repositories.automation_runs import SQLiteAutomationRunRepository
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.standard_resolver import SQLiteStandardRepository
from repositories.sqlite import connect
from scheduler.worker import TaskScheduler


class _FlakyConnection(sqlite3.Connection):
    """Inject one transient lock at a chosen SQL statement."""

    fail_once_contains: str | None = None

    def execute(self, sql, parameters=()):  # type: ignore[no-untyped-def]
        normalized = " ".join(str(sql).split())
        marker = self.fail_once_contains
        if marker and marker in normalized:
            self.fail_once_contains = None
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, parameters)


class _FlakyTaskRepository:
    def __init__(self) -> None:
        self.calls = 0

    def claim_due(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return ()


class _StopAfter:
    def __init__(self, count: int) -> None:
        self.count = count
        self.wait_calls = 0

    def wait(self, timeout: float) -> bool:
        self.wait_calls += 1
        return self.wait_calls >= self.count


class SQLiteConcurrencyTests(unittest.TestCase):
    def test_eleven_devices_write_tasks_events_runs_and_snapshot_concurrently(self) -> None:
        """The Runtime and audit tables remain consistent under 11 writers."""
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory(prefix="temperature-monitor-sqlite-") as temp_dir:
            database = Path(temp_dir) / "runtime.sqlite3"
            connections = [connect(database) for _ in range(11)]
            repositories = [
                (
                    SQLiteAutomationTaskRepository(connection),
                    SQLiteEnvironmentEventRepository(connection),
                    SQLiteAutomationRunRepository(connection),
                    SQLiteStandardRepository(connection),
                )
                for connection in connections
            ]
            standard = EnvironmentStandard(
                standard_id="STRESS-LOGICAL",
                revision="R1",
                area="stress-area",
                device_id="TH-01",
                operation_type=None,
                control_type="ALL_DAY",
                temperature_min=20,
                temperature_max=26,
                humidity_min=40,
                humidity_max=60,
                effective_from=now,
                effective_to=None,
                source_document="stress",
                clause="1",
                priority=1,
                standard_source="feishu:test",
            )
            barrier = threading.Barrier(11)
            errors: list[BaseException] = []
            errors_lock = threading.Lock()

            def writer(index: int) -> None:
                device_id = f"TH-{index + 1:02d}"
                tasks, events, runs, standards = repositories[index]
                try:
                    barrier.wait(timeout=5)
                    tasks.create_or_get(
                        task_type="SHADOW_COMPARE",
                        entity_type="DEVICE",
                        entity_id=device_id,
                        due_at=now,
                        payload={"device_id": device_id},
                        dedupe_key=f"stress-task:{device_id}",
                        created_at=now,
                    )
                    events.create_or_get_active(
                        device_id=device_id,
                        event_key=f"ENV:{device_id}:{now.isoformat()}",
                        opened_at=now,
                        payload={"device_id": device_id},
                    )
                    runs.record_comparison(
                        device_id=device_id,
                        sample_time=now,
                        expected={"device_id": device_id},
                        observed={"device_id": device_id},
                        diff=AutomationDiff(True, (), {}),
                        created_at=now,
                    )
                    standards.apply_snapshot(
                        (standard,), source="feishu:test", synced_at=now
                    )
                    # Exercise the same status/readiness reads that run next
                    # to writers in production.
                    tasks.health_summary(now=now)
                    standards.readiness(expected_device_ids=("TH-01",))
                except BaseException as error:  # pragma: no cover - assertion below
                    with errors_lock:
                        errors.append(error)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(11)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            connection = connections[0]
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM automation_tasks").fetchone()[0],
                11,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM environment_events").fetchone()[0],
                11,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0],
                11,
            )
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )
            for connection in connections:
                connection.close()

    def test_transient_lock_rolls_back_and_retries_task_finalize(self) -> None:
        connection = sqlite3.connect(
            ":memory:", check_same_thread=False, factory=_FlakyConnection
        )
        repository = SQLiteAutomationTaskRepository(connection)
        now = datetime(2026, 9, 10, 12, 0)
        task = repository.create_or_get(
            task_type="VERIFY_ALARM",
            entity_type="DEVICE",
            entity_id="TH-01",
            due_at=now,
            dedupe_key="flaky-finalize",
            created_at=now,
        )
        repository.claim_due(now=now, worker_id="test", lease_for=timedelta(minutes=5))
        connection.fail_once_contains = "UPDATE automation_tasks SET status = ?"

        finished = repository.mark_succeeded(
            task.task_id, finished_at=now, worker_id="test"
        )

        self.assertEqual(finished.status.value, "SUCCEEDED")
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        connection.close()

    def test_scheduler_run_forever_survives_transient_lock(self) -> None:
        repository = _FlakyTaskRepository()
        scheduler = TaskScheduler(
            repository=repository,  # type: ignore[arg-type]
            handlers={},
            poll_interval=0.001,
        )
        stop_event = _StopAfter(2)

        scheduler.run_forever(stop_event)

        self.assertEqual(repository.calls, 2)
        self.assertEqual(stop_event.wait_calls, 2)


if __name__ == "__main__":
    unittest.main()
