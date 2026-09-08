from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from application.standard_sync import StandardSyncService, StandardSyncStatus
from application.action_executor import (
    ActionExecution,
    ActionExecutionStatus,
    ActionExecutor,
    AutomationMode,
)
from domain.models import AlarmAction, AlarmActionType
from domain.models import (
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationState,
    OperationStatus,
    OverallStatus,
)
from domain.monitor_engine import evaluate_monitor_state
from domain.standard_resolver import StandardNotFoundError
from repositories.standard_resolver import SQLiteStandardRepository, SQLiteStandardResolver
from repositories.automation_runs import SQLiteAutomationRunRepository


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class _MutableSource:
    def __init__(self, standards: tuple[EnvironmentStandard, ...]) -> None:
        self.standards = standards

    def fetch_standards(self) -> tuple[EnvironmentStandard, ...]:
        return self.standards


def _standard(
    *,
    revision: str = "R1",
    temperature_max: float = 26.0,
    effective_from: datetime = NOW - timedelta(days=1),
    enabled: bool = True,
) -> EnvironmentStandard:
    return EnvironmentStandard(
        standard_id="ENV-TH-01",
        revision=revision,
        area="对拖测试区",
        device_id="TH-01",
        operation_type=None,
        control_type=ControlType.ALL_DAY,
        temperature_min=20.0,
        temperature_max=temperature_max,
        humidity_min=40.0,
        humidity_max=60.0,
        effective_from=effective_from,
        effective_to=None,
        source_document="SOP-001",
        clause="5.2.3",
        enabled=enabled,
    )


def _service(source: _MutableSource) -> tuple[StandardSyncService, SQLiteStandardRepository]:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteStandardRepository(connection)
    service = StandardSyncService(
        source=source,
        repository=repository,
        source_name="feishu:standard-table",
    )
    return service, repository


def test_feishu_update_activates_new_validated_revision_and_keeps_history() -> None:
    source = _MutableSource((_standard(),))
    service, repository = _service(source)
    assert service.sync(now=NOW).status == StandardSyncStatus.SUCCEEDED

    source.standards = (
        _standard(
            revision="R2",
            temperature_max=30.0,
            effective_from=NOW + timedelta(minutes=1),
        ),
    )
    assert service.sync(now=NOW + timedelta(minutes=1)).status == StandardSyncStatus.SUCCEEDED

    selected = SQLiteStandardResolver(repository).resolve(
        area_id="对拖测试区",
        operation_type=None,
        device_id="TH-01",
        timestamp=NOW + timedelta(minutes=1),
    )
    assert (selected.standard_id, selected.revision) == ("ENV-TH-01", "R2")
    assert selected.temperature_max == 30.0
    assert len(repository.list_all()) == 2


def test_unchanged_snapshot_is_idempotent() -> None:
    standard = _standard()
    source = _MutableSource((standard,))
    service, repository = _service(source)
    service.sync(now=NOW)
    service.sync(now=NOW + timedelta(minutes=1))

    assert len(repository.list_all()) == 1
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM standard_snapshots"
    ).fetchone()[0] == 1
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM standard_sync_runs WHERE status = 'SUCCEEDED'"
    ).fetchone()[0] == 2


def test_no_active_pointer_keeps_standards_not_ready() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteStandardRepository(connection)

    readiness = repository.readiness(expected_device_ids=("TH-01",))

    assert readiness["active_snapshot_id"] is None
    assert readiness["last_known_good_snapshot_id"] is None
    assert readiness["validated_standard_count"] == 0
    assert readiness["expected_standard_count"] == 1
    assert readiness["standards_ready"] is False


def test_non_feishu_source_cannot_create_validated_active_snapshot() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteStandardRepository(connection)

    with pytest.raises(ValueError, match=r"feishu:\*"):
        repository.apply_snapshot((_standard(),), source="local", synced_at=NOW)

    assert repository.active_snapshot_id() is None
    assert repository.list_all() == ()


def test_first_strict_feishu_sync_establishes_readiness() -> None:
    source = _MutableSource((_standard(),))
    service, repository = _service(source)

    assert service.sync(now=NOW).status == StandardSyncStatus.SUCCEEDED
    readiness = repository.readiness(expected_device_ids=("TH-01",))

    assert readiness["active_snapshot_id"] is not None
    assert readiness["active_snapshot_id"] == readiness["last_known_good_snapshot_id"]
    assert readiness["validated_standard_count"] == 1
    assert readiness["expected_standard_count"] == 1
    assert readiness["latest_sync_status"] == StandardSyncStatus.SUCCEEDED
    assert readiness["last_successful_sync_at"] == NOW.isoformat()
    assert readiness["standard_source"] == "feishu:standard-table"
    assert readiness["standards_ready"] is True


def test_failed_sync_preserves_readiness_pointers_and_last_success() -> None:
    source = _MutableSource((_standard(),))
    service, repository = _service(source)
    service.sync(now=NOW)
    before = repository.readiness(expected_device_ids=("TH-01",))

    source.fetch_standards = lambda: (_standard(temperature_max=20.0),)  # type: ignore[method-assign]
    report = service.sync(now=NOW + timedelta(minutes=1))
    after = repository.readiness(expected_device_ids=("TH-01",))

    assert report.status == StandardSyncStatus.FAILED
    assert after["latest_sync_status"] == StandardSyncStatus.FAILED
    assert after["active_snapshot_id"] == before["active_snapshot_id"]
    assert after["last_known_good_snapshot_id"] == before["last_known_good_snapshot_id"]
    assert after["last_successful_sync_at"] == before["last_successful_sync_at"]
    assert after["standards_ready"] is True


def test_invalid_snapshot_does_not_replace_last_known_good() -> None:
    source = _MutableSource((_standard(),))
    service, repository = _service(source)
    service.sync(now=NOW)

    source.standards = (replace(_standard(), temperature_max=20.0),)
    report = service.sync(now=NOW + timedelta(minutes=1))

    assert report.status == StandardSyncStatus.FAILED
    selected = SQLiteStandardResolver(repository).resolve(
        area_id="对拖测试区",
        operation_type=None,
        device_id="TH-01",
        timestamp=NOW,
    )
    assert selected.revision == "R1"
    assert selected.temperature_max == 26.0
    assert len(repository.list_all()) == 1


def test_feishu_outage_keeps_last_known_good() -> None:
    source = _MutableSource((_standard(),))
    service, repository = _service(source)
    service.sync(now=NOW)

    def fail() -> tuple[EnvironmentStandard, ...]:
        raise RuntimeError("temporary Feishu outage")

    source.fetch_standards = fail  # type: ignore[method-assign]
    report = service.sync(now=NOW + timedelta(minutes=1))

    assert report.status == StandardSyncStatus.FAILED
    assert SQLiteStandardResolver(repository).resolve(
        area_id="对拖测试区",
        operation_type=None,
        device_id="TH-01",
        timestamp=NOW,
    ).revision == "R1"
    assert repository.connection.execute(
        "SELECT status FROM standard_sync_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()[0] == "FAILED"


def test_never_having_a_validated_standard_fails_closed() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteStandardRepository(connection)
    resolver = SQLiteStandardResolver(repository)
    with pytest.raises(StandardNotFoundError):
        resolver.resolve(
            area_id="对拖测试区",
            operation_type=None,
            device_id="TH-01",
            timestamp=NOW,
        )

    result = evaluate_monitor_state(
        device=DeviceContext("TH-01", "对拖测试区", control_type=ControlType.ALL_DAY),
        sample=MonitorSample("TH-01", NOW, 24.0, 50.0),
        standard=None,
        operation_state=OperationState(
            "对拖测试区", OperationStatus.IDLE, None, None, None, None
        ),
    )
    assert result.overall_status is OverallStatus.UNKNOWN
    assert result.standard_id is None
    assert result.standard_revision is None
    assert result.standard_source is None
    assert result.resolved_control_type is None
    assert result.control_type_source == "standard_unavailable"


def test_device_context_control_type_can_never_be_a_business_fallback() -> None:
    result = evaluate_monitor_state(
        device=DeviceContext("TH-01", "对拖测试区", control_type=ControlType.ALL_DAY),
        sample=MonitorSample("TH-01", NOW, 24.0, 50.0),
        standard=replace(_standard(), control_type=None),
        operation_state=OperationState(
            "对拖测试区", OperationStatus.IDLE, None, None, None, None
        ),
    )
    assert result.resolved_control_type is None
    assert result.overall_status is OverallStatus.UNKNOWN


def test_monitor_audit_persists_standard_traceability_columns() -> None:
    connection = sqlite3.connect(":memory:")
    recorder = SQLiteAutomationRunRepository(connection)
    recorder.record(
        ActionExecution(
            action=AlarmAction(
                action_type=AlarmActionType.CREATE_ALARM_EVENT,
                device_id="TH-01",
            ),
            mode=AutomationMode.SHADOW,
            status=ActionExecutionStatus.PLANNED,
            context={
                "sample_time": NOW.isoformat(),
                "python_monitor_result": {
                    "standard_id": "ENV-TH-01",
                    "standard_revision": "R1",
                    "standard_source": "feishu:standard-table",
                },
            },
            created_at=NOW,
        )
    )
    row = connection.execute(
        "SELECT standard_id, standard_revision, standard_source "
        "FROM automation_runs"
    ).fetchone()
    assert tuple(row) == ("ENV-TH-01", "R1", "feishu:standard-table")


def test_active_action_is_blocked_until_standards_are_ready() -> None:
    calls: list[str] = []
    executor = ActionExecutor(
        mode=AutomationMode.ACTIVE,
        active_device_ids=("TH-01",),
        handlers={
            AlarmActionType.CREATE_ALARM_EVENT: lambda action: calls.append("write")
        },
        standards_ready_provider=lambda: False,
    )

    executions = executor.execute(
        (
            AlarmAction(
                action_type=AlarmActionType.CREATE_ALARM_EVENT,
                device_id="TH-01",
            ),
        ),
        context={"device_id": "TH-01"},
        created_at=NOW,
    )

    assert executions[0].status is ActionExecutionStatus.PLANNED
    assert "standards_ready=false" in (executions[0].error or "")
    assert calls == []


def test_active_executor_without_readiness_provider_fails_closed() -> None:
    calls: list[str] = []
    executor = ActionExecutor(
        mode=AutomationMode.ACTIVE,
        active_device_ids=("TH-01",),
        handlers={
            AlarmActionType.CREATE_ALARM_EVENT: lambda action: calls.append("write")
        },
    )

    execution = executor.execute(
        (
            AlarmAction(
                action_type=AlarmActionType.CREATE_ALARM_EVENT,
                device_id="TH-01",
            ),
        ),
        context={"device_id": "TH-01"},
        created_at=NOW,
    )[0]

    assert execution.status is ActionExecutionStatus.PLANNED
    assert "standards_ready=false" in (execution.error or "")
    assert calls == []
