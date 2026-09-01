from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from application.operation_sync import OperationObservationService
from domain.models import (
    ApplicabilityStatus,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationStatus,
    OverallStatus,
)
from domain.monitor_engine import evaluate_monitor_state
from domain.operation import OperationAction, OperationObservation
from repositories.runtime_state import SQLiteOperationRepository
from runtime.bootstrap import DEFAULT_DEVICE_CONTEXTS


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _device(device_id: str, area: str, control_type: ControlType) -> DeviceContext:
    return DeviceContext(device_id=device_id, area=area, control_type=control_type)


def _standard(device: DeviceContext, operation_type: str | None = None) -> EnvironmentStandard:
    return EnvironmentStandard(
        standard_id=f"ENV-{device.device_id}",
        revision="Rev.A",
        area=device.area,
        operation_type=operation_type,
        temperature_min=20.0,
        temperature_max=26.0,
        humidity_min=40.0,
        humidity_max=60.0,
        effective_from=NOW - timedelta(days=1),
        effective_to=None,
        source_document="SOP-001",
        clause=None,
        device_id=device.device_id,
    )


def _evaluate(
    repository: SQLiteOperationRepository,
    device: DeviceContext,
    *,
    operation_type: str | None = None,
):
    return evaluate_monitor_state(
        device=device,
        sample=MonitorSample(device.device_id, NOW, 24.0, 50.0, online_status="online"),
        standard=_standard(device, operation_type),
        operation_state=repository.get(device),
    )


@pytest.mark.parametrize("device_id", ("TH-01", "TH-02", "TH-06"))
def test_monitor_only_production_devices_default_to_not_applicable(device_id: str) -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteOperationRepository(connection)
    area, control_type = DEFAULT_DEVICE_CONTEXTS[device_id]
    device = _device(device_id, area, control_type)

    state = repository.get(device)
    result = _evaluate(repository, device)

    assert control_type is ControlType.MONITOR_ONLY
    assert state.state is OperationStatus.NOT_APPLICABLE
    assert result.applicability is ApplicabilityStatus.NOT_APPLICABLE
    assert result.overall_status is OverallStatus.UNKNOWN
    connection.close()


@pytest.mark.parametrize(
    ("device_id", "expected_control", "expected_operation", "expected_overall"),
    (
        ("TH-05", ControlType.OPERATION_PERIOD, OperationStatus.IDLE, OverallStatus.UNKNOWN),
        ("TH-07", ControlType.OPERATION_PERIOD, OperationStatus.IDLE, OverallStatus.UNKNOWN),
        ("TH-08", ControlType.ALL_DAY, OperationStatus.NOT_APPLICABLE, OverallStatus.NORMAL),
        ("TH-09", ControlType.ALL_DAY, OperationStatus.NOT_APPLICABLE, OverallStatus.NORMAL),
        ("TH-11", ControlType.ALL_DAY, OperationStatus.NOT_APPLICABLE, OverallStatus.NORMAL),
    ),
)
def test_current_match_device_branches_are_preserved(
    device_id: str,
    expected_control: ControlType,
    expected_operation: OperationStatus,
    expected_overall: OverallStatus,
) -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteOperationRepository(connection)
    area, control_type = DEFAULT_DEVICE_CONTEXTS[device_id]
    device = _device(device_id, area, control_type)

    state = repository.get(device)
    result = _evaluate(repository, device)

    assert control_type is expected_control
    assert state.state is expected_operation
    assert result.overall_status is expected_overall
    connection.close()


def test_operation_period_with_valid_operation_is_applicable() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteOperationRepository(connection)
    service = OperationObservationService(store=repository)
    area, control_type = DEFAULT_DEVICE_CONTEXTS["TH-05"]
    device = _device("TH-05", area, control_type)
    service.apply(
        OperationObservation(
            device_id=device.device_id,
            area_id=device.area,
            action=OperationAction.START,
            operation_type="总装",
            work_order="WO-1",
            source_record_id="op-start",
            source_created_at=NOW - timedelta(minutes=5),
            observed_at=NOW - timedelta(minutes=5),
        )
    )

    state = repository.get(device)
    result = _evaluate(repository, device, operation_type="总装")

    assert state.state is OperationStatus.OPERATING
    assert result.applicability is ApplicabilityStatus.APPLICABLE
    assert result.overall_status is OverallStatus.NORMAL
    connection.close()


def test_operation_just_ended_returns_operation_period_to_idle() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteOperationRepository(connection)
    service = OperationObservationService(store=repository)
    area, control_type = DEFAULT_DEVICE_CONTEXTS["TH-05"]
    device = _device("TH-05", area, control_type)
    service.apply(
        OperationObservation(
            device_id=device.device_id,
            area_id=device.area,
            action=OperationAction.START,
            operation_type="总装",
            work_order="WO-1",
            source_record_id="op-start",
            source_created_at=NOW - timedelta(minutes=5),
            observed_at=NOW - timedelta(minutes=5),
        )
    )
    service.apply(
        OperationObservation(
            device_id=device.device_id,
            area_id=device.area,
            action=OperationAction.END,
            operation_type=None,
            work_order=None,
            source_record_id="op-end",
            source_created_at=NOW,
            observed_at=NOW,
        )
    )

    state = repository.get(device)
    result = _evaluate(repository, device)

    assert state.state is OperationStatus.IDLE
    assert state.ended_at == NOW
    assert result.applicability is ApplicabilityStatus.NOT_APPLICABLE
    assert result.overall_status is OverallStatus.UNKNOWN
    connection.close()
