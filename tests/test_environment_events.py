from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from repositories.environment_events import SQLiteEnvironmentEventRepository


class EnvironmentEventRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.repository = SQLiteEnvironmentEventRepository(self.connection)
        self.opened_at = datetime(2026, 8, 28, 13, 0)

    def tearDown(self) -> None:
        self.connection.close()

    def test_same_device_cannot_create_two_active_events(self) -> None:
        first = self.repository.create_or_get_active(
            device_id="TH-10",
            event_key="TH-10:20260828130000",
            opened_at=self.opened_at,
        )
        second = self.repository.create_or_get_active(
            device_id="TH-10",
            event_key="TH-10:20260828130100",
            opened_at=self.opened_at + timedelta(minutes=1),
        )
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(len(self.repository.list_active(device_id="TH-10")), 1)

    def test_retry_with_same_event_key_is_idempotent(self) -> None:
        first = self.repository.create_or_get_active(
            device_id="TH-03",
            event_key="TH-03:round-1",
            opened_at=self.opened_at,
            payload={"humidity": 71.0},
        )
        second = self.repository.create_or_get_active(
            device_id="TH-03",
            event_key="TH-03:round-1",
            opened_at=self.opened_at + timedelta(minutes=1),
            payload={"humidity": 72.0},
        )
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.payload, {"humidity": 71.0})

    def test_closed_event_allows_next_active_event(self) -> None:
        first = self.repository.create_or_get_active(
            device_id="TH-10",
            event_key="TH-10:round-1",
            opened_at=self.opened_at,
        )
        self.repository.close(first.event_id, closed_at=self.opened_at + timedelta(minutes=5))
        second = self.repository.create_or_get_active(
            device_id="TH-10",
            event_key="TH-10:round-2",
            opened_at=self.opened_at + timedelta(minutes=6),
        )
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertEqual(len(self.repository.list_active(device_id="TH-10")), 1)

    def test_recovered_cycle_keeps_external_record_binding_for_history(self) -> None:
        event = self.repository.create_or_get_active(
            device_id="TH-10",
            event_key="TH-10:round-1",
            opened_at=self.opened_at,
        )
        self.repository.bind_external_record(event.event_id, record_id="rec-A")
        self.repository.mark_recovered(
            event.event_id,
            recovered_at=self.opened_at + timedelta(minutes=5),
        )

        historical = self.repository.get(event.event_id)
        self.assertIsNotNone(historical)
        self.assertEqual(historical.payload["feishu_record_id"], "rec-A")
        self.assertEqual(historical.closed_at, self.opened_at + timedelta(minutes=5))

    def test_two_alarm_cycles_keep_distinct_external_record_bindings(self) -> None:
        first = self.repository.create_or_get_active(
            device_id="TH-01",
            event_key="ENV:TH-01:cycle-A",
            opened_at=self.opened_at,
        )
        self.repository.bind_external_record(first.event_id, record_id="rec-A")
        self.repository.mark_recovered(
            first.event_id,
            recovered_at=self.opened_at + timedelta(minutes=5),
        )
        second = self.repository.create_or_get_active(
            device_id="TH-01",
            event_key="ENV:TH-01:cycle-B",
            opened_at=self.opened_at + timedelta(minutes=6),
        )
        self.repository.bind_external_record(second.event_id, record_id="rec-B")

        self.assertEqual(
            self.repository.get(first.event_id).payload["feishu_record_id"],
            "rec-A",
        )
        self.assertEqual(
            self.repository.get(second.event_id).payload["feishu_record_id"],
            "rec-B",
        )

    def test_two_connections_racing_for_different_events_keep_one_active(self) -> None:
        database_path = Path.cwd() / "events-race-test.sqlite"
        database_path.unlink(missing_ok=True)
        connections = [
            sqlite3.connect(str(database_path), check_same_thread=False, timeout=5)
            for _ in range(2)
        ]
        repositories = [SQLiteEnvironmentEventRepository(connection) for connection in connections]
        barrier = threading.Barrier(2)
        results: list[str] = []
        errors: list[Exception] = []

        def create(event_key: str, repository: SQLiteEnvironmentEventRepository) -> None:
            try:
                barrier.wait(timeout=5)
                record = repository.create_or_get_active(
                    device_id="TH-10",
                    event_key=event_key,
                    opened_at=self.opened_at,
                )
                results.append(record.event_id)
            except Exception as exc:  # pragma: no cover - failure diagnostic
                errors.append(exc)

        threads = [
            threading.Thread(target=create, args=("TH-10:race-1", repositories[0])),
            threading.Thread(target=create, args=("TH-10:race-2", repositories[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        verification_repository = SQLiteEnvironmentEventRepository(connections[0])
        self.assertEqual(
            len(verification_repository.list_active(device_id="TH-10")),
            1,
        )
        for connection in connections:
            connection.close()
        database_path.unlink(missing_ok=True)
