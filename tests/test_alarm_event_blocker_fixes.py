from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from application.action_executor import (
    ActionExecutionStatus,
    ActionExecutor,
    AutomationMode,
)
from application.actions import (
    ApplicationAction,
    ApplicationActionKind,
    ApplicationActionMapper,
)
from application.monitor_service import MonitorApplicationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmAction,
    AlarmActionType,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationState,
    OperationStatus,
)
from domain.standard_resolver import StaticStandardResolver
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import (
    FeishuCreateNotPersistedError,
    FeishuEnvironmentEventWriter,
)
from repositories.automation_runs import SQLiteAutomationRunRepository
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import (
    ExternalCreateOutcome,
    SQLiteEnvironmentEventRepository,
)
from repositories.runtime_state import SQLiteAlarmStateRepository
from services.event_identity import external_effect_key


BASE_TIME = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)


class _RemoteFeishu:
    def __init__(self) -> None:
        self.events: list[FeishuRawRecord] = []
        self.create_calls = 0
        self.update_calls = 0
        self.fail_create_not_persisted = 0
        self.create_unknown = False
        self.fail_update = 0

    def read_records(self, table_id: str) -> tuple[FeishuRawRecord, ...]:
        if table_id == "devices":
            return (
                FeishuRawRecord(
                    "device-record",
                    {
                        "设备编号": "TEST-TH-01",
                        "默认异常责任人": [{"id": "test-owner"}],
                        "要求来源": "E2E TEST standard",
                    },
                ),
            )
        return tuple(self.events)

    def create(
        self,
        table_id: str,
        fields: dict[str, Any],
        *,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        self.create_calls += 1
        if self.fail_create_not_persisted:
            self.fail_create_not_persisted -= 1
            raise FeishuCreateNotPersistedError("test transport rejected before persist")
        record = FeishuRawRecord(
            f"remote-event-{self.create_calls}",
            dict(fields),
        )
        self.events.append(record)
        if self.create_unknown:
            self.create_unknown = False
            raise TimeoutError("test response lost after remote commit")
        return {"code": 0, "data": {"record": {"record_id": record.record_id}}}

    def update(
        self,
        table_id: str,
        record_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        self.update_calls += 1
        record = next(record for record in self.events if record.record_id == record_id)
        if self.fail_update:
            self.fail_update -= 1
            raise TimeoutError("test recovery response lost")
        record.fields.update(fields)
        return {"code": 0, "record_id": record_id, "fields": fields}


def _event_setup(
    *,
    opened_at: datetime = BASE_TIME,
) -> tuple[
    sqlite3.Connection,
    SQLiteEnvironmentEventRepository,
    _RemoteFeishu,
    FeishuEnvironmentEventWriter,
    str,
]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repository = SQLiteEnvironmentEventRepository(connection)
    event = repository.create_or_get_active(
        device_id="TEST-TH-01",
        event_key=f"ENV:TEST-TH-01:{opened_at.isoformat()}",
        opened_at=opened_at,
        payload={
            "projection": "isolated_e2e_test",
            "violation_started_at": opened_at.isoformat(),
            "feishu_binding_status": "PENDING",
            "feishu_create_attempted": False,
        },
    )
    remote = _RemoteFeishu()
    writer = FeishuEnvironmentEventWriter(
        writer=remote,
        source=remote,
        event_table_id="events",
        device_table_id="devices",
        event_repository=repository,
    )
    return connection, repository, remote, writer, event.event_id


def _event_action(event_id: str, action_type: AlarmActionType) -> AlarmAction:
    return AlarmAction(
        action_type=action_type,
        device_id="TEST-TH-01",
        alarm_id=event_id,
    )


def _event_context(
    start: datetime = BASE_TIME,
    *,
    created_at: datetime | None = None,
    recovered_at: datetime | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "device_id": "TEST-TH-01",
        "created_at": (created_at or start).isoformat(),
        "sample_time": start.isoformat(),
        "sample": {"temperature": 28.0, "humidity": 50.0},
        "python_monitor_result": {
            "temperature_status": "HIGH",
            "humidity_status": "NORMAL",
        },
        "python_alarm_transition": {
            "violation_started_at": start.isoformat(),
        },
        "operation_state": {"area_id": "TEST AREA"},
    }
    if recovered_at is not None:
        context["recovered_at"] = recovered_at.isoformat()
    return context


def test_create_explicit_no_persist_failure_reuses_identity_and_retries() -> None:
    connection, repository, remote, writer, event_id = _event_setup()
    task_repository = SQLiteAutomationTaskRepository(connection)
    try:
        task = task_repository.create_or_get_unfinished(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id="TEST-TH-01",
            due_at=BASE_TIME,
            payload={"local_event_id": event_id},
            dedupe_key=f"RECONCILE_ALARM_EVENT:{event_id}",
            created_at=BASE_TIME,
        )
        claimed = task_repository.claim_due(now=BASE_TIME, worker_id="create-worker")[0]
        assert claimed.task_id == task.task_id
        remote.fail_create_not_persisted = 1
        action = _event_action(event_id, AlarmActionType.CREATE_ALARM_EVENT)
        context = _event_context()
        context["automation_task_id"] = claimed.task_id
        with pytest.raises(FeishuCreateNotPersistedError):
            writer.handle_alarm_action(action, context)

        failed = repository.get(event_id)
        assert failed is not None
        assert failed.payload["feishu_create_state"] == ExternalCreateOutcome.RETRYABLE

        task_repository.reschedule_running(
            claimed,
            due_at=BASE_TIME + timedelta(seconds=1),
            updated_at=BASE_TIME,
            payload={"local_event_id": event_id},
        )
        retry = task_repository.claim_due(
            now=BASE_TIME + timedelta(seconds=1), worker_id="create-worker-2"
        )[0]
        assert retry.task_id == claimed.task_id
        context["automation_task_id"] = retry.task_id
        writer.handle_alarm_action(action, context)
        completed = repository.get(event_id)
        assert completed is not None
        assert completed.payload["feishu_create_state"] == ExternalCreateOutcome.SUCCEEDED
        assert completed.payload["feishu_record_id"] == "remote-event-2"
        assert remote.create_calls == 2
        assert len(remote.events) == 1
    finally:
        connection.close()


def test_create_unknown_outcome_lookup_binds_without_second_post() -> None:
    connection, repository, remote, writer, event_id = _event_setup()
    try:
        remote.create_unknown = True
        writer.handle_alarm_action(
            _event_action(event_id, AlarmActionType.CREATE_ALARM_EVENT),
            _event_context(),
        )
        event = repository.get(event_id)
        assert event is not None
        assert event.payload["feishu_record_id"] == "remote-event-1"
        assert event.payload["feishu_create_state"] == ExternalCreateOutcome.SUCCEEDED
        assert remote.create_calls == 1
        assert len(remote.events) == 1
    finally:
        connection.close()


def test_create_crash_after_remote_commit_recovers_after_lease_expiry() -> None:
    connection, repository, remote, writer, event_id = _event_setup()
    original_bind = repository.bind_external_record

    def crash_before_binding(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated worker crash before binding")

    try:
        repository.bind_external_record = crash_before_binding  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="simulated worker crash"):
            writer.handle_alarm_action(
                _event_action(event_id, AlarmActionType.CREATE_ALARM_EVENT),
                _event_context(),
            )
        repository.bind_external_record = original_bind  # type: ignore[method-assign]

        retry_time = BASE_TIME + timedelta(days=1)
        writer.handle_alarm_action(
            _event_action(event_id, AlarmActionType.CREATE_ALARM_EVENT),
            _event_context(created_at=retry_time),
        )
        event = repository.get(event_id)
        assert event is not None
        assert event.payload["feishu_record_id"] == "remote-event-1"
        assert remote.create_calls == 1
        assert len(remote.events) == 1
    finally:
        repository.bind_external_record = original_bind  # type: ignore[method-assign]
        connection.close()


def test_recovery_success_marker_makes_duplicate_action_a_noop() -> None:
    connection, repository, remote, writer, event_id = _event_setup()
    try:
        writer.handle_alarm_action(
            _event_action(event_id, AlarmActionType.CREATE_ALARM_EVENT),
            _event_context(),
        )
        recovered_at = BASE_TIME + timedelta(minutes=6)
        action = _event_action(event_id, AlarmActionType.MARK_ALARM_RECOVERED)
        context = _event_context(created_at=recovered_at, recovered_at=recovered_at)
        writer.handle_alarm_action(action, context)
        writer.handle_alarm_action(action, context)

        assert remote.update_calls == 1
        effect_key = external_effect_key(
            AlarmActionType.MARK_ALARM_RECOVERED.value,
            event_id,
            recovered_at=recovered_at,
        )
        event = repository.get(event_id)
        assert event is not None
        assert event.payload["feishu_external_effects"][effect_key]["status"] == "SUCCEEDED"
    finally:
        connection.close()


def test_recovery_failure_retries_same_task_and_same_effect_identity() -> None:
    connection, repository, remote, writer, event_id = _event_setup()
    task_repository = SQLiteAutomationTaskRepository(connection)
    try:
        writer.handle_alarm_action(
            _event_action(event_id, AlarmActionType.CREATE_ALARM_EVENT),
            _event_context(),
        )
        recovery_at = BASE_TIME + timedelta(minutes=6)
        action = _event_action(event_id, AlarmActionType.MARK_ALARM_RECOVERED)
        dedupe_key = f"RECONCILE_RECOVERY:{event_id}:1"
        task = task_repository.create_or_get_unfinished(
            task_type="RECONCILE_ALARM_EVENT",
            entity_type="DEVICE",
            entity_id="TEST-TH-01",
            due_at=BASE_TIME,
            payload={"local_event_id": event_id},
            dedupe_key=dedupe_key,
            created_at=BASE_TIME,
        )
        claimed = task_repository.claim_due(now=BASE_TIME, worker_id="recovery-worker")[0]
        assert claimed.task_id == task.task_id

        remote.fail_update = 1
        context = _event_context(created_at=BASE_TIME, recovered_at=recovery_at)
        context["automation_task_id"] = claimed.task_id
        with pytest.raises(TimeoutError):
            writer.handle_alarm_action(action, context)
        task_repository.reschedule_running(
            claimed,
            due_at=BASE_TIME + timedelta(minutes=1),
            updated_at=BASE_TIME + timedelta(seconds=1),
            payload={"local_event_id": event_id},
        )
        retry = task_repository.claim_due(
            now=BASE_TIME + timedelta(minutes=1),
            worker_id="recovery-worker-2",
        )[0]
        assert retry.task_id == claimed.task_id
        context["automation_task_id"] = retry.task_id
        writer.handle_alarm_action(action, context)
        task_repository.mark_succeeded(retry.task_id, finished_at=BASE_TIME + timedelta(minutes=1))

        assert remote.update_calls == 2
        effect_key = external_effect_key(
            AlarmActionType.MARK_ALARM_RECOVERED.value,
            event_id,
            recovered_at=recovery_at,
        )
        event = repository.get(event_id)
        assert event is not None
        assert event.payload["feishu_external_effects"][effect_key]["status"] == "SUCCEEDED"
    finally:
        connection.close()


def test_audit_has_direct_task_event_and_standard_trace_for_event_actions() -> None:
    connection = sqlite3.connect(":memory:")
    recorder = SQLiteAutomationRunRepository(connection)
    event_id = "local-event-1"
    task_id = "task-reconcile-1"
    source = AlarmAction(
        action_type=AlarmActionType.CREATE_ALARM_EVENT,
        device_id="TEST-TH-01",
        alarm_id=event_id,
    )
    action = ApplicationAction(
        action_type=AlarmActionType.CREATE_ALARM_EVENT,
        kind=ApplicationActionKind.EVENT,
        device_id="TEST-TH-01",
        source=source,
        alarm_id=event_id,
        task_id=task_id,
        dedupe_key=f"RECONCILE_ALARM_EVENT:{event_id}",
    )
    executor = ActionExecutor(
        mode=AutomationMode.ACTIVE,
        active_device_ids=("TEST-TH-01",),
        standards_ready_provider=lambda: True,
        context_handlers={AlarmActionType.CREATE_ALARM_EVENT: lambda _a, _c: None},
        recorder=recorder,
    )
    try:
        execution = executor.execute(
            [action],
            context={
                "device_id": "TEST-TH-01",
                "sample_time": BASE_TIME.isoformat(),
                "python_monitor_result": {
                    "standard_id": "STD-TEST-01",
                    "standard_revision": "R1",
                    "standard_source": "feishu:test",
                },
                "python_alarm_transition": {"active_alarm_id": event_id},
            },
            created_at=BASE_TIME,
        )
        assert execution[0].status is ActionExecutionStatus.SUCCEEDED
        row = connection.execute(
            """
            SELECT alarm_id, event_id, automation_task_id, dedupe_key,
                   standard_id, standard_revision, standard_source
            FROM automation_runs
            WHERE action_type = 'CREATE_ALARM_EVENT'
            """
        ).fetchone()
        assert row is not None
        assert tuple(row) == (
            event_id,
            event_id,
            task_id,
            f"RECONCILE_ALARM_EVENT:{event_id}",
            "STD-TEST-01",
            "R1",
            "feishu:test",
        )
    finally:
        connection.close()


def test_automation_run_migration_is_additive_and_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE automation_runs (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            sample_time TEXT,
            mode TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_status TEXT NOT NULL,
            alarm_id TEXT,
            planned_run_at TEXT,
            python_monitor_result_json TEXT,
            python_alarm_transition_json TEXT,
            feishu_observed_state_json TEXT,
            matched INTEGER,
            difference_type TEXT,
            details_json TEXT,
            context_json TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            standard_id TEXT,
            standard_revision TEXT,
            standard_source TEXT
        )
        """
    )
    connection.commit()
    try:
        SQLiteAutomationRunRepository(connection)
        SQLiteAutomationRunRepository(connection)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(automation_runs)")
        }
        assert {"event_id", "automation_task_id", "dedupe_key"} <= columns
    finally:
        connection.close()


def test_isolated_service_preserves_full_lifecycle_and_one_event() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    event_repository = SQLiteEnvironmentEventRepository(connection)
    task_repository = SQLiteAutomationTaskRepository(connection)
    alarm_repository = SQLiteAlarmStateRepository(connection)
    recorder = SQLiteAutomationRunRepository(connection)
    remote = _RemoteFeishu()
    writer = FeishuEnvironmentEventWriter(
        writer=remote,
        source=remote,
        event_table_id="events",
        device_table_id="devices",
        event_repository=event_repository,
    )
    device = DeviceContext(
        device_id="TEST-TH-01",
        area="TEST AREA",
        control_type=ControlType.ALL_DAY,
    )
    standard = EnvironmentStandard(
        standard_id="STD-TEST-01",
        revision="R1",
        area="TEST AREA",
        operation_type=None,
        temperature_min=20.0,
        temperature_max=26.0,
        humidity_min=40.0,
        humidity_max=60.0,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_to=None,
        source_document="isolated",
        clause="E2E",
        control_type=ControlType.ALL_DAY,
        standard_source="feishu:test",
    )

    class _OperationProvider:
        def get(self, _device: DeviceContext) -> OperationState:
            return OperationState(
                area_id="TEST AREA",
                state=OperationStatus.OPERATING,
                operation_type=None,
                work_order=None,
                started_at=None,
                ended_at=None,
            )

    executor = ActionExecutor(
        mode=AutomationMode.ACTIVE,
        active_device_ids=("TEST-TH-01",),
        standards_ready_provider=lambda: True,
        handlers={
            AlarmActionType.CREATE_VERIFY_TASK: lambda _a: None,
            AlarmActionType.COMPLETE_VERIFY_TASK: lambda _a: None,
            AlarmActionType.CANCEL_VERIFY_TASK: lambda _a: None,
        },
        context_handlers={
            AlarmActionType.CREATE_ALARM_EVENT: writer.handle_alarm_action,
            AlarmActionType.UPDATE_ALARM_EVENT: writer.handle_alarm_action,
            AlarmActionType.START_RECOVERY: writer.handle_alarm_action,
            AlarmActionType.MARK_ALARM_RECOVERED: writer.handle_alarm_action,
        },
        recorder=recorder,
    )
    service = MonitorApplicationService(
        operation_state_provider=_OperationProvider(),
        standard_resolver=StaticStandardResolver((standard,)),
        alarm_state_repository=alarm_repository,
        alarm_state_machine=AlarmStateMachine(
            verify_after=timedelta(minutes=5),
            recovery_after=timedelta(minutes=1),
        ),
        action_mapper=ApplicationActionMapper(),
        action_executor=executor,
        task_repository=task_repository,
        event_repository=event_repository,
    )

    def run(at: datetime, temperature: float):
        return service.handle_sample(
            device=device,
            sample=MonitorSample(
                device_id="TEST-TH-01",
                sample_time=at,
                temperature=temperature,
                humidity=50.0,
            ),
            now=at,
        )

    try:
        states = [
            run(BASE_TIME, 24.0).transition.next.state.value,
            run(BASE_TIME + timedelta(minutes=1), 28.0).transition.next.state.value,
            run(BASE_TIME + timedelta(minutes=6), 28.0).transition.next.state.value,
            run(BASE_TIME + timedelta(minutes=7), 28.0).transition.next.state.value,
            run(BASE_TIME + timedelta(minutes=8), 24.0).transition.next.state.value,
            run(BASE_TIME + timedelta(minutes=9), 24.0).transition.next.state.value,
        ]
        assert states == ["NORMAL", "PENDING", "ALARM", "ALARM", "RECOVERY", "NORMAL"]
        assert remote.create_calls == 1
        assert len(remote.events) == 1
        assert len(event_repository.list_active(device_id="TEST-TH-01")) == 0
        event_rows = connection.execute(
            """
            SELECT action_type, alarm_id, event_id, automation_task_id,
                   dedupe_key, standard_id, standard_revision, standard_source
            FROM automation_runs
            WHERE action_type IN ('CREATE_ALARM_EVENT', 'UPDATE_ALARM_EVENT', 'MARK_ALARM_RECOVERED')
            ORDER BY rowid
            """
        ).fetchall()
        assert [row[0] for row in event_rows] == [
            "CREATE_ALARM_EVENT",
            "UPDATE_ALARM_EVENT",
            "MARK_ALARM_RECOVERED",
        ]
        assert all(row[1] and row[2] and row[3] and row[4] for row in event_rows)
        assert all(tuple(row[5:]) == ("STD-TEST-01", "R1", "feishu:test") for row in event_rows)
        assert len({row[2] for row in event_rows}) == 1
        assert len({row[3] for row in event_rows}) == 3
    finally:
        connection.close()


def test_shadow_event_action_stays_planned_and_never_calls_writer() -> None:
    calls: list[str] = []
    action = AlarmAction(
        action_type=AlarmActionType.CREATE_ALARM_EVENT,
        device_id="TEST-TH-01",
        alarm_id="local-event-shadow",
    )
    executor = ActionExecutor(
        mode=AutomationMode.SHADOW,
        context_handlers={
            AlarmActionType.CREATE_ALARM_EVENT: lambda _a, _c: calls.append("write"),
        },
    )
    execution = executor.execute(
        [action],
        context={"device_id": "TEST-TH-01"},
    )
    assert execution[0].status is ActionExecutionStatus.PLANNED
    assert calls == []
