from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from application.action_executor import ActionExecutor
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from application.shadow import (
    ObservedAutomationState,
    ShadowComparisonService,
    expected_state_from,
)
from application.standard_sync import StandardSyncService, StandardSyncStatus
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmState,
    ApplicabilityStatus,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationState,
    OperationStatus,
    OverallStatus,
)
from domain.monitor_engine import evaluate_monitor_state
from domain.standard_resolver import StaticStandardResolver
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_standard import FeishuStandardAdapter, FeishuStandardFieldMap
from repositories.automation_runs import SQLiteAutomationRunRepository
from repositories.standard_resolver import SQLiteStandardRepository, SQLiteStandardResolver


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _standard(
    control_type: ControlType | str | None,
    *,
    revision: str = "R1",
    effective_from: datetime = NOW - timedelta(days=1),
    effective_to: datetime | None = None,
) -> EnvironmentStandard:
    return EnvironmentStandard(
        standard_id="ENV-TH-03",
        revision=revision,
        area="精密装配间",
        operation_type=None,
        temperature_min=20.0,
        temperature_max=26.0,
        humidity_min=40.0,
        humidity_max=60.0,
        effective_from=effective_from,
        effective_to=effective_to,
        source_document="SOP-1",
        clause=None,
        device_id="TH-03",
        control_type=control_type,
    )


def _evaluate(
    standard_control: ControlType | str | None,
    legacy_control: ControlType | str | None,
    operation_status: OperationStatus,
):
    device = DeviceContext("TH-03", "精密装配间", control_type=legacy_control)
    return evaluate_monitor_state(
        device=device,
        sample=MonitorSample("TH-03", NOW, 24.0, 50.0, online_status="online"),
        standard=_standard(standard_control),
        operation_state=OperationState(
            area_id=device.area,
            state=operation_status,
            operation_type=None,
            work_order=None,
            started_at=None,
            ended_at=None,
        ),
    )


def test_standard_all_day_overrides_legacy_operation_period() -> None:
    result = _evaluate(ControlType.ALL_DAY, ControlType.OPERATION_PERIOD, OperationStatus.IDLE)
    assert result.resolved_control_type is ControlType.ALL_DAY
    assert result.control_type_source == "standard"
    assert result.control_type_consistency == "mismatch"
    assert result.applicability is ApplicabilityStatus.APPLICABLE


@pytest.mark.parametrize(
    ("operation_status", "applicability"),
    (
        (OperationStatus.OPERATING, ApplicabilityStatus.APPLICABLE),
        (OperationStatus.IDLE, ApplicabilityStatus.NOT_APPLICABLE),
    ),
)
def test_standard_operation_period_uses_operation_state(
    operation_status: OperationStatus, applicability: ApplicabilityStatus
) -> None:
    result = _evaluate(
        ControlType.OPERATION_PERIOD, ControlType.ALL_DAY, operation_status
    )
    assert result.applicability is applicability


def test_standard_monitor_only_never_enters_alarm_judgement() -> None:
    result = _evaluate(ControlType.MONITOR_ONLY, ControlType.ALL_DAY, OperationStatus.OPERATING)
    assert result.applicability is ApplicabilityStatus.NOT_APPLICABLE
    assert result.overall_status is OverallStatus.UNKNOWN


def test_missing_standard_control_type_uses_legacy_fallback() -> None:
    result = _evaluate(None, ControlType.OPERATION_PERIOD, OperationStatus.OPERATING)
    assert result.resolved_control_type is ControlType.OPERATION_PERIOD
    assert result.control_type_source == "device_context_fallback"
    assert result.control_type_consistency == "standard_missing"


def test_both_control_types_missing_is_configuration_error() -> None:
    result = _evaluate(None, None, OperationStatus.OPERATING)
    assert result.resolved_control_type is None
    assert result.control_type_source == "configuration_error"
    assert result.applicability is ApplicabilityStatus.NOT_APPLICABLE
    assert "control_type_configuration_error" in result.reasons


class _Source:
    def __init__(self, value: object) -> None:
        self.value = value

    def read_records(self, table_id: str):  # noqa: ARG002
        return (
            FeishuRawRecord(
                record_id="rec-1",
                fields={
                    "id": "ENV-TH-03",
                    "revision": "R1",
                    "area": "精密装配间",
                    "device": "TH-03",
                    "operation": None,
                    "control": self.value,
                    "tmin": 20,
                    "tmax": 26,
                    "hmin": 40,
                    "hmax": 60,
                    "from": NOW.isoformat(),
                    "to": None,
                    "priority": 1,
                    "enabled": True,
                    "source": "SOP-1",
                    "clause": None,
                },
            ),
        )


def _adapter(value: object) -> FeishuStandardAdapter:
    return FeishuStandardAdapter(
        source=_Source(value),
        table_id="standards",
        fields=FeishuStandardFieldMap(
            standard_id="id",
            revision="revision",
            area="area",
            device_id="device",
            operation_type="operation",
            control_type="control",
            temperature_min="tmin",
            temperature_max="tmax",
            humidity_min="hmin",
            humidity_max="hmax",
            effective_from="from",
            effective_to="to",
            priority="priority",
            enabled="enabled",
            source_document="source",
            clause="clause",
        ),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("全天控制", ControlType.ALL_DAY),
        ("作业期间控制", ControlType.OPERATION_PERIOD),
        ("仅监测", ControlType.MONITOR_ONLY),
        ("ALL_DAY", ControlType.ALL_DAY),
        ("OPERATION_PERIOD", ControlType.OPERATION_PERIOD),
        ("MONITOR_ONLY", ControlType.MONITOR_ONLY),
    ),
)
def test_feishu_control_type_parser(raw: str, expected: ControlType) -> None:
    assert _adapter(raw).fetch_standards()[0].control_type is expected


def test_invalid_feishu_control_type_records_sync_failure() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteStandardRepository(connection)
    report = StandardSyncService(source=_adapter("随便控制"), repository=repository).sync(
        now=NOW
    )
    assert report.status == StandardSyncStatus.FAILED
    assert "unsupported control_type" in report.errors[0]
    assert repository.list_all() == ()


def test_control_type_switches_with_effective_revision() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteStandardRepository(connection)
    boundary = NOW
    repository.apply_snapshot(
        (
            _standard(ControlType.OPERATION_PERIOD, effective_to=boundary),
            _standard(ControlType.ALL_DAY, revision="R2", effective_from=boundary),
        ),
        source="test",
        synced_at=NOW,
    )
    resolver = SQLiteStandardResolver(repository)
    assert resolver.resolve(
        area_id="精密装配间", operation_type=None,
        timestamp=boundary - timedelta(seconds=1), device_id="TH-03"
    ).control_type is ControlType.OPERATION_PERIOD
    assert resolver.resolve(
        area_id="精密装配间", operation_type=None,
        timestamp=boundary, device_id="TH-03"
    ).control_type is ControlType.ALL_DAY


def test_old_sqlite_schema_is_upgraded_without_losing_rows() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE standard_versions (
        standard_id TEXT NOT NULL, revision TEXT NOT NULL, area TEXT NOT NULL,
        operation_type TEXT, temperature_min REAL, temperature_max REAL,
        humidity_min REAL, humidity_max REAL, effective_from TEXT NOT NULL,
        effective_to TEXT, source_document TEXT NOT NULL, clause TEXT,
        priority INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (standard_id, revision))"""
    )
    connection.execute(
        """INSERT INTO standard_versions VALUES
        ('OLD', 'R1', '精密装配间', NULL, 20, 26, 40, 60, ?, NULL,
         'legacy', NULL, 0, 1, ?, ?)""",
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    repository = SQLiteStandardRepository(connection)
    assert repository.list_all()[0].control_type is None
    repository.upsert(_standard(ControlType.ALL_DAY), updated_at=NOW)
    inserted = next(row for row in repository.list_all() if row.standard_id == "ENV-TH-03")
    assert inserted.control_type is ControlType.ALL_DAY


class _OperationProvider:
    def get(self, device: DeviceContext) -> OperationState:
        return OperationState(device.area, OperationStatus.IDLE, None, None, None, None)


class _AlarmRepository:
    def __init__(self) -> None:
        self.state: AlarmState | None = None

    def get(self, device_id: str) -> AlarmState | None:  # noqa: ARG002
        return self.state

    def save(self, state: AlarmState) -> None:
        self.state = state


def test_control_type_conflict_warns_and_standard_wins() -> None:
    standard = _standard(ControlType.ALL_DAY)
    service = MonitorApplicationService(
        operation_state_provider=_OperationProvider(),
        standard_resolver=StaticStandardResolver((standard,)),
        alarm_state_repository=_AlarmRepository(),
        alarm_state_machine=AlarmStateMachine(),
        action_mapper=ApplicationActionMapper(),
        action_executor=ActionExecutor(mode="shadow"),
    )
    with patch("application.monitor_service.logger.warning") as warning:
        result = service.handle_sample(
            device=DeviceContext(
                "TH-03", "精密装配间", control_type=ControlType.OPERATION_PERIOD
            ),
            sample=MonitorSample("TH-03", NOW, 24.0, 50.0),
            now=NOW,
        )
    assert result.monitor_result.resolved_control_type is ControlType.ALL_DAY
    warning.assert_called_once()
    message, *values = warning.call_args.args
    rendered = message % tuple(values)
    assert "source=standard_table" in rendered
    assert "device_id=TH-03" in rendered


class _ObservationAdapter:
    def observe(self, device_id: str) -> ObservedAutomationState:
        return ObservedAutomationState(device_id, "NORMAL", "IDLE", False)


def test_shadow_automation_context_contains_control_type_source() -> None:
    connection = sqlite3.connect(":memory:")
    recorder = SQLiteAutomationRunRepository(connection)
    expected = expected_state_from(
        device_id="TH-03",
        alarm_state="NORMAL",
        operation_state=OperationState(
            "精密装配间", OperationStatus.IDLE, None, None, None, None
        ),
        resolved_control_type="ALL_DAY",
        control_type_source="standard",
        control_type_consistency="mismatch",
    )
    ShadowComparisonService(
        observation_adapter=_ObservationAdapter(), recorder=recorder
    ).compare(expected=expected, sample_time=NOW, created_at=NOW)
    context = json.loads(
        connection.execute("SELECT context_json FROM automation_runs").fetchone()[0]
    )
    assert context["expected"]["resolved_control_type"] == "ALL_DAY"
    assert context["expected"]["control_type_source"] == "standard"
    assert context["expected"]["control_type_consistency"] == "mismatch"
