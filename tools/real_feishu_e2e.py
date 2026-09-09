"""Isolated real-Feishu E2E for temperature-monitor.

This runner deliberately uses lark-cli's already-authorized user identity and
the dedicated E2E Base.  It never imports production configuration and never
opens the production SQLite file.  The three fault scenarios use real API
requests against the test table, with deterministic adapter-side fault
injection:

* timeout after a successful CREATE request;
* a schema-rejected CREATE request followed by a normal retry;
* a schema-rejected recovery UPDATE request followed by a normal retry.

The test Base is intentionally not cleaned up; its rows are audit evidence.
"""

# The workspace path must be inserted before importing the application package
# when this file is executed as ``python tools/real_feishu_e2e.py``.
# ruff: noqa: E402

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping
from uuid import uuid4

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))
LARK_CLI = shutil.which("lark-cli") or "lark-cli.cmd"

from application.action_executor import ActionExecutionStatus, ActionExecutor, AutomationMode
from application.actions import ApplicationAction, ApplicationActionKind
from application.monitor_service import (
    MonitorApplicationService,
    _monitor_result_dict,
    _operation_state_dict,
    _sample_dict,
    _transition_dict,
)
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
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import (
    FeishuCreateNotPersistedError,
    FeishuEnvironmentEventWriter,
    FeishuRecordWriter,
    FeishuWriteError,
)
from repositories.automation_runs import SQLiteAutomationRunRepository
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.runtime_state import SQLiteAlarmStateRepository, SQLiteLatestSampleRepository
from repositories.standard_resolver import SQLiteStandardRepository, SQLiteStandardResolver
from services.event_identity import epoch_milliseconds, external_effect_key


def _e2e_setting(name: str, default: str = "") -> str:
    """Read E2E identities from the environment, never from source code."""
    return os.environ.get(name, default).strip()


# These values are intentionally empty in the repository.  A real isolated
# run must provide dedicated test identities and the production identity only
# for the non-overlap safety check through environment variables.
BASE_TOKEN = _e2e_setting("TEMPERATURE_MONITOR_E2E_BASE_TOKEN")
EVENT_TABLE_ID = _e2e_setting("TEMPERATURE_MONITOR_E2E_EVENT_TABLE_ID")
DEVICE_TABLE_ID = _e2e_setting("TEMPERATURE_MONITOR_E2E_DEVICE_TABLE_ID")
PRODUCTION_BASE_TOKEN = _e2e_setting(
    "TEMPERATURE_MONITOR_E2E_PRODUCTION_BASE_TOKEN"
)
PRODUCTION_EVENT_TABLE_ID = _e2e_setting(
    "TEMPERATURE_MONITOR_E2E_PRODUCTION_EVENT_TABLE_ID"
)
DEVICE_ID = _e2e_setting(
    "TEMPERATURE_MONITOR_E2E_DEVICE_ID", "E2E-TEST-TH-01"
)


def cli_json(*args: str) -> Mapping[str, Any]:
    completed = subprocess.run(
        [LARK_CLI, *args],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"lark-cli failed rc={completed.returncode}: {detail[-1200:]}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli returned non-JSON output: {completed.stdout[-1200:]}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("lark-cli returned a non-object JSON envelope")
    return value


def list_remote_records(table_id: str = EVENT_TABLE_ID) -> tuple[FeishuRawRecord, ...]:
    envelope = cli_json(
        "base",
        "+record-list",
        "--base-token",
        BASE_TOKEN,
        "--table-id",
        table_id,
        "--format",
        "json",
        "--as",
        "user",
        "--limit",
        "200",
    )
    outer_data = envelope.get("data", {})
    if not isinstance(outer_data, Mapping):
        raise RuntimeError("record-list response data is not an object")
    rows = outer_data.get("data", [])
    field_names = outer_data.get("fields", [])
    record_ids = outer_data.get("record_id_list", [])
    if not isinstance(rows, list) or not isinstance(field_names, list) or not isinstance(record_ids, list):
        raise RuntimeError("record-list response has an unexpected tabular shape")
    records: list[FeishuRawRecord] = []
    for index, row in enumerate(rows):
        if index >= len(record_ids) or not isinstance(row, list):
            raise RuntimeError("record-list response row/id count mismatch")
        fields = {
            str(name): row[field_index]
            for field_index, name in enumerate(field_names)
            if field_index < len(row)
        }
        records.append(FeishuRawRecord(record_id=str(record_ids[index]), fields=fields))
    return tuple(records)


class LiveCliSource:
    """FeishuEventSource backed by the real Base API through lark-cli."""

    def read_records(self, table_id: str) -> tuple[FeishuRawRecord, ...]:
        last_error: RuntimeError | None = None
        for attempt in range(3):
            try:
                return list_remote_records(table_id)
            except RuntimeError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1)
        assert last_error is not None
        raise last_error


@dataclass
class LiveCliWriter(FeishuRecordWriter):
    """Real Base writes plus test-only, deterministic transport fault injection."""

    test_marker: str
    create_fault: str | None = None
    update_fault: str | None = None
    create_calls: int = 0
    update_calls: int = 0

    def create(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        client_token: str | None = None,
    ) -> Mapping[str, Any]:
        del client_token  # lark-cli uses the real OpenAPI record endpoint.
        outbound = dict(fields)
        outbound["测试标识"] = self.test_marker
        if self.create_fault == "explicit_fail":
            # This is a real POST to the isolated table.  The deliberately
            # invalid field is rejected before a row is persisted.
            self.create_calls += 1
            outbound["E2E_TEST_INVALID_FIELD"] = "must_be_rejected"
            try:
                cli_json(
                    "base",
                    "+record-upsert",
                    "--base-token",
                    BASE_TOKEN,
                    "--table-id",
                    table_id,
                    "--json",
                    json.dumps(outbound, ensure_ascii=False, separators=(",", ":")),
                    "--as",
                    "user",
                )
            except Exception as exc:
                self.create_fault = None
                raise FeishuCreateNotPersistedError(
                    "isolated E2E explicit Feishu CREATE rejection; no remote row persisted"
                ) from exc
            raise RuntimeError("fault injection unexpectedly persisted an invalid CREATE")

        self.create_calls += 1
        response = cli_json(
            "base",
            "+record-upsert",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            table_id,
            "--json",
            json.dumps(outbound, ensure_ascii=False, separators=(",", ":")),
            "--as",
            "user",
        )
        data = response.get("data", {})
        record = data.get("record", {}) if isinstance(data, Mapping) else {}
        record_ids = record.get("record_id_list", []) if isinstance(record, Mapping) else []
        if not isinstance(record_ids, list) or not record_ids or not str(record_ids[0]).strip():
            raise RuntimeError("real Feishu CREATE response did not contain record_id")
        record_id = str(record_ids[0]).strip()
        if self.create_fault == "timeout_after_commit":
            self.create_fault = None
            raise TimeoutError("isolated E2E timeout injected after remote CREATE commit")
        return {"code": 0, "data": {"record": {"record_id": record_id}}}

    def update(
        self,
        table_id: str,
        record_id: str,
        fields: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        outbound = dict(fields)
        if self.update_fault == "explicit_fail":
            self.update_calls += 1
            outbound["E2E_TEST_INVALID_FIELD"] = "must_be_rejected"
            try:
                cli_json(
                    "base",
                    "+record-upsert",
                    "--base-token",
                    BASE_TOKEN,
                    "--table-id",
                    table_id,
                    "--record-id",
                    record_id,
                    "--json",
                    json.dumps(outbound, ensure_ascii=False, separators=(",", ":")),
                    "--as",
                    "user",
                )
            except Exception as exc:
                self.update_fault = None
                raise FeishuWriteError(
                    "isolated E2E explicit Feishu recovery UPDATE rejection"
                ) from exc
            raise RuntimeError("fault injection unexpectedly persisted an invalid UPDATE")

        self.update_calls += 1
        cli_json(
            "base",
            "+record-upsert",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            table_id,
            "--record-id",
            record_id,
            "--json",
            json.dumps(outbound, ensure_ascii=False, separators=(",", ":")),
            "--as",
            "user",
        )
        return {"code": 0, "data": {"record_id": record_id}}


class FixedOperationProvider:
    def __init__(self, *, area: str) -> None:
        self.area = area

    def get(self, device: DeviceContext) -> OperationState:
        return OperationState(
            area_id=self.area,
            state=OperationStatus.OPERATING,
            operation_type=None,
            work_order=None,
            started_at=None,
            ended_at=None,
        )


@dataclass
class LocalHarness:
    connection: sqlite3.Connection
    task_repository: SQLiteAutomationTaskRepository
    event_repository: SQLiteEnvironmentEventRepository
    audit_repository: SQLiteAutomationRunRepository
    action_executor: ActionExecutor
    event_writer: FeishuEnvironmentEventWriter
    cli_writer: LiveCliWriter
    service: MonitorApplicationService
    device: DeviceContext
    area: str
    standard: EnvironmentStandard


def new_harness(*, marker: str, start: datetime) -> LocalHarness:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    task_repository = SQLiteAutomationTaskRepository(connection)
    event_repository = SQLiteEnvironmentEventRepository(connection)
    audit_repository = SQLiteAutomationRunRepository(connection)
    alarm_state_repository = SQLiteAlarmStateRepository(connection)
    latest_sample_repository = SQLiteLatestSampleRepository(connection)

    area = f"E2E_TEST_AREA_{marker}"
    standard = EnvironmentStandard(
        standard_id="STD-E2E-TEST",
        revision="R1",
        area=area,
        operation_type=None,
        temperature_min=20,
        temperature_max=26,
        humidity_min=40,
        humidity_max=60,
        effective_from=start - timedelta(hours=1),
        effective_to=None,
        source_document="E2E_TEST isolated validated standard",
        clause="E2E_TEST",
        control_type=ControlType.ALL_DAY,
        enabled=True,
        standard_source="feishu:e2e-test",
    )
    standard_repository = SQLiteStandardRepository(connection)
    standard_repository.apply_snapshot(
        (standard,),
        source="feishu:e2e-test",
        synced_at=start,
    )
    standard_resolver = SQLiteStandardResolver(standard_repository)

    cli_writer = LiveCliWriter(test_marker=f"E2E_TEST_{marker}")
    event_writer = FeishuEnvironmentEventWriter(
        writer=cli_writer,
        source=LiveCliSource(),
        event_table_id=EVENT_TABLE_ID,
        device_table_id=DEVICE_TABLE_ID,
        event_repository=event_repository,
    )
    event_action_types = {
        AlarmActionType.CREATE_ALARM_EVENT,
        AlarmActionType.UPDATE_ALARM_EVENT,
        AlarmActionType.START_RECOVERY,
        AlarmActionType.MARK_ALARM_RECOVERED,
    }
    context_handlers = {
        action_type: event_writer.handle_alarm_action
        for action_type in event_action_types
    }
    action_executor = ActionExecutor(
        mode=AutomationMode.ACTIVE,
        handlers={action_type: lambda _action: None for action_type in AlarmActionType},
        context_handlers=context_handlers,
        active_device_ids=(DEVICE_ID,),
        recorder=audit_repository,
        standards_ready_provider=lambda: True,
    )
    device = DeviceContext(
        device_id=DEVICE_ID,
        area=area,
        name="E2E isolated device",
        control_type=ControlType.ALL_DAY,
    )
    service = MonitorApplicationService(
        operation_state_provider=FixedOperationProvider(area=area),
        standard_resolver=standard_resolver,
        alarm_state_repository=alarm_state_repository,
        alarm_state_machine=AlarmStateMachine(
            verify_after=timedelta(minutes=5),
            recovery_after=timedelta(minutes=1),
        ),
        action_mapper=__import__("application.actions", fromlist=["ApplicationActionMapper"]).ApplicationActionMapper(),
        action_executor=action_executor,
        task_repository=task_repository,
        event_repository=event_repository,
        latest_sample_repository=latest_sample_repository,
    )
    return LocalHarness(
        connection=connection,
        task_repository=task_repository,
        event_repository=event_repository,
        audit_repository=audit_repository,
        action_executor=action_executor,
        event_writer=event_writer,
        cli_writer=cli_writer,
        service=service,
        device=device,
        area=area,
        standard=standard,
    )


def make_sample(at: datetime, temperature: float, humidity: float = 50) -> MonitorSample:
    return MonitorSample(
        device_id=DEVICE_ID,
        sample_time=at,
        temperature=temperature,
        humidity=humidity,
        online_status="online",
    )


def service_context(result: Any, sample: MonitorSample, evaluated_at: datetime) -> dict[str, Any]:
    return {
        "device_id": DEVICE_ID,
        "created_at": evaluated_at.isoformat(),
        "sample_time": sample.sample_time.isoformat(),
        "sample": _sample_dict(sample),
        "python_monitor_result": _monitor_result_dict(result.monitor_result),
        "python_alarm_transition": _transition_dict(result.transition),
        "operation_state": _operation_state_dict(result.operation_state),
    }


def remote_cycle_records(start: datetime) -> tuple[FeishuRawRecord, ...]:
    expected = epoch_milliseconds(start)
    return tuple(
        record
        for record in list_remote_records()
        if str(record.fields.get("监测点", "")).strip().upper() == DEVICE_ID
        and _numeric_epoch(record.fields.get("开始时间")) == expected
    )


def _numeric_epoch(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return epoch_milliseconds(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def task_health(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    status_rows = connection.execute(
        "SELECT status, COUNT(*) AS count FROM automation_tasks GROUP BY status ORDER BY status"
    ).fetchall()
    failed = connection.execute(
        "SELECT COUNT(*) FROM automation_tasks WHERE status = 'FAILED'"
    ).fetchone()[0]
    stale = connection.execute(
        """
        SELECT COUNT(*) FROM automation_tasks
        WHERE status = 'RUNNING' AND lease_until IS NOT NULL AND lease_until <= ?
        """,
        (now.isoformat(),),
    ).fetchone()[0]
    return {
        "by_status": {row[0]: row[1] for row in status_rows},
        "failed_count": int(failed),
        "stale_count": int(stale),
    }


def event_action_audit(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT action_type, action_status, event_id, alarm_id, automation_task_id,
               dedupe_key, standard_id, standard_revision, standard_source, error
        FROM automation_runs
        WHERE action_type IN (
            'CREATE_ALARM_EVENT', 'UPDATE_ALARM_EVENT',
            'START_RECOVERY', 'MARK_ALARM_RECOVERED'
        )
        ORDER BY created_at, id
        """
    ).fetchall()
    columns = [
        "action_type", "action_status", "event_id", "alarm_id",
        "automation_task_id", "dedupe_key", "standard_id",
        "standard_revision", "standard_source", "error",
    ]
    return [dict(zip(columns, row)) for row in rows]


def remote_summary(start: datetime, connection: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    records = remote_cycle_records(start)
    all_remote_records = list_remote_records()
    unclosed = tuple(
        record
        for record in records
        if str(record.fields.get("处理状态", "")).strip().lower()
        not in {"关闭", "已关闭", "closed"}
    )
    active_local = connection.execute(
        "SELECT COUNT(*) FROM environment_events WHERE status <> 'CLOSED'"
    ).fetchone()[0]
    duplicates = max(0, len(records) - 1)
    return {
        "remote_total_record_count_in_test_table": len(all_remote_records),
        "remote_cycle_record_count": len(records),
        "remote_cycle_record_ids": [record.record_id for record in records],
        "remote_cycle_unclosed_record_count": len(unclosed),
        "duplicate_count": duplicates,
        "local_active_event_count": int(active_local),
        "task_health": task_health(connection, now),
        "remote_fields": dict(records[0].fields) if records else None,
    }


def run_main_chain(marker: str) -> dict[str, Any]:
    start = datetime.now(timezone.utc).replace(microsecond=0)
    harness = new_harness(marker=marker, start=start)
    samples = [
        ("NORMAL", start, make_sample(start, 24)),
        ("PENDING", start + timedelta(minutes=1), make_sample(start + timedelta(minutes=1), 28)),
        ("ALARM", start + timedelta(minutes=6), make_sample(start + timedelta(minutes=6), 28)),
        ("ALARM_UPDATE", start + timedelta(minutes=7), make_sample(start + timedelta(minutes=7), 29)),
        ("RECOVERY", start + timedelta(minutes=8), make_sample(start + timedelta(minutes=8), 24)),
        ("NORMAL", start + timedelta(minutes=9), make_sample(start + timedelta(minutes=9), 24)),
    ]
    states: list[dict[str, Any]] = []
    results: list[Any] = []
    for label, evaluated_at, sample in samples:
        result = harness.service.handle_sample(
            device=harness.device,
            sample=sample,
            now=evaluated_at,
        )
        results.append(result)
        states.append(
            {
                "step": label,
                "monitor_result": result.monitor_result.overall_status.value,
                "state": result.transition.next.state.value,
                "actions": [action.action_type.value for action in result.actions],
                "execution_statuses": [execution.status.value for execution in result.executions],
            }
        )

    final = results[-1]
    mark_action = next(
        action for action in final.actions
        if action.action_type is AlarmActionType.MARK_ALARM_RECOVERED
    )
    before_duplicate_recovery = harness.cli_writer.update_calls
    duplicate_execution = harness.action_executor.execute(
        (mark_action,),
        context=service_context(final, samples[-1][2], samples[-1][1]),
        created_at=samples[-1][1],
    )
    after_duplicate_recovery = harness.cli_writer.update_calls
    local_event_id = final.transition.previous.active_alarm_id
    local_event = harness.event_repository.get(local_event_id) if local_event_id else None
    return {
        "scenario": "main_lifecycle",
        "start_time": start.isoformat(),
        "violation_started_at": (start + timedelta(minutes=1)).isoformat(),
        "states": states,
        "create_calls": harness.cli_writer.create_calls,
        "update_calls": harness.cli_writer.update_calls,
        "duplicate_recovery_execution": [execution.status.value for execution in duplicate_execution],
        "duplicate_recovery_remote_update_delta": after_duplicate_recovery - before_duplicate_recovery,
        "local_event_id": local_event_id,
        "local_event_status": local_event.status if local_event else None,
        "local_event_payload": dict(local_event.payload) if local_event else None,
        "audit_trace": event_action_audit(harness.connection),
        "summary": remote_summary(start + timedelta(minutes=1), harness.connection, now=samples[-1][1]),
    }


def direct_event_action(
    *,
    event_id: str,
    action_type: AlarmActionType,
    task_id: str,
    start: datetime,
    sample: MonitorSample,
    recovered_at: datetime | None = None,
    standard: EnvironmentStandard,
) -> ApplicationAction:
    source = AlarmAction(
        action_type=action_type,
        device_id=DEVICE_ID,
        alarm_id=event_id,
    )
    result = {
        "temperature_status": "HIGH" if sample.temperature > 26 else "NORMAL",
        "humidity_status": "NORMAL",
        "standard_id": standard.standard_id,
        "standard_revision": standard.revision,
        "standard_source": standard.standard_source,
    }
    effect = external_effect_key(
        action_type.value,
        event_id,
        sample_time=sample.sample_time,
        recovered_at=recovered_at or start,
        sample=_sample_dict(sample),
        result=result,
    )
    return ApplicationAction(
        action_type=action_type,
        kind=ApplicationActionKind.EVENT,
        device_id=DEVICE_ID,
        source=source,
        alarm_id=event_id,
        task_id=task_id,
        dedupe_key=f"RECONCILE_ALARM_EVENT:{event_id}",
        payload={"external_effect_key": effect, "reason": "isolated_e2e"},
    )


def direct_context(
    *,
    start: datetime,
    sample: MonitorSample,
    standard: EnvironmentStandard,
    recovered_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "device_id": DEVICE_ID,
        "created_at": (recovered_at or sample.sample_time).isoformat(),
        "sample_time": sample.sample_time.isoformat(),
        "sample": _sample_dict(sample),
        "python_monitor_result": {
            "temperature_status": "HIGH" if sample.temperature > 26 else "NORMAL",
            "humidity_status": "NORMAL",
            "standard_id": standard.standard_id,
            "standard_revision": standard.revision,
            "standard_source": standard.standard_source,
        },
        "python_alarm_transition": {
            "from": "ALARM",
            "to": "ALARM",
            "reason": "isolated_e2e",
            "violation_started_at": start.isoformat(),
            "alarm_started_at": start.isoformat(),
            "active_alarm_id": None,
        },
        "operation_state": {"area_id": standard.area},
        **({"recovered_at": recovered_at.isoformat()} if recovered_at else {}),
    }


def create_local_event(harness: LocalHarness, start: datetime) -> str:
    event = harness.event_repository.create_or_get_active(
        device_id=DEVICE_ID,
        event_key=f"ENV:{DEVICE_ID}:{start.isoformat()}",
        opened_at=start,
        payload={
            "projection": "isolated_e2e",
            "area": harness.area,
            "violation_started_at": start.isoformat(),
            "standard_id": harness.standard.standard_id,
            "standard_revision": harness.standard.revision,
            "standard_source": harness.standard.standard_source,
            "feishu_binding_status": "PENDING",
            "feishu_create_attempted": False,
        },
    )
    harness.event_repository.mark_external_binding_pending(event.event_id, requested_at=start)
    return event.event_id


def run_task_attempt(
    harness: LocalHarness,
    *,
    event_id: str,
    action_type: AlarmActionType,
    start: datetime,
    sample: MonitorSample,
    due_at: datetime,
    recovered_at: datetime | None = None,
    task_dedupe: str | None = None,
) -> tuple[Any, Any, Any]:
    dedupe = task_dedupe or f"RECONCILE_ALARM_EVENT:{event_id}"
    task = harness.task_repository.create_or_get_unfinished(
        task_type="RECONCILE_ALARM_EVENT",
        entity_type="DEVICE",
        entity_id=DEVICE_ID,
        due_at=due_at,
        payload={
            "local_event_id": event_id,
            "device_id": DEVICE_ID,
            "area": harness.area,
            "sample_time": sample.sample_time.isoformat(),
            "temperature": sample.temperature,
            "humidity": sample.humidity,
            "temperature_status": "HIGH" if sample.temperature > 26 else "NORMAL",
            "humidity_status": "NORMAL",
            "standard_id": harness.standard.standard_id,
            "standard_revision": harness.standard.revision,
            "standard_source": harness.standard.standard_source,
            "violation_started_at": start.isoformat(),
            "alarm_started_at": start.isoformat(),
            "retry_attempt": 0,
        },
        dedupe_key=dedupe,
        created_at=due_at,
    )
    claimed = harness.task_repository.claim_due(
        now=due_at,
        worker_id="real-feishu-e2e",
        lease_for=timedelta(minutes=5),
    )
    owned = next(item for item in claimed if item.task_id == task.task_id)
    action = direct_event_action(
        event_id=event_id,
        action_type=action_type,
        task_id=owned.task_id,
        start=start,
        sample=sample,
        recovered_at=recovered_at,
        standard=harness.standard,
    )
    context = direct_context(
        start=start,
        sample=sample,
        standard=harness.standard,
        recovered_at=recovered_at,
    )
    execution = harness.action_executor.execute((action,), context=context, created_at=due_at)[0]
    return owned, execution, action


def retry_same_task(
    harness: LocalHarness,
    *,
    owned: Any,
    action: ApplicationAction,
    start: datetime,
    sample: MonitorSample,
    retry_at: datetime,
    recovered_at: datetime | None = None,
) -> tuple[Any, Any]:
    harness.task_repository.reschedule_running(
        owned,
        due_at=retry_at,
        updated_at=retry_at,
        payload={**dict(owned.payload), "retry_attempt": owned.attempt_count},
    )
    claimed = harness.task_repository.claim_due(
        now=retry_at,
        worker_id="real-feishu-e2e",
        lease_for=timedelta(minutes=5),
    )
    retry_owned = next(item for item in claimed if item.task_id == owned.task_id)
    retry_action = ApplicationAction(
        **{
            **action.__dict__,
            "task_id": retry_owned.task_id,
        }
    )
    execution = harness.action_executor.execute(
        (retry_action,),
        context=direct_context(
            start=start,
            sample=sample,
            standard=harness.standard,
            recovered_at=recovered_at,
        ),
        created_at=retry_at,
    )[0]
    if execution.status is ActionExecutionStatus.SUCCEEDED:
        harness.task_repository.mark_succeeded(
            retry_owned.task_id,
            finished_at=retry_at,
            worker_id=retry_owned.worker_id,
        )
    return retry_owned, execution


def run_create_fault_scenarios(marker: str) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for name, fault in (
        ("create_timeout_after_remote_commit", "timeout_after_commit"),
        ("create_explicit_failure_then_retry", "explicit_fail"),
    ):
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)
        harness = new_harness(marker=f"{marker}_{name}", start=start)
        harness.cli_writer.create_fault = fault
        event_id = create_local_event(harness, start)
        sample = make_sample(start + timedelta(minutes=6), 28)
        owned, first, action = run_task_attempt(
            harness,
            event_id=event_id,
            action_type=AlarmActionType.CREATE_ALARM_EVENT,
            start=start,
            sample=sample,
            due_at=start + timedelta(minutes=6),
        )
        if first.status is ActionExecutionStatus.FAILED:
            retry_owned, second = retry_same_task(
                harness,
                owned=owned,
                action=action,
                start=start,
                sample=sample,
                retry_at=start + timedelta(minutes=7),
            )
        else:
            retry_owned, second = owned, None
            harness.task_repository.mark_succeeded(
                owned.task_id,
                finished_at=start + timedelta(minutes=6),
                worker_id=owned.worker_id,
            )
        local_event = harness.event_repository.get(event_id)
        scenarios.append(
            {
                "scenario": name,
                "start_time": start.isoformat(),
                "first_execution": first.status.value,
                "retry_execution": second.status.value if second else "NOT_NEEDED",
                "task_id": owned.task_id,
                "retry_task_id": retry_owned.task_id,
                "task_identity_unchanged": owned.task_id == retry_owned.task_id,
                "create_calls": harness.cli_writer.create_calls,
                "update_calls": harness.cli_writer.update_calls,
                "local_event_id": event_id,
                "local_binding": dict(local_event.payload) if local_event else None,
                "audit_trace": event_action_audit(harness.connection),
                "summary": remote_summary(start, harness.connection, now=start + timedelta(minutes=7)),
            }
        )
    return scenarios


def run_recovery_fault_scenario(marker: str) -> dict[str, Any]:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)
    recovery_at = start + timedelta(minutes=9)
    harness = new_harness(marker=f"{marker}_recovery", start=start)
    event_id = create_local_event(harness, start)
    sample = make_sample(start + timedelta(minutes=6), 28)
    create_owned, create_execution, _ = run_task_attempt(
        harness,
        event_id=event_id,
        action_type=AlarmActionType.CREATE_ALARM_EVENT,
        start=start,
        sample=sample,
        due_at=start + timedelta(minutes=6),
    )
    if create_execution.status is not ActionExecutionStatus.SUCCEEDED:
        raise RuntimeError(f"recovery fixture CREATE failed: {create_execution.error}")
    harness.task_repository.mark_succeeded(
        create_owned.task_id,
        finished_at=start + timedelta(minutes=6),
        worker_id=create_owned.worker_id,
    )
    harness.event_repository.patch_external_projection(
        event_id,
        feishu_recovery_pending=True,
        feishu_recovered_at=recovery_at.isoformat(),
    )
    harness.event_repository.mark_recovered(event_id, recovered_at=recovery_at)
    harness.cli_writer.update_fault = "explicit_fail"
    normal_sample = make_sample(recovery_at, 24)
    owned, first, action = run_task_attempt(
        harness,
        event_id=event_id,
        action_type=AlarmActionType.MARK_ALARM_RECOVERED,
        start=start,
        sample=normal_sample,
        due_at=recovery_at,
        recovered_at=recovery_at,
    )
    retry_owned, second = retry_same_task(
        harness,
        owned=owned,
        action=action,
        start=start,
        sample=normal_sample,
        retry_at=recovery_at + timedelta(minutes=1),
        recovered_at=recovery_at,
    )
    local_event = harness.event_repository.get(event_id)
    return {
        "scenario": "recovery_update_failure_then_retry",
        "start_time": start.isoformat(),
        "first_execution": first.status.value,
        "retry_execution": second.status.value,
        "task_id": owned.task_id,
        "retry_task_id": retry_owned.task_id,
        "task_identity_unchanged": owned.task_id == retry_owned.task_id,
        "create_calls": harness.cli_writer.create_calls,
        "update_calls": harness.cli_writer.update_calls,
        "local_event_id": event_id,
        "local_event_status": local_event.status if local_event else None,
        "local_event_payload": dict(local_event.payload) if local_event else None,
        "audit_trace": event_action_audit(harness.connection),
        "summary": remote_summary(start, harness.connection, now=recovery_at + timedelta(minutes=1)),
    }


def verify_isolation() -> None:
    required = {
        "TEMPERATURE_MONITOR_E2E_BASE_TOKEN": BASE_TOKEN,
        "TEMPERATURE_MONITOR_E2E_EVENT_TABLE_ID": EVENT_TABLE_ID,
        "TEMPERATURE_MONITOR_E2E_DEVICE_TABLE_ID": DEVICE_TABLE_ID,
        "TEMPERATURE_MONITOR_E2E_PRODUCTION_BASE_TOKEN": PRODUCTION_BASE_TOKEN,
        "TEMPERATURE_MONITOR_E2E_PRODUCTION_EVENT_TABLE_ID": PRODUCTION_EVENT_TABLE_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "real Feishu E2E requires dedicated identities in environment: "
            + ", ".join(missing)
        )
    if BASE_TOKEN == PRODUCTION_BASE_TOKEN or EVENT_TABLE_ID == PRODUCTION_EVENT_TABLE_ID:
        raise RuntimeError("test Base/table identity overlaps production event table")
    records = list_remote_records()
    # A dedicated table may contain evidence from an earlier interrupted run,
    # but every row must be visibly an E2E row before this run proceeds.
    for record in records:
        marker = str(record.fields.get("测试标识", ""))
        device = str(record.fields.get("监测点", ""))
        if not marker.startswith("E2E_TEST_") or device.upper() != DEVICE_ID:
            raise RuntimeError(
                "isolated event table contains a non-E2E/non-dedicated-device row; refusing to write"
            )


def main() -> int:
    verify_isolation()
    marker = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    main_chain = run_main_chain(marker)
    create_faults = run_create_fault_scenarios(marker)
    recovery_fault = run_recovery_fault_scenario(marker)
    report = {
        "commit_base": "e8e302f8212242b69e54d714833b6ed3b9cec28b",
        "working_tree": "uncommitted blocker fixes present; no new code changed by this runner",
        "feishu_identity": "user (authorized)",
        "test_base_configured": bool(BASE_TOKEN),
        "test_event_table_configured": bool(EVENT_TABLE_ID),
        "test_device_table_configured": bool(DEVICE_TABLE_ID),
        "production_identity_overlap_checked": True,
        "test_device_id": DEVICE_ID,
        "test_marker_prefix": "E2E_TEST_",
        "main_chain": main_chain,
        "fault_scenarios": create_faults + [recovery_fault],
        "remote_rows_retained_as_audit_evidence": True,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
