from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from domain.standard_resolver import (
    StandardConfigurationConflictError,
    StandardNotFoundError,
    StaticStandardResolver,
)
from domain.models import EnvironmentStandard
from repositories.standard_resolver import (
    SQLiteStandardRepository,
    SQLiteStandardResolver,
)


class StandardResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.timestamp = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    def _standard(
        self,
        standard_id: str,
        *,
        device_id: str | None = None,
        operation_type: str | None = None,
        priority: int = 0,
        enabled: bool = True,
        effective_to: datetime | None = None,
    ) -> EnvironmentStandard:
        return EnvironmentStandard(
            standard_id=standard_id,
            revision="Rev.A",
            area="仓库",
            device_id=device_id,
            operation_type=operation_type,
            temperature_min=20.0,
            temperature_max=26.0,
            humidity_min=40.0,
            humidity_max=60.0,
            effective_from=self.start,
            effective_to=effective_to,
            source_document="SOP-001",
            clause="5.2.3",
            priority=priority,
            enabled=enabled,
        )

    def test_exact_operation_type_beats_default_even_with_lower_priority(self) -> None:
        resolver = StaticStandardResolver(
            (
                self._standard("DEFAULT", priority=99),
                self._standard("OPERATION", operation_type="清洁", priority=1),
            )
        )
        selected = resolver.resolve(
            area_id="仓库",
            operation_type="清洁",
            timestamp=self.timestamp,
        )
        self.assertEqual(selected.standard_id, "OPERATION")

    def test_exact_device_beats_area_default(self) -> None:
        resolver = StaticStandardResolver(
            (
                self._standard("AREA-DEFAULT", priority=100),
                self._standard("DEVICE-SPECIFIC", device_id="TH-05", priority=1),
            )
        )
        selected = resolver.resolve(
            area_id="仓库",
            operation_type=None,
            device_id="TH-05",
            timestamp=self.timestamp,
        )
        self.assertEqual(selected.standard_id, "DEVICE-SPECIFIC")

    def test_same_area_different_device_standards_are_distinct(self) -> None:
        resolver = StaticStandardResolver(
            (
                self._standard("TH-05", device_id="TH-05"),
                self._standard("TH-06", device_id="TH-06"),
            )
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-05",
                timestamp=self.timestamp,
            ).standard_id,
            "TH-05",
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                device_id="TH-06",
                timestamp=self.timestamp,
            ).standard_id,
            "TH-06",
        )

    def test_priority_selects_between_same_match_kind(self) -> None:
        resolver = StaticStandardResolver(
            (
                self._standard("LOW", priority=1),
                self._standard("HIGH", priority=2),
            )
        )
        selected = resolver.resolve(
            area_id="仓库",
            operation_type=None,
            timestamp=self.timestamp,
        )
        self.assertEqual(selected.standard_id, "HIGH")

    def test_default_standard_is_used_when_exact_operation_is_absent(self) -> None:
        resolver = StaticStandardResolver(
            (
                self._standard("DEFAULT"),
                self._standard("CLEANING", operation_type="清洁", priority=10),
            )
        )
        selected = resolver.resolve(
            area_id="仓库",
            operation_type="包装",
            timestamp=self.timestamp,
        )
        self.assertEqual(selected.standard_id, "DEFAULT")

    def test_conflict_is_an_error(self) -> None:
        resolver = StaticStandardResolver(
            (self._standard("A"), self._standard("B"))
        )
        with self.assertRaises(StandardConfigurationConflictError):
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                timestamp=self.timestamp,
            )

    def test_disabled_and_out_of_window_standards_are_ignored(self) -> None:
        resolver = StaticStandardResolver(
            (
                self._standard("DISABLED", enabled=False, priority=100),
                self._standard(
                    "EXPIRED",
                    effective_to=self.timestamp - timedelta(seconds=1),
                    priority=100,
                ),
            )
        )
        with self.assertRaises(StandardNotFoundError):
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                timestamp=self.timestamp,
            )

    def test_sqlite_resolver_uses_same_selection_rules(self) -> None:
        connection = sqlite3.connect(":memory:")
        repository = SQLiteStandardRepository(connection)
        repository.upsert(
            self._standard("DEFAULT"),
            updated_at=self.timestamp,
        )
        repository.upsert(
            self._standard("OPERATION", operation_type="清洁", priority=1),
            updated_at=self.timestamp,
        )
        resolver = SQLiteStandardResolver(repository)
        selected = resolver.resolve(
            area_id="仓库",
            operation_type="清洁",
            timestamp=self.timestamp,
        )
        self.assertEqual(selected.standard_id, "OPERATION")
        self.assertIsNone(selected.device_id)
        self.assertEqual(repository.list_all()[1].priority, 1)
        connection.close()

    def test_effective_from_is_inclusive_and_effective_to_is_exclusive(self) -> None:
        end = self.timestamp + timedelta(hours=1)
        resolver = StaticStandardResolver(
            (self._standard("WINDOW", effective_to=end),)
        )
        self.assertEqual(
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                timestamp=self.start,
            ).standard_id,
            "WINDOW",
        )
        with self.assertRaises(StandardNotFoundError):
            resolver.resolve(
                area_id="仓库",
                operation_type=None,
                timestamp=end,
            )

    def test_aware_standard_and_sample_time_compare_without_error(self) -> None:
        selected = StaticStandardResolver((self._standard("AWARE"),)).resolve(
            area_id="仓库",
            operation_type=None,
            timestamp=datetime(2026, 8, 28, 21, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        self.assertEqual(selected.standard_id, "AWARE")

    def test_environment_standard_rejects_naive_effective_from(self) -> None:
        with self.assertRaisesRegex(ValueError, "effective_from must be timezone-aware"):
            EnvironmentStandard(
                standard_id="NAIVE",
                revision="Rev.A",
                area="仓库",
                operation_type=None,
                temperature_min=20.0,
                temperature_max=26.0,
                humidity_min=40.0,
                humidity_max=60.0,
                effective_from=datetime(2026, 1, 1),
                effective_to=None,
                source_document="SOP-001",
                clause=None,
            )

    def test_bounds_must_be_complete_pairs(self) -> None:
        with self.assertRaises(ValueError):
            EnvironmentStandard(
                standard_id="BROKEN",
                revision="Rev.A",
                area="仓库",
                operation_type=None,
                temperature_min=20.0,
                temperature_max=None,
                humidity_min=40.0,
                humidity_max=60.0,
                effective_from=self.start,
                effective_to=None,
                source_document="SOP-001",
                clause=None,
            )


if __name__ == "__main__":
    unittest.main()
