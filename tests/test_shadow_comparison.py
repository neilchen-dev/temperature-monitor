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
from integrations.feishu_records import FeishuRawRecord
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

    def test_th08_all_day_device_remains_a_shadow_match_control(self) -> None:
        expected = ExpectedAutomationState(
            "TH-08",
            "NORMAL",
            "NOT_APPLICABLE",
            False,
            overall_status="NORMAL",
            applicability="APPLICABLE",
        )
        observed = ObservedAutomationState(
            "TH-08",
            "NORMAL",
            "NOT_APPLICABLE",
            False,
            overall_status="NORMAL",
            applicability="APPLICABLE",
        )

        self.assertTrue(compare_states(expected, observed).matched)

    def test_operation_type_difference_is_operation_mismatch(self) -> None:
        expected = ExpectedAutomationState(
            "TH-03", "NORMAL", "OPERATING", False, operation_type="工艺A"
        )
        observed = ObservedAutomationState(
            "TH-03", "NORMAL", "OPERATING", False, operation_type="工艺B"
        )
        diff = compare_states(expected, observed)
        self.assertEqual(diff.difference_type, ("OPERATION_STATE_MISMATCH",))

    def test_overall_status_difference_is_not_an_alarm_state_mismatch(self) -> None:
        expected = ExpectedAutomationState(
            "TH-01",
            "NORMAL",
            "NOT_APPLICABLE",
            False,
            overall_status="UNKNOWN",
        )
        observed = ObservedAutomationState(
            "TH-01",
            "NORMAL",
            "NOT_APPLICABLE",
            False,
            overall_status="NORMAL",
        )

        diff = compare_states(expected, observed)

        self.assertEqual(diff.difference_type, ("OVERALL_STATUS_MISMATCH",))
        self.assertNotIn("alarm_state", diff.details)
        self.assertEqual(
            diff.details["overall_status"],
            {"expected": "UNKNOWN", "observed": "NORMAL"},
        )

    def test_monitor_result_differences_have_precise_types(self) -> None:
        expected = ExpectedAutomationState(
            "TH-03",
            "NORMAL",
            "IDLE",
            False,
            applicability="NOT_APPLICABLE",
            data_quality="GOOD",
            temperature_status="NORMAL",
            humidity_status="NORMAL",
        )
        observed = ObservedAutomationState(
            "TH-03",
            "NORMAL",
            "IDLE",
            False,
            applicability="APPLICABLE",
            data_quality="OFFLINE",
            temperature_status="HIGH",
            humidity_status="LOW",
        )

        self.assertEqual(
            compare_states(expected, observed).difference_type,
            (
                "APPLICABILITY_MISMATCH",
                "DATA_QUALITY_MISMATCH",
                "TEMPERATURE_STATUS_MISMATCH",
                "HUMIDITY_STATUS_MISMATCH",
            ),
        )

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

    # ---- 待人工闭环（已写恢复时间、未关闭）与活动报警是两种生命周期 ----

    def test_recovered_pending_closure_event_is_not_duplicated_for_normal(self) -> None:
        """NORMAL + 仅剩“已恢复待人工关闭”事件 = 设计内终态，应判定一致。"""
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "NORMAL", "OPERATING", False, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "NORMAL",
            "OPERATING",
            True,  # 飞书事件仍打开
            active_event_count=0,  # 但全部已写恢复时间
            pending_closure_count=1,
            observed_at=expected_at,
        )
        diff = compare_states(expected, observed)
        self.assertTrue(diff.matched)
        self.assertEqual(diff.difference_type, ())

    def test_unrecovered_open_event_is_duplicated_for_normal(self) -> None:
        """NORMAL + 未写恢复时间的打开事件 = 真正的重复报警。"""
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "NORMAL", "OPERATING", False, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "NORMAL",
            "OPERATING",
            True,
            active_event_count=1,
            pending_closure_count=0,
            observed_at=expected_at,
        )
        self.assertEqual(
            compare_states(expected, observed).difference_type,
            ("EVENT_DUPLICATED",),
        )

    def test_recovered_pending_closure_satisfies_recovery_projection(self) -> None:
        """RECOVERY 窗口内飞书事件已写恢复时间属正常中间态，不应 EVENT_MISSING。"""
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "RECOVERY", "OPERATING", True, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "RECOVERY",
            "OPERATING",
            True,
            active_event_count=0,
            pending_closure_count=1,
            observed_at=expected_at,
        )
        self.assertTrue(compare_states(expected, observed).matched)

    def test_pending_closure_does_not_satisfy_alarm_projection(self) -> None:
        """ALARM 期望下只有已恢复的打开事件 → 活动报警事件缺失。"""
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "ALARM", "OPERATING", True, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "ALARM",
            "OPERATING",
            True,
            active_event_count=0,
            pending_closure_count=1,
            observed_at=expected_at - timedelta(seconds=61),
        )
        self.assertEqual(
            compare_states(expected, observed).difference_type,
            ("EVENT_MISSING",),
        )

    def test_alarm_with_stale_recovered_events_still_matches(self) -> None:
        """ALARM + 1 条活动事件 + 2 条历史待闭环 → 仍按匹配处理。"""
        expected_at = datetime(2026, 8, 28, 13, 0)
        expected = ExpectedAutomationState(
            "TH-03", "ALARM", "OPERATING", True, expected_at=expected_at
        )
        observed = ObservedAutomationState(
            "TH-03",
            "ALARM",
            "OPERATING",
            True,
            active_event_count=1,
            pending_closure_count=2,
            observed_at=expected_at,
        )
        self.assertTrue(compare_states(expected, observed).matched)

    def test_record_failure_persists_observation_error_run(self) -> None:
        """观察失败也要落 automation_runs，避免“静默无比对”。"""
        connection = sqlite3.connect(":memory:")
        recorder = SQLiteAutomationRunRepository(connection)
        comparison = ShadowComparisonService(
            observation_adapter=FeishuObservationAdapter(
                source=_RawSource(),
                fields=FeishuObservationFieldMap("alarm", "operation", "event"),
            ),
            recorder=recorder,
        )
        comparison.record_failure(
            device_id="TH-01",
            sample_time=datetime(2026, 8, 28, 13, 0),
            expected={"device_id": "TH-01", "alarm_state": "NORMAL"},
            error="RuntimeError: device record not found",
            created_at=datetime(2026, 8, 28, 13, 0),
        )
        row = connection.execute(
            "SELECT action_type, matched, difference_type, error FROM automation_runs"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("SHADOW_COMPARE", 0, "OBSERVATION_ERROR", None),
        )
        context = connection.execute(
            "SELECT feishu_observed_state_json FROM automation_runs"
        ).fetchone()[0]
        self.assertIn("device record not found", context)
        connection.close()

    def test_bitable_source_zero_counts_do_not_break_observe(self) -> None:
        """零事件/零待闭环是常态，observe() 不得因 count=0 抛 ValueError。

        回归：_optional_int 以前对整型 0 调 _text(0)（falsy → 空串），
        int("") 抛错，导致任何无打开事件的设备比对必然失败并无限重试。
        """
        from integrations.feishu_observation import FeishuBitableObservationSource

        class _TableSource:
            def read_records(self, table_id: str):
                if table_id == "device-table":
                    return (
                        FeishuRawRecord(
                            record_id="device-1",
                            fields={
                                "设备编号": "TH-01",
                                "警报状态": "未触发",
                                "当前作业状态": "N/A",
                            },
                            updated_at=datetime(2026, 8, 31, 12, 0),
                        ),
                    )
                return ()  # 事件表为空：TH-01 没有任何打开事件

        source = FeishuBitableObservationSource(
            source=_TableSource(),
            device_table_id="device-table",
            event_table_id="event-table",
        )
        raw = source.read("TH-01")
        self.assertEqual(raw["__active_event_count"], 0)
        self.assertEqual(raw["__pending_closure_count"], 0)
        adapter = FeishuObservationAdapter(
            source=source,
            fields=FeishuObservationFieldMap(
                alarm_state="警报状态",
                operation_state="当前作业状态",
                event_exists="__event_exists",
                active_event_count="__active_event_count",
                pending_closure_count="__pending_closure_count",
            ),
        )
        observed = adapter.observe("TH-01")
        self.assertEqual(observed.active_event_count, 0)
        self.assertEqual(observed.pending_closure_count, 0)
        self.assertFalse(observed.event_exists)


if __name__ == "__main__":
    unittest.main()
