from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from application.standard_sync import (
    StandardSyncService,
    StandardSyncStatus,
)
from domain.models import EnvironmentStandard
from repositories.standard_resolver import SQLiteStandardRepository, SQLiteStandardResolver


class _Source:
    def __init__(self, standards: tuple[EnvironmentStandard, ...]) -> None:
        self.standards = standards

    def fetch_standards(self) -> tuple[EnvironmentStandard, ...]:
        return self.standards


class StandardSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.repository = SQLiteStandardRepository(self.connection)
        self.now = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.connection.close()

    def _standard(
        self,
        standard_id: str,
        *,
        device_id: str | None = None,
        priority: int = 0,
    ) -> EnvironmentStandard:
        return EnvironmentStandard(
            standard_id=standard_id,
            revision="Rev.A",
            area="仓库",
            device_id=device_id,
            operation_type=None,
            temperature_min=20.0,
            temperature_max=26.0,
            humidity_min=40.0,
            humidity_max=60.0,
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            effective_to=None,
            source_document="SOP-001",
            clause="5.2.3",
            priority=priority,
        )

    def test_valid_snapshot_is_activated_atomically(self) -> None:
        service = StandardSyncService(
            source=_Source((self._standard("ENV-001"),)),
            repository=self.repository,
            source_name="test",
        )
        report = service.sync(now=self.now)
        self.assertEqual(report.status, StandardSyncStatus.SUCCEEDED)
        resolver = SQLiteStandardResolver(self.repository)
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                timestamp=self.now,
            ).standard_id,
            "ENV-001",
        )

    def test_same_area_different_device_standards_are_activated(self) -> None:
        standards = (
            self._standard("ENV-TH-05", device_id="TH-05"),
            self._standard("ENV-TH-06", device_id="TH-06"),
        )
        report = StandardSyncService(
            source=_Source(standards),
            repository=self.repository,
            source_name="test",
        ).sync(now=self.now)
        self.assertEqual(report.status, StandardSyncStatus.SUCCEEDED)
        resolver = SQLiteStandardResolver(self.repository)
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-05",
                timestamp=self.now,
            ).standard_id,
            "ENV-TH-05",
        )

    def test_invalid_snapshot_keeps_previous_active_standard(self) -> None:
        initial = self._standard("ENV-001")
        self.repository.apply_snapshot((initial,), source="test", synced_at=self.now)
        invalid = (
            self._standard("ENV-002"),
            self._standard("ENV-003"),
        )
        report = StandardSyncService(
            source=_Source(invalid),
            repository=self.repository,
            source_name="test",
        ).sync(now=self.now + timedelta(minutes=1))
        self.assertEqual(report.status, StandardSyncStatus.FAILED)
        self.assertFalse(report.activated)
        selected = SQLiteStandardResolver(self.repository).resolve(
            area_id="仓库",
            operation_type=None,
            timestamp=self.now,
        )
        self.assertEqual(selected.standard_id, "ENV-001")

    def test_source_failure_keeps_previous_active_standard(self) -> None:
        initial = self._standard("ENV-001")
        self.repository.apply_snapshot((initial,), source="test", synced_at=self.now)

        class FailingSource:
            def fetch_standards(self) -> tuple[EnvironmentStandard, ...]:
                raise RuntimeError("temporary Feishu outage")

        report = StandardSyncService(
            source=FailingSource(),
            repository=self.repository,
            source_name="test",
        ).sync(now=self.now + timedelta(minutes=1))
        self.assertEqual(report.status, StandardSyncStatus.FAILED)
        self.assertEqual(
            SQLiteStandardResolver(self.repository)
            .resolve(area_id="仓库", operation_type=None, timestamp=self.now)
            .standard_id,
            "ENV-001",
        )


if __name__ == "__main__":
    unittest.main()
