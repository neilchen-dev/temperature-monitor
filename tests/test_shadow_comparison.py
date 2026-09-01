from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta

from application.shadow import (
    ExpectedAutomationState,
    ObservedAutomationState,
    ShadowComparisonService,
    compare_states,
)
from integrations.feishu_observation import (
    FeishuObservationAdapter,
    FeishuObservationFieldMap,
)
from repositories.automation_runs import SQLiteAutomationRunRepository


class _RawSource:
    def read(self, device_id: str) -> dict[str, object]:
        return {
            "alarm": {"value": [{"text": "ALARM"}]},
            "operation": "OPERATING",
            "operation_type": "工艺A",
            "event": "是",
        }


class ShadowComparisonTests(unittest.TestCase):
    def test_feishu_adapter_normalizes_raw_values(self) -> None:
        adapter = FeishuObservationAdapter(
            source=_RawSource(),
            fields=FeishuObservationFieldMap(
                alarm_state="alarm",
                operation_state="operation",
                event_exists="event",
                operation_type="operation_type",
            ),
        )
        observed = adapter.observe("TH-03")
        self.assertEqual(
            observed,
            ObservedAutomationState(
                "TH-03", "ALARM", "OPERATING", True, operation_type="工艺A"
            ),
        )

    def test_compare_returns_structured_diff_and_records_it(self) -> None:
        expected = ExpectedAutomationState("TH-03", "ALARM", "OPERATING", True)
        adapter = FeishuObservationAdapter(
            source=_RawSource(),
            fields=FeishuObservationFieldMap("alarm", "operation", "event"),
        )
        connection = sqlite3.connect(":memory:")
        recorder = SQLiteAutomationRunRepository(connection)
        comparison = ShadowComparisonService(
            observation_adapter=adapter,
            recorder=recorder,
        )
        diff = comparison.compare(
            expected=expected,
            sample_time=datetime(2026, 8, 28, 13, 0),
            created_at=datetime(2026, 8, 28, 13, 0),
        )
        self.assertTrue(diff.matched)
        row = connection.execute(
            "SELECT action_type, matched FROM automation_runs"
        ).fetchone()
        self.assertEqual(tuple(row), ("SHADOW_COMPARE", 1))
        connection.close()

    def test_compare_identifies_only_normalized_fields(self) -> None:
        expected = ExpectedAutomationState("TH-03", "ALARM", "OPERATING", True)
        observed = ObservedAutomationState("TH-03", "PENDING", "OPERATING", False)
        diff = compare_states(expected, observed)
        self.assertFalse(diff.matched)
        self.assertEqual(diff.difference_type, ("ALARM_STATE", "EVENT_EXISTS"))

    def test_missing_feishu_standard_columns_are_not_a_standard_mismatch(self) -> None:
        expected = ExpectedAutomationState(
            "TH-10",
            "NORMAL",
            "NOT_APPLICABLE",
            False,
            overall_status="NORMAL",
            standard_id="ENV-LEGACY-TH-10",
            standard_revision="LEGACY-2026-08-31",
        )
        observed = ObservedAutomationState(
            "TH-10",
            "NORMAL",
            "NOT_APPLICABLE",
            False,
            overall_status="NORMAL",
        )

        diff = compare_states(expected, observed)

        self.assertTrue(diff.matched)

    def test_operation_type_difference_is_operation_mismatch(self) -> None:
        expected = ExpectedAutomationState(
            "TH-03", "NORMAL", "OPERATING", False, operation_type="工艺A"
        )
        observed = ObservedAutomationState(
            "TH-03", "NORMAL", "OPERATING", False, operation_type="工艺B"
        )
        diff = compare_states(expected, observed)
        self.assertEqual(diff.difference_type, ("OPERATION_STATE_MISMATCH",))

    def test_thirty_second_difference_is_feishu_delay(self) -> None:
        expected = ExpectedAutomationState(
            "TH-03",
            "ALARM",
            "OPERATING",
            True,
            expected_at=datetime(2026, 8, 28, 13, 0),
        )
        observed = ObservedAutomationState(
            "TH-03",
            "PENDING",
            "OPERATING",
            True,
            observed_at=datetime(2026, 8, 28, 12, 59, 30),
        )
        diff = compare_states(expected, observed)
        self.assertEqual(diff.difference_type, ("FEISHU_DELAY",))
        self.assertEqual(diff.details["feishu_latency_seconds"], 30.0)

    def test_sixty_second_difference_is_still_feishu_delay(self) -> None:
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "ALARM", "OPERATING", True, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "PENDING",
            "OPERATING",
            True,
            observed_at=expected_at - timedelta(seconds=60),
        )
        self.assertEqual(compare_states(expected, observed).difference_type, ("FEISHU_DELAY",))

    def test_sixty_one_second_difference_is_real_mismatch(self) -> None:
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "ALARM", "OPERATING", True, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "PENDING",
            "OPERATING",
            True,
            observed_at=expected_at - timedelta(seconds=61),
        )
        self.assertEqual(
            compare_states(expected, observed).difference_type,
            ("ALARM_STATE_MISMATCH",),
        )

    def test_duplicate_event_is_prioritized_over_alarm_mismatch(self) -> None:
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "ALARM", "OPERATING", True, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "PENDING",
            "OPERATING",
            True,
            active_event_count=2,
            observed_at=expected_at,
        )
        self.assertEqual(
            compare_states(expected, observed).difference_type,
            ("EVENT_DUPLICATED",),
        )

    def test_missing_event_after_delay_is_event_missing(self) -> None:
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "ALARM", "OPERATING", True, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "PENDING",
            "OPERATING",
            False,
            active_event_count=0,
            observed_at=expected_at - timedelta(seconds=61),
        )
        self.assertEqual(
            compare_states(expected, observed).difference_type,
            ("EVENT_MISSING",),
        )


if __name__ == "__main__":
    unittest.main()
