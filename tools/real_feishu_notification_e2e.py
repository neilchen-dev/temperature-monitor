"""Isolated real Feishu IM E2E for the Python notification chain.

This runner is intentionally separate from production bootstrap/configuration:

* the Base and device are the dedicated E2E fixtures from
  ``real_feishu_e2e.py``;
* the only IM recipient is an explicitly supplied private E2E chat;
* SQLite is in-memory and is never the production database;
* the message text is forced to contain ``E2E_TEST`` via the isolated area;
* no workflow, production table, or deployment is changed.

The first alarm attempt injects a timeout before the network call.  The same
task is then retried by the real scheduler handler, and only that retry calls
the real Feishu IM API.  Replays are checked against the local external-effect
marker without making another API call.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Mapping
from uuid import uuid4

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

# ruff: noqa: E402
import config
from application.action_executor import ActionExecutionStatus, ActionExecutor, AutomationMode
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmActionType,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationState,
    OperationStatus,
)
from integrations.feishu_notifications import FeishuNotificationError, FeishuNotificationWriter
from integrations.feishu_writers import FeishuEnvironmentEventWriter
from repositories.automation_runs import SQLiteAutomationRunRepository
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.runtime_state import SQLiteAlarmStateRepository, SQLiteLatestSampleRepository
from repositories.standard_resolver import SQLiteStandardRepository, SQLiteStandardResolver
from scheduler.worker import TaskScheduler
from tools.real_feishu_e2e import (
    DEVICE_ID,
    DEVICE_TABLE_ID,
    EVENT_TABLE_ID,
    LiveCliSource,
    LiveCliWriter,
    remote_cycle_records,
    verify_isolation,
)


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


class LarkCliMessageSender:
    """Use the already-authorized bot identity for this isolated E2E only."""

    def __init__(self) -> None:
        self.cli = shutil.which("lark-cli") or "lark-cli.cmd"

    def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        del receive_id_type, max_attempts, timeout
        completed = subprocess.run(
            [
                self.cli,
                "im",
                "+messages-send",
                "--as",
                "bot",
                "--chat-id",
                receive_id,
                "--text",
                text,
                "--idempotency-key",
                idempotency_key or "E2E_TEST_no_key",
                "--format",
                "json",
            ],
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise FeishuNotificationError(
                f"real Feishu CLI IM send failed: {detail[-1000:]}",
                error_code="feishu_cli_send_failed",
                retryable=False,
                outcome_unknown=False,
                recipient=receive_id,
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FeishuNotificationError(
                "real Feishu CLI IM response was not JSON",
                error_code="invalid_response",
                retryable=False,
                outcome_unknown=True,
                recipient=receive_id,
            ) from exc
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            raise FeishuNotificationError(
                "real Feishu CLI IM response was not successful",
                error_code="feishu_cli_send_failed",
                retryable=False,
                outcome_unknown=True,
                recipient=receive_id,
            )
        return value


class TimeoutBeforeSendOnce:
    """Inject one pre-send timeout, then delegate to the real IM client."""

    def __init__(self) -> None:
        self.client = LarkCliMessageSender()
        self.calls = 0
        self.remote_calls = 0
        self.successes: list[dict[str, Any]] = []
        self.texts: list[str] = []

    def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        self.calls += 1
        self.texts.append(str(text))
        if self.calls == 1:
            raise FeishuNotificationError(
                "E2E_TEST timeout injected before Feishu request",
                error_code="network_error",
                retryable=True,
                outcome_unknown=False,
                recipient=receive_id,
            )
        self.remote_calls += 1
        response = self.client.send_text(
            receive_id,
            receive_id_type,
            text,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            timeout=timeout,
        )
        self.successes.append(dict(response))
        return response


@dataclass
class Harness:
    connection: sqlite3.Connection
    tasks: SQLiteAutomationTaskRepository
    events: SQLiteEnvironmentEventRepository
    audit: SQLiteAutomationRunRepository
    service: MonitorApplicationService
    sender: TimeoutBeforeSendOnce
    device: DeviceContext
    area: str
    start: datetime


def build_harness(*, marker: str, chat_id: str, start: datetime) -> Harness:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    tasks = SQLiteAutomationTaskRepository(connection)
    events = SQLiteEnvironmentEventRepository(connection)
    audit = SQLiteAutomationRunRepository(connection)
    alarm_states = SQLiteAlarmStateRepository(connection)
    latest_samples = SQLiteLatestSampleRepository(connection)

    area = f"E2E_TEST_AREA_{marker}"
    standard = EnvironmentStandard(
        standard_id="STD-E2E-TEST",
        revision="R1",
        area=area,
        operation_type=None,
        temperature_min=20.0,
        temperature_max=26.0,
        humidity_min=40.0,
        humidity_max=60.0,
        effective_from=start - timedelta(hours=1),
        effective_to=None,
        source_document="E2E_TEST isolated standard",
        clause="E2E_TEST",
        control_type=ControlType.ALL_DAY,
        enabled=True,
        standard_source="feishu:e2e-test",
    )
    standard_repository = SQLiteStandardRepository(connection)
    standard_repository.apply_snapshot((standard,), source="feishu:e2e-test", synced_at=start)

    source = LiveCliSource()
    event_writer = FeishuEnvironmentEventWriter(
        writer=LiveCliWriter(test_marker=f"E2E_TEST_{marker}"),
        source=source,
        event_table_id=EVENT_TABLE_ID,
        device_table_id=DEVICE_TABLE_ID,
        event_repository=events,
    )
    sender = TimeoutBeforeSendOnce()
    notification_writer = FeishuNotificationWriter(
        sender=sender,
        source=source,
        event_table_id=EVENT_TABLE_ID,
        device_table_id=DEVICE_TABLE_ID,
        event_repository=events,
        # The dedicated chat is the only allowed recipient for this E2E.
        event_owner_field="E2E_TEST_OWNER_FIELD_NOT_PRESENT",
        device_owner_field="E2E_TEST_OWNER_FIELD_NOT_PRESENT",
    )

    def action_enabled(action: Any) -> bool:
        action_type = getattr(getattr(action, "action_type", None), "value", "")
        if action_type == AlarmActionType.NOTIFY_ALARM.value:
            return bool(config.FEISHU_ALARM_NOTIFY_ENABLED)
        if action_type == AlarmActionType.NOTIFY_RECOVERY.value:
            return bool(config.FEISHU_RECOVERY_NOTIFY_ENABLED)
        return True

    executor = ActionExecutor(
        mode=AutomationMode.ACTIVE,
        active_device_ids=(DEVICE_ID,),
        handlers={action_type: lambda _action: None for action_type in AlarmActionType},
        context_handlers={
            AlarmActionType.CREATE_ALARM_EVENT: event_writer.handle_alarm_action,
            AlarmActionType.UPDATE_ALARM_EVENT: event_writer.handle_alarm_action,
            AlarmActionType.START_RECOVERY: event_writer.handle_alarm_action,
            AlarmActionType.MARK_ALARM_RECOVERED: event_writer.handle_alarm_action,
            AlarmActionType.NOTIFY_ALARM: notification_writer.handle_notification_action,
            AlarmActionType.NOTIFY_RECOVERY: notification_writer.handle_notification_action,
        },
        recorder=audit,
        action_enabled_provider=action_enabled,
        standards_ready_provider=lambda: True,
    )
    device = DeviceContext(
        device_id=DEVICE_ID,
        area=area,
        name="E2E isolated test device",
        control_type=ControlType.ALL_DAY,
    )
    service = MonitorApplicationService(
        operation_state_provider=FixedOperationProvider(area=area),
        standard_resolver=SQLiteStandardResolver(standard_repository),
        alarm_state_repository=alarm_states,
        alarm_state_machine=AlarmStateMachine(
            verify_after=timedelta(minutes=5),
            recovery_after=timedelta(minutes=1),
        ),
        action_mapper=ApplicationActionMapper(emit_notifications=True),
        action_executor=executor,
        task_repository=tasks,
        event_repository=events,
        latest_sample_repository=latest_samples,
    )

    config.FEISHU_ALARM_NOTIFY_ENABLED = True
    config.FEISHU_RECOVERY_NOTIFY_ENABLED = True
    config.FEISHU_ALARM_CHAT_ID = chat_id
    config.FEISHU_NOTIFY_RECEIVE_ID_TYPE = "open_id"
    return Harness(connection, tasks, events, audit, service, sender, device, area, start)


def sample(at: datetime, temperature: float, humidity: float) -> MonitorSample:
    return MonitorSample(
        device_id=DEVICE_ID,
        sample_time=at,
        temperature=temperature,
        humidity=humidity,
        online_status="online",
    )


def notification_tasks(harness: Harness, task_type: str) -> list[Any]:
    rows = harness.connection.execute(
        "SELECT id FROM automation_tasks WHERE task_type = ? ORDER BY created_at, id",
        (task_type,),
    ).fetchall()
    return [harness.tasks.get(str(row[0])) for row in rows]


def run() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True)
    args = parser.parse_args()
    chat_id = str(args.chat_id).strip()
    if not chat_id.startswith("oc_"):
        raise SystemExit("refusing E2E: --chat-id must be a Feishu chat_id (oc_...)")

    verify_isolation()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    marker = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    harness = build_harness(marker=marker, chat_id=chat_id, start=start)

    normal_at = start
    pending_at = start + timedelta(minutes=1)
    alarm_at = start + timedelta(minutes=6)
    continued_at = start + timedelta(minutes=7)
    recovery_pending_at = start + timedelta(minutes=8)
    recovery_confirmed_at = start + timedelta(minutes=9)

    results = []
    results.append(
        harness.service.handle_sample(
            device=harness.device,
            sample=sample(normal_at, 24.0, 50.0),
            now=normal_at,
        )
    )
    results.append(
        harness.service.handle_sample(
            device=harness.device,
            sample=sample(pending_at, 28.0, 70.0),
            now=pending_at,
        )
    )
    alarm_result = harness.service.handle_sample(
        device=harness.device,
        sample=sample(alarm_at, 28.0, 70.0),
        now=alarm_at,
    )
    results.append(alarm_result)

    alarm_tasks = notification_tasks(harness, "NOTIFY_ALARM")
    if len(alarm_tasks) != 1 or alarm_tasks[0] is None:
        raise RuntimeError(f"expected exactly one NOTIFY_ALARM task, got {alarm_tasks}")
    alarm_task_id = alarm_tasks[0].task_id
    if harness.sender.remote_calls != 0:
        raise RuntimeError("E2E timeout injection unexpectedly reached Feishu")

    scheduler = TaskScheduler(
        repository=harness.tasks,
        worker_id="e2e-notification-worker",
        handlers={
            "NOTIFY_ALARM": lambda task: harness.service.execute_notification_task(
                task=task, now=alarm_at + timedelta(seconds=2)
            ),
            "NOTIFY_RECOVERY": lambda task: harness.service.execute_notification_task(
                task=task, now=recovery_confirmed_at
            ),
        },
        now_provider=lambda: alarm_at + timedelta(seconds=2),
    )
    retry_report = scheduler.run_once(now=alarm_at + timedelta(seconds=2), limit=1)
    alarm_task_after_retry = harness.tasks.get(alarm_task_id)
    if alarm_task_after_retry is None or alarm_task_after_retry.status.value != "SUCCEEDED":
        raise RuntimeError(f"alarm retry did not succeed: {alarm_task_after_retry}")
    if harness.sender.remote_calls != 1:
        raise RuntimeError(f"expected one real alarm send, got {harness.sender.remote_calls}")

    harness.service.handle_sample(
        device=harness.device,
        sample=sample(continued_at, 29.0, 71.0),
        now=continued_at,
    )
    if len(notification_tasks(harness, "NOTIFY_ALARM")) != 1:
        raise RuntimeError("continuous ALARM created a duplicate notification task")
    if harness.sender.remote_calls != 1:
        raise RuntimeError("continuous ALARM caused a duplicate real message")

    results.append(
        harness.service.handle_sample(
            device=harness.device,
            sample=sample(recovery_pending_at, 24.0, 50.0),
            now=recovery_pending_at,
        )
    )
    recovery_result = harness.service.handle_sample(
        device=harness.device,
        sample=sample(recovery_confirmed_at, 24.0, 50.0),
        now=recovery_confirmed_at,
    )
    results.append(recovery_result)
    recovery_tasks = notification_tasks(harness, "NOTIFY_RECOVERY")
    if len(recovery_tasks) != 1 or recovery_tasks[0] is None:
        raise RuntimeError(f"expected exactly one NOTIFY_RECOVERY task, got {recovery_tasks}")
    recovery_task_id = recovery_tasks[0].task_id
    if harness.sender.remote_calls != 2:
        raise RuntimeError(f"expected one real recovery send, got {harness.sender.remote_calls}")

    # Replay both successful actions.  The local marker must short-circuit the
    # sender, so this adds no external call and no message_id.
    for result in (alarm_result, recovery_result):
        action = next(
            action
            for action in result.actions
            if action.action_type
            in {AlarmActionType.NOTIFY_ALARM, AlarmActionType.NOTIFY_RECOVERY}
        )
        execution = next(
            execution
            for execution in result.executions
            if execution.action.action_type is action.action_type
        )
        replay = harness.service.action_executor.execute(
            (action,), context=dict(execution.context), created_at=recovery_confirmed_at
        )[0]
        if replay.status is not ActionExecutionStatus.SUCCEEDED:
            raise RuntimeError(f"marker replay failed: {replay}")
        if replay.context.get("result") != "ALREADY_SENT":
            raise RuntimeError(f"marker replay was not recognized: {replay.context}")
    if harness.sender.remote_calls != 2:
        raise RuntimeError("marker replay caused a duplicate real message")

    message_ids = [
        response.get("data", {}).get("message_id")
        for response in harness.sender.successes
        if isinstance(response.get("data"), Mapping)
    ]
    if len(message_ids) != 2 or any(not item for item in message_ids):
        raise RuntimeError(f"real Feishu response did not provide two message_ids: {message_ids}")
    if any("E2E_TEST" not in text for text in harness.sender.texts):
        raise RuntimeError("every E2E message must contain E2E_TEST")

    event_id = recovery_result.transition.previous.active_alarm_id
    event = harness.events.get(event_id) if event_id else None
    event_records = remote_cycle_records(start + timedelta(minutes=1))
    audit_rows = harness.connection.execute(
        """
        SELECT action_type, action_status, event_id, automation_task_id,
               recipient, message_id, dedupe_key, standard_id,
               standard_revision, standard_source, result, error_code, sent_at
        FROM automation_runs
        WHERE action_type IN ('NOTIFY_ALARM', 'NOTIFY_RECOVERY')
        ORDER BY created_at, id
        """
    ).fetchall()
    audit_columns = [
        "action_type", "action_status", "event_id", "automation_task_id",
        "recipient", "message_id", "dedupe_key", "standard_id",
        "standard_revision", "standard_source", "result", "error_code", "sent_at",
    ]
    audit_trace = [dict(zip(audit_columns, row)) for row in audit_rows]
    if len([row for row in audit_trace if row["action_type"] == "NOTIFY_ALARM"]) < 2:
        raise RuntimeError("alarm retry/replay audit trace is incomplete")
    if len([row for row in audit_trace if row["action_type"] == "NOTIFY_RECOVERY"]) < 2:
        raise RuntimeError("recovery/replay audit trace is incomplete")

    return {
        "scenario": "real_feishu_notification_isolated_e2e",
        "test_base": "dedicated existing E2E Base (token omitted)",
        "test_event_table_id": EVENT_TABLE_ID,
        "test_device_table_id": DEVICE_TABLE_ID,
        "test_device_id": DEVICE_ID,
        "test_chat_id": chat_id,
        "test_marker": f"E2E_TEST_{marker}",
        "alarm_message_count": 1,
        "recovery_message_count": 1,
        "duplicate": 0,
        "message_ids": message_ids,
        "recipient": chat_id,
        "alarm_task_id": alarm_task_id,
        "recovery_task_id": recovery_task_id,
        "alarm_task_identity_stable": alarm_task_id == alarm_tasks[0].task_id,
        "recovery_task_identity_stable": recovery_task_id == recovery_tasks[0].task_id,
        "retry_scheduler_report": retry_report.__dict__,
        "real_remote_send_calls": harness.sender.remote_calls,
        "local_event_id": event_id,
        "local_event_status": event.status if event else None,
        "remote_test_event_record_count": len(event_records),
        "remote_test_event_record_ids": [record.record_id for record in event_records],
        "event_payload": dict(event.payload) if event else None,
        "audit_trace": audit_trace,
        "production_active_enabled": False,
        "production_workflow_modified": False,
        "production_sqlite_modified": False,
        "production_tables_modified": False,
        "deploy_or_push": False,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
