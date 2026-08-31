from __future__ import annotations

import unittest
from datetime import datetime

from domain.models import (
    ApplicabilityStatus,
    ControlType,
    DataQualityStatus,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OverallStatus,
    TemperatureStatus,
)
from domain.monitor_engine import evaluate_monitor_state


class MonitorEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = DeviceContext(device_id="TH-03", area="仓库")
        self.standard = EnvironmentStandard(
            standard_id="ENV-002",
            revision="Rev.B",
            area="仓库",
            operation_type=None,
            temperature_min=20.0,
            temperature_max=26.0,
            humidity_min=40.0,
            humidity_max=60.0,
            effective_from=datetime(2026, 1, 1),
            effective_to=None,
            source_document="test-standard",
            clause="5.2.3",
        )
        self.sample_time = datetime(2026, 8, 28, 13, 0)

    def test_normal_inclusive_boundaries(self) -> None:
        result = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 20.0, 60.0),
            standard=self.standard,
        )
        self.assertEqual(result.temperature_status, TemperatureStatus.NORMAL)
        self.assertEqual(result.humidity_status, TemperatureStatus.NORMAL)
        self.assertEqual(result.overall_status, OverallStatus.NORMAL)

    def test_temperature_high_is_violation(self) -> None:
        result = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 26.1, 50.0),
            standard=self.standard,
        )
        self.assertEqual(result.temperature_status, TemperatureStatus.HIGH)
        self.assertEqual(result.overall_status, OverallStatus.VIOLATION)
        self.assertEqual(result.reasons, ("temperature_above_upper_limit",))

    def test_missing_applicable_measurement_is_unknown(self) -> None:
        result = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 24.0, None),
            standard=self.standard,
        )
        self.assertEqual(result.humidity_status, TemperatureStatus.UNKNOWN)
        self.assertEqual(result.overall_status, OverallStatus.UNKNOWN)

    def test_unconstrained_dimension_does_not_become_unknown(self) -> None:
        standard = EnvironmentStandard(
            standard_id="ENV-TEMP",
            revision="1",
            area="仓库",
            operation_type=None,
            temperature_min=20.0,
            temperature_max=26.0,
            humidity_min=None,
            humidity_max=None,
            effective_from=datetime(2026, 1, 1),
            effective_to=None,
            source_document="test-standard",
            clause=None,
        )
        result = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 24.0, None),
            standard=standard,
        )
        self.assertEqual(result.humidity_status, TemperatureStatus.NORMAL)
        self.assertEqual(result.overall_status, OverallStatus.NORMAL)

    def test_monitor_only_is_not_applicable_not_normal(self) -> None:
        result = evaluate_monitor_state(
            device=DeviceContext(
                device_id="TH-01",
                area="对拖测试区",
                control_type=ControlType.MONITOR_ONLY,
            ),
            sample=MonitorSample("TH-01", self.sample_time, 24.0, 50.0),
            standard=self.standard,
        )
        self.assertEqual(result.applicability, ApplicabilityStatus.NOT_APPLICABLE)
        self.assertEqual(result.overall_status, OverallStatus.UNKNOWN)

    def test_operation_period_idle_is_not_applicable(self) -> None:
        from domain.models import OperationState, OperationStatus

        result = evaluate_monitor_state(
            device=DeviceContext(
                device_id="TH-03",
                area="仓库",
                control_type=ControlType.OPERATION_PERIOD,
            ),
            sample=MonitorSample("TH-03", self.sample_time, 24.0, 50.0),
            standard=self.standard,
            operation_state=OperationState(
                area_id="仓库",
                state=OperationStatus.IDLE,
                operation_type=None,
                work_order=None,
                started_at=None,
                ended_at=None,
            ),
        )
        self.assertEqual(result.applicability, ApplicabilityStatus.NOT_APPLICABLE)

    def test_all_day_na_operation_context_remains_applicable(self) -> None:
        from domain.models import OperationState, OperationStatus

        result = evaluate_monitor_state(
            device=DeviceContext(
                device_id="TH-10",
                area="仓库",
                control_type=ControlType.ALL_DAY,
            ),
            sample=MonitorSample("TH-10", self.sample_time, 24.0, 50.0),
            standard=self.standard,
            operation_state=OperationState(
                area_id="仓库",
                state=OperationStatus.NOT_APPLICABLE,
                operation_type=None,
                work_order=None,
                started_at=None,
                ended_at=None,
            ),
        )
        self.assertEqual(result.applicability, ApplicabilityStatus.APPLICABLE)
        self.assertEqual(result.overall_status, OverallStatus.NORMAL)

    def test_paused_operation_status_is_not_supported(self) -> None:
        from domain.models import OperationState

        with self.assertRaises(ValueError):
            OperationState(
                area_id="仓库",
                state="PAUSED",
                operation_type=None,
                work_order=None,
                started_at=None,
                ended_at=None,
            )

    def test_no_standard_is_explicit(self) -> None:
        result = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 24.0, 50.0),
            standard=None,
        )
        self.assertEqual(result.applicability, ApplicabilityStatus.NO_STANDARD)
        self.assertEqual(result.overall_status, OverallStatus.UNKNOWN)

    def test_offline_is_distinct_from_missing_and_error(self) -> None:
        offline = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, None, None, online_status="离线"),
            standard=self.standard,
        )
        missing = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 24.0, None),
            standard=self.standard,
        )
        error = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample(
                "TH-03",
                self.sample_time,
                24.0,
                50.0,
                data_quality=DataQualityStatus.ERROR,
            ),
            standard=self.standard,
        )
        self.assertEqual(offline.data_quality, DataQualityStatus.OFFLINE)
        self.assertEqual(missing.data_quality, DataQualityStatus.MISSING)
        self.assertEqual(error.data_quality, DataQualityStatus.ERROR)
        self.assertEqual(offline.overall_status, OverallStatus.UNKNOWN)

    def test_temperature_low_and_humidity_high(self) -> None:
        low = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 19.9, 50.0),
            standard=self.standard,
        )
        high = evaluate_monitor_state(
            device=self.device,
            sample=MonitorSample("TH-03", self.sample_time, 24.0, 60.1),
            standard=self.standard,
        )
        self.assertEqual(low.temperature_status, TemperatureStatus.LOW)
        self.assertEqual(high.humidity_status, TemperatureStatus.HIGH)
        self.assertEqual(low.overall_status, OverallStatus.VIOLATION)
        self.assertEqual(high.overall_status, OverallStatus.VIOLATION)


if __name__ == "__main__":
    unittest.main()
