from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from application.standard_sync import (
    StandardSyncService,
    StandardSyncStatus,
)
from domain.models import ControlType, EnvironmentStandard
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
            device_id=device_id or "TH-01",
            operation_type=None,
            control_type=ControlType.ALL_DAY,
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
            source_name="feishu:test",
        )
        report = service.sync(now=self.now)
        self.assertEqual(report.status, StandardSyncStatus.SUCCEEDED)
        resolver = SQLiteStandardResolver(self.repository)
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-01",
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
            source_name="feishu:test",
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

    def test_same_logical_standard_revisions_can_overlap_and_latest_wins(self) -> None:
        old = self._standard("ENV-TH-02", device_id="TH-02")
        new = replace(
            old,
            revision="Rev.B",
            humidity_max=50.0,
            effective_from=self.now + timedelta(minutes=1),
        )
        source = _Source((old,))
        service = StandardSyncService(
            source=source,
            repository=self.repository,
            source_name="feishu:test",
        )

        self.assertEqual(service.sync(now=self.now).status, StandardSyncStatus.SUCCEEDED)
        source.standards = (old, new)
        report = service.sync(now=self.now + timedelta(minutes=2))

        self.assertEqual(report.status, StandardSyncStatus.SUCCEEDED)
        resolver = SQLiteStandardResolver(self.repository)
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-02",
                timestamp=self.now,
            ).revision,
            "Rev.A",
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-02",
                timestamp=self.now + timedelta(minutes=1),
            ).revision,
            "Rev.B",
        )
        self.assertEqual(
            self.repository.connection.execute(
                "SELECT COUNT(*) FROM standard_snapshot_members"
            ).fetchone()[0],
            3,
        )
        snapshots = self.repository.connection.execute(
            "SELECT snapshot_id FROM standard_snapshots ORDER BY synced_at, snapshot_id"
        ).fetchall()
        self.assertEqual(
            self.repository.connection.execute(
                "SELECT COUNT(*) FROM standard_snapshot_members WHERE snapshot_id = ?",
                (snapshots[0][0],),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.repository.connection.execute(
                "SELECT COUNT(*) FROM standard_snapshot_members WHERE snapshot_id = ?",
                (snapshots[-1][0],),
            ).fetchone()[0],
            2,
        )

    def test_revision_chain_can_change_priority_without_becoming_independent(self) -> None:
        old = self._standard("ENV-TH-01", priority=100)
        new = replace(
            old,
            revision="Rev.B",
            priority=101,
            effective_from=self.now + timedelta(minutes=1),
        )
        report = StandardSyncService(
            source=_Source((old, new)),
            repository=self.repository,
            source_name="feishu:test",
        ).sync(now=self.now + timedelta(minutes=2))

        self.assertEqual(report.status, StandardSyncStatus.SUCCEEDED)
        selected = SQLiteStandardResolver(self.repository).resolve(
            area_id="仓库",
            operation_type=None,
            device_id="TH-01",
            timestamp=self.now + timedelta(minutes=1),
        )
        self.assertEqual((selected.standard_id, selected.revision), ("ENV-TH-01", "Rev.B"))
        self.assertEqual(selected.priority, 101)

    def test_same_logical_standard_revision_with_same_start_is_rejected(self) -> None:
        old = self._standard("ENV-TH-02", device_id="TH-02")
        same_start = replace(old, revision="Rev.B", humidity_max=50.0)
        report = StandardSyncService(
            source=_Source((old, same_start)),
            repository=self.repository,
            source_name="feishu:test",
        ).sync(now=self.now)

        self.assertEqual(report.status, StandardSyncStatus.FAILED)
        self.assertTrue(
            any("same effective_from" in error for error in report.errors)
        )

    def test_same_logical_standard_selector_change_with_overlap_is_rejected(self) -> None:
        old = self._standard("ENV-TH-02", device_id="TH-02")
        changed_selector = replace(
            old,
            revision="Rev.B",
            area="另一仓库",
            humidity_max=50.0,
        )
        report = StandardSyncService(
            source=_Source((old, changed_selector)),
            repository=self.repository,
            source_name="feishu:test",
        ).sync(now=self.now)

        self.assertEqual(report.status, StandardSyncStatus.FAILED)
        self.assertTrue(
            any("incompatible selectors" in error for error in report.errors)
        )

    def test_multiple_devices_have_independent_revision_chains(self) -> None:
        th02_old = self._standard("ENV-TH-02", device_id="TH-02")
        th02_new = replace(
            th02_old,
            revision="Rev.B",
            humidity_max=50.0,
            effective_from=self.now + timedelta(minutes=1),
        )
        th03_old = self._standard("ENV-TH-03", device_id="TH-03")
        th03_new = replace(
            th03_old,
            revision="Rev.B",
            humidity_max=55.0,
            effective_from=self.now + timedelta(minutes=1),
        )
        report = StandardSyncService(
            source=_Source((th02_old, th02_new, th03_old, th03_new)),
            repository=self.repository,
            source_name="feishu:test",
        ).sync(now=self.now + timedelta(minutes=2))

        self.assertEqual(report.status, StandardSyncStatus.SUCCEEDED)
        resolver = SQLiteStandardResolver(self.repository)
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-02",
                timestamp=self.now + timedelta(minutes=1),
            ).revision,
            "Rev.B",
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-03",
                timestamp=self.now + timedelta(minutes=1),
            ).revision,
            "Rev.B",
        )

    def test_th02_th03_legacy_abnormal_recovery_chain_is_valid(self) -> None:
        th02_legacy = self._standard("ENV-LEGACY-TH-02", device_id="TH-02")
        th03_legacy = self._standard("ENV-LEGACY-TH-03", device_id="TH-03")
        th02_abnormal = replace(
            th02_legacy,
            revision="TH02-E2E-ABNORMAL",
            humidity_max=50.0,
            effective_from=self.now + timedelta(minutes=1),
        )
        th03_abnormal = replace(
            th03_legacy,
            revision="TH03-E2E-ABNORMAL",
            humidity_max=50.0,
            effective_from=self.now + timedelta(minutes=1),
        )
        th02_recovery = replace(
            th02_legacy,
            revision="TH02-E2E-RECOVERY",
            effective_from=self.now + timedelta(minutes=2),
        )
        th03_recovery = replace(
            th03_legacy,
            revision="TH03-E2E-RECOVERY",
            effective_from=self.now + timedelta(minutes=2),
        )
        source = _Source((th02_legacy, th03_legacy))
        service = StandardSyncService(
            source=source,
            repository=self.repository,
            source_name="feishu:test",
        )

        self.assertEqual(service.sync(now=self.now).status, StandardSyncStatus.SUCCEEDED)
        source.standards = (
            th02_legacy,
            th02_abnormal,
            th03_legacy,
            th03_abnormal,
        )
        self.assertEqual(
            service.sync(now=self.now + timedelta(minutes=1)).status,
            StandardSyncStatus.SUCCEEDED,
        )
        resolver = SQLiteStandardResolver(self.repository)
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-02",
                timestamp=self.now + timedelta(minutes=1),
            ).revision,
            "TH02-E2E-ABNORMAL",
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-03",
                timestamp=self.now + timedelta(minutes=1),
            ).revision,
            "TH03-E2E-ABNORMAL",
        )

        source.standards = (
            th02_legacy,
            th02_abnormal,
            th02_recovery,
            th03_legacy,
            th03_abnormal,
            th03_recovery,
        )
        self.assertEqual(
            service.sync(now=self.now + timedelta(minutes=2)).status,
            StandardSyncStatus.SUCCEEDED,
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-02",
                timestamp=self.now + timedelta(minutes=2),
            ).revision,
            "TH02-E2E-RECOVERY",
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-03",
                timestamp=self.now + timedelta(minutes=2),
            ).revision,
            "TH03-E2E-RECOVERY",
        )
        self.assertEqual(len(self.repository.list_all()), 6)

    def test_invalid_snapshot_keeps_previous_active_standard(self) -> None:
        initial = self._standard("ENV-001")
        self.repository.apply_snapshot((initial,), source="feishu:test", synced_at=self.now)
        invalid = (
            self._standard("ENV-002"),
            self._standard("ENV-003"),
        )
        report = StandardSyncService(
            source=_Source(invalid),
            repository=self.repository,
            source_name="feishu:test",
        ).sync(now=self.now + timedelta(minutes=1))
        self.assertEqual(report.status, StandardSyncStatus.FAILED)
        self.assertFalse(report.activated)
        selected = SQLiteStandardResolver(self.repository).resolve(
            area_id="仓库",
            operation_type=None,
            device_id="TH-01",
            timestamp=self.now,
        )
        self.assertEqual(selected.standard_id, "ENV-001")

    def test_source_failure_keeps_previous_active_standard(self) -> None:
        initial = self._standard("ENV-001")
        self.repository.apply_snapshot((initial,), source="feishu:test", synced_at=self.now)

        class FailingSource:
            def fetch_standards(self) -> tuple[EnvironmentStandard, ...]:
                raise RuntimeError("temporary Feishu outage")

        report = StandardSyncService(
            source=FailingSource(),
            repository=self.repository,
            source_name="feishu:test",
        ).sync(now=self.now + timedelta(minutes=1))
        self.assertEqual(report.status, StandardSyncStatus.FAILED)
        self.assertEqual(
            SQLiteStandardResolver(self.repository)
            .resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-01",
                timestamp=self.now,
            )
            .standard_id,
            "ENV-001",
        )

    def test_existing_revision_content_change_is_rejected(self) -> None:
        initial = self._standard("ENV-001")
        service = StandardSyncService(
            source=_Source((initial,)),
            repository=self.repository,
            source_name="feishu:test",
        )
        self.assertEqual(service.sync(now=self.now).status, StandardSyncStatus.SUCCEEDED)

        changed = replace(initial, temperature_max=30.0)
        report = StandardSyncService(
            source=_Source((changed,)),
            repository=self.repository,
            source_name="feishu:test",
        ).sync(now=self.now + timedelta(minutes=1))

        self.assertEqual(report.status, StandardSyncStatus.FAILED)
        self.assertTrue(
            any("immutable standard revision changed" in error for error in report.errors)
        )


if __name__ == "__main__":
    unittest.main()
