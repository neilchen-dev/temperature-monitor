from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import config
from application.action_executor import ActionExecutionStatus, ActionExecutor
from application.actions import ApplicationAction, ApplicationActionKind, ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmAction,
    AlarmActionType,
    AlarmLifecycleState,
    AlarmState,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorResult,
    MonitorSample,
    OverallStatus,
    OperationState,
    OperationStatus,
    TemperatureStatus,
)
from integrations.feishu_notifications import (
    FeishuNotificationError,
    FeishuNotificationWriter,
)
from integrations.feishu_records import FeishuRawRecord
from repositories.automation_runs import SQLiteAutomationRunRepository
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from scheduler.worker import TaskScheduler
from services.feishu import FeishuIMClient, FeishuIMError
from domain.standard_resolver import StaticStandardResolver


UTC = timezone.utc


class _Source:
    def __init__(self, records: dict[str, tuple[FeishuRawRecord, ...]]) -> None:
        self.records = records

    def read_records(self, table_id: str):
        return self.records.get(table_id, ())


class _Sender:
    def __init__(self, outcomes=None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[dict[str, object]] = []
        self.counter = 0

    def send_text(self, receive_id, receive_id_type, text, **kwargs):
        self.calls.append(
            {
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "text": text,
                **kwargs,
            }
        )
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        self.counter += 1
        return {"code": 0, "data": {"message_id": f"om_test_{self.counter}"}}


class NotificationTestCase(unittest.TestCase):
    event_table = "tbl-event"
    device_table = "tbl-device"

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.events = SQLiteEnvironmentEventRepository(self.connection)
        self.tasks = SQLiteAutomationTaskRepository(self.connection)
        self.runs = SQLiteAutomationRunRepository(self.connection)
        self.now = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)
        self.event = self.events.create_or_get_active(
            device_id="TH-01",
            event_key="ENV:TH-01:2026-09-09T09:55:00+00:00",
            opened_at=self.now - timedelta(minutes=5),
            payload={
                "feishu_record_id": "rec-event-1",
                "violation_started_at": "2026-09-09T09:55:00+00:00",
            },
        )
        self.source = _Source(
            {
                self.event_table: (
                    FeishuRawRecord(
                        "rec-event-1",
                        {"责任人": [{"open_id": "ou_owner_1"}]},
                    ),
                ),
                self.device_table: (
                    FeishuRawRecord(
                        "rec-device-1",
                        {"设备编号": "TH-01", "默认异常责任人": "ou_device_1"},
                    ),
                ),
            }
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _writer(self, sender=None) -> FeishuNotificationWriter:
        return FeishuNotificationWriter(
            sender=sender or _Sender(),
            source=self.source,
            event_table_id=self.event_table,
            device_table_id=self.device_table,
            event_repository=self.events,
            event_table_url="https://feishu.test/base/x?record={record_id}",
        )

    def test_alarm_message_includes_configured_closure_form_url(self) -> None:
        writer = FeishuNotificationWriter(
            sender=_Sender(),
            source=self.source,
            event_table_id=self.event_table,
            device_table_id=self.device_table,
            event_repository=self.events,
            alarm_form_url="https://feishu.test/share/base/form/test-closure",
        )

        message = writer._message(
            action_type=AlarmActionType.NOTIFY_ALARM.value,
            event_id=self.event.event_id,
            record_id="rec-event-1",
            context=self._context(),
        )

        self.assertIn("异常处置登记表：https://feishu.test/share/base/form/test-closure", message)

    def test_alarm_notification_sends_configured_closure_form_url(self) -> None:
        sender = _Sender()
        writer = FeishuNotificationWriter(
            sender=sender,
            source=self.source,
            event_table_id=self.event_table,
            device_table_id=self.device_table,
            event_repository=self.events,
            alarm_form_url="https://feishu.test/share/base/form/test-closure",
        )

        writer.handle_notification_action(
            self._action(
                AlarmActionType.NOTIFY_ALARM,
                f"NOTIFY_ALARM:{self.event.event_id}",
            ),
            self._context(),
        )

        self.assertEqual(len(sender.calls), 1)
        self.assertIn(
            "异常处置登记表：https://feishu.test/share/base/form/test-closure",
            sender.calls[0]["text"],
        )

    def _action(self, action_type: AlarmActionType, key: str) -> ApplicationAction:
        return ApplicationAction(
            action_type=action_type,
            kind=ApplicationActionKind.NOTIFICATION,
            device_id="TH-01",
            source=AlarmAction(
                action_type=action_type,
                device_id="TH-01",
                alarm_id=self.event.event_id,
            ),
            alarm_id=self.event.event_id,
            dedupe_key=key,
        )

    def _context(self, *, recovery: bool = False) -> dict[str, object]:
        return {
            "device_id": "TH-01",
            "event_id": self.event.event_id,
            "created_at": self.now.isoformat(),
            "sample_time": self.now.isoformat(),
            "sample": {"temperature": 31.2, "humidity": 71.0},
            "operation_state": {"area_id": "仓库"},
            "python_monitor_result": {
                "temperature_status": "HIGH",
                "humidity_status": "HIGH",
                "reasons": ["temperature_high", "humidity_high"],
                "standard_id": "STD-1",
                "standard_revision": "R2",
                "standard_source": "feishu:tbl-standard",
            },
            "standard": {
                "standard_id": "STD-1",
                "revision": "R2",
                "temperature_min": 20,
                "temperature_max": 26,
                "humidity_min": 40,
                "humidity_max": 60,
                "standard_source": "feishu:tbl-standard",
            },
            "python_alarm_transition": {
                "violation_started_at": "2026-09-09T09:55:00+00:00",
                "recovery_started_at": (
                    "2026-09-09T09:59:00+00:00" if recovery else None
                ),
            },
        }

    def test_mapper_contract_creates_one_alarm_notification(self) -> None:
        transition = AlarmStateMachine(verify_after=timedelta(0)).apply(
            result=MonitorResult(
                "TH-01", self.now, 31, 70, TemperatureStatus.HIGH,
                TemperatureStatus.HIGH, OverallStatus.VIOLATION, "S", "R",
                ("temperature_high",),
            ),
            current_state=AlarmState(
                "TH-01", AlarmLifecycleState.PENDING,
                violation_started_at=self.now - timedelta(minutes=5),
            ),
            now=self.now,
        )
        from application.actions import ApplicationActionMapper

        actions = ApplicationActionMapper(emit_notifications=True).map(transition)
        self.assertEqual(
            [item.action_type for item in actions].count(AlarmActionType.NOTIFY_ALARM),
            1,
        )

    def test_continuous_alarm_has_no_new_notification(self) -> None:
        state = AlarmState("TH-01", AlarmLifecycleState.ALARM, active_alarm_id="evt")
        transition = AlarmStateMachine().apply(
            result=MonitorResult(
                "TH-01", self.now, 31, 70, TemperatureStatus.HIGH,
                TemperatureStatus.HIGH, OverallStatus.VIOLATION, "S", "R", (),
            ),
            current_state=state,
            now=self.now,
        )
        self.assertEqual(
            sum(a.action_type is AlarmActionType.NOTIFY_ALARM for a in transition.actions),
            0,
        )

    def test_alarm_send_and_replay_are_one_remote_call(self) -> None:
        sender = _Sender()
        writer = self._writer(sender)
        action = self._action(AlarmActionType.NOTIFY_ALARM, f"NOTIFY_ALARM:{self.event.event_id}")
        context = self._context()
        writer.handle_notification_action(action, context)
        writer.handle_notification_action(action, context)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(context["message_id"], "om_test_1")
        marker = self.events.get_external_effect(self.event.event_id, action.dedupe_key)
        self.assertEqual(marker["status"], "SUCCEEDED")
        self.assertEqual(marker["recipient"], "ou_owner_1")

    def test_recovery_send_is_one_remote_call(self) -> None:
        sender = _Sender()
        writer = self._writer(sender)
        key = f"NOTIFY_RECOVERY:{self.event.event_id}:2026-09-09T09:59:00+00:00"
        action = self._action(AlarmActionType.NOTIFY_RECOVERY, key)
        context = self._context(recovery=True)
        writer.handle_notification_action(action, context)
        writer.handle_notification_action(action, context)
        self.assertEqual(len(sender.calls), 1)
        self.assertIn("恢复", sender.calls[0]["text"])

    def test_event_owner_takes_priority_over_device_owner(self) -> None:
        sender = _Sender()
        self._writer(sender).handle_notification_action(
            self._action(AlarmActionType.NOTIFY_ALARM, f"NOTIFY_ALARM:{self.event.event_id}"),
            self._context(),
        )
        self.assertEqual(sender.calls[0]["receive_id"], "ou_owner_1")

    def test_invalid_display_name_falls_back_to_chat(self) -> None:
        self.source.records[self.event_table] = (
            FeishuRawRecord("rec-event-1", {"责任人": "张三"}),
        )
        self.source.records[self.device_table] = (
            FeishuRawRecord("rec-device-1", {"设备编号": "TH-01", "默认异常责任人": "李四"}),
        )
        with patch.object(config, "FEISHU_ALARM_CHAT_ID", "oc_test_chat"):
            sender = _Sender()
            self._writer(sender).handle_notification_action(
                self._action(AlarmActionType.NOTIFY_ALARM, f"NOTIFY_ALARM:{self.event.event_id}"),
                self._context(),
            )
        self.assertEqual(sender.calls[0]["receive_id"], "oc_test_chat")
        self.assertEqual(sender.calls[0]["receive_id_type"], "chat_id")

    def test_recipient_unresolved_is_audited_and_not_sent(self) -> None:
        self.source.records[self.event_table] = (
            FeishuRawRecord("rec-event-1", {"责任人": "张三"}),
        )
        self.source.records[self.device_table] = (
            FeishuRawRecord("rec-device-1", {"设备编号": "TH-01", "默认异常责任人": "李四"}),
        )
        action = self._action(AlarmActionType.NOTIFY_ALARM, f"NOTIFY_ALARM:{self.event.event_id}")
        with patch.object(config, "FEISHU_ALARM_CHAT_ID", ""):
            with self.assertRaisesRegex(FeishuNotificationError, "recipient"):
                self._writer(_Sender()).handle_notification_action(action, self._context())
        marker = self.events.get_external_effect(self.event.event_id, action.dedupe_key)
        self.assertEqual(marker["error_code"], "recipient_unresolved")
        self.assertEqual(marker["status"], "FAILED")

    def test_unbound_event_is_retryable_and_independent(self) -> None:
        unbound = self.events.create_or_get_active(
            device_id="TH-02",
            event_key="ENV:TH-02:2026-09-09T10:00:00+00:00",
            opened_at=self.now,
            payload={},
        )
        action = ApplicationAction(
            action_type=AlarmActionType.NOTIFY_ALARM,
            kind=ApplicationActionKind.NOTIFICATION,
            device_id="TH-02",
            source=AlarmAction(AlarmActionType.NOTIFY_ALARM, "TH-02", alarm_id=unbound.event_id),
            alarm_id=unbound.event_id,
            dedupe_key=f"NOTIFY_ALARM:{unbound.event_id}",
        )
        with self.assertRaises(FeishuNotificationError) as raised:
            self._writer(_Sender()).handle_notification_action(
                action, {"device_id": "TH-02", "event_id": unbound.event_id}
            )
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(self.source.records[self.event_table]), 1)

    def test_timeout_retry_keeps_task_identity(self) -> None:
        action_key = f"NOTIFY_ALARM:{self.event.event_id}"
        task = self.tasks.create_or_get(
            task_type="NOTIFY_ALARM",
            entity_type="EVENT",
            entity_id=self.event.event_id,
            due_at=self.now,
            payload={**self._context(), "event_id": self.event.event_id},
            dedupe_key=action_key,
            created_at=self.now,
        )
        sender = _Sender(
            [
                FeishuIMError(
                    "timeout", error_code="network_error", retryable=True,
                    outcome_unknown=True,
                )
            ]
        )
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-01",),
            context_handlers={AlarmActionType.NOTIFY_ALARM: self._writer(sender).handle_notification_action},
            standards_ready_provider=lambda: True,
        )
        from application.monitor_service import MonitorApplicationService

        service = object.__new__(MonitorApplicationService)
        service.action_executor = executor
        service.task_repository = self.tasks
        service.now_provider = lambda: self.now
        with self.assertRaises(RuntimeError):
            claimed = self.tasks.claim_due(
                now=self.now, limit=1, worker_id="test-worker", lease_for=timedelta(minutes=5)
            )[0]
            service.execute_notification_task(task=claimed, now=self.now)
        pending = self.tasks.get(task.task_id)
        self.assertEqual(pending.task_id, task.task_id)
        self.assertEqual(pending.status.value, "PENDING")
        retry_at = datetime.fromisoformat(pending.due_at.isoformat())
        scheduler = TaskScheduler(
            repository=self.tasks,
            handlers={"NOTIFY_ALARM": lambda current: service.execute_notification_task(task=current, now=retry_at)},
            worker_id="test-worker",
            now_provider=lambda: retry_at,
        )
        scheduler.run_once(now=retry_at, limit=1)
        self.assertEqual(self.tasks.get(task.task_id).status.value, "SUCCEEDED")
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[0]["idempotency_key"], sender.calls[1]["idempotency_key"])

    def test_forbidden_is_not_rescheduled(self) -> None:
        action_key = f"NOTIFY_ALARM:{self.event.event_id}"
        task = self.tasks.create_or_get(
            task_type="NOTIFY_ALARM", entity_type="EVENT", entity_id=self.event.event_id,
            due_at=self.now, payload=self._context(), dedupe_key=action_key, created_at=self.now,
        )
        sender = _Sender([FeishuIMError("403", error_code="permission_denied", retryable=False)])
        executor = ActionExecutor(
            mode="active", active_device_ids=("TH-01",),
            context_handlers={AlarmActionType.NOTIFY_ALARM: self._writer(sender).handle_notification_action},
            standards_ready_provider=lambda: True,
        )
        from application.monitor_service import MonitorApplicationService

        service = object.__new__(MonitorApplicationService)
        service.action_executor = executor
        service.task_repository = self.tasks
        service.now_provider = lambda: self.now
        scheduler = TaskScheduler(
            repository=self.tasks,
            handlers={"NOTIFY_ALARM": lambda current: service.execute_notification_task(task=current, now=self.now)},
            worker_id="test-worker",
        )
        scheduler.run_once(now=self.now, limit=1)
        self.assertEqual(self.tasks.get(task.task_id).status.value, "FAILED")
        self.assertEqual(len(sender.calls), 1)

    def test_shadow_gate_does_not_call_sender(self) -> None:
        sender = _Sender()
        executor = ActionExecutor(
            mode="shadow",
            active_device_ids=("TH-01",),
            context_handlers={AlarmActionType.NOTIFY_ALARM: self._writer(sender).handle_notification_action},
            standards_ready_provider=lambda: True,
        )
        execution = executor.execute(
            (self._action(AlarmActionType.NOTIFY_ALARM, f"NOTIFY_ALARM:{self.event.event_id}"),),
            context=self._context(),
            created_at=self.now,
        )[0]
        self.assertEqual(execution.status, ActionExecutionStatus.PLANNED)
        self.assertEqual(sender.calls, [])

    def test_active_gate_without_standards_does_not_call_sender(self) -> None:
        sender = _Sender()
        executor = ActionExecutor(
            mode="active", active_device_ids=("TH-01",),
            context_handlers={AlarmActionType.NOTIFY_ALARM: self._writer(sender).handle_notification_action},
            standards_ready_provider=lambda: False,
        )
        execution = executor.execute(
            (self._action(AlarmActionType.NOTIFY_ALARM, f"NOTIFY_ALARM:{self.event.event_id}"),),
            context=self._context(), created_at=self.now,
        )[0]
        self.assertEqual(execution.status, ActionExecutionStatus.PLANNED)
        self.assertEqual(sender.calls, [])

    def test_audit_has_structured_notification_columns(self) -> None:
        sender = _Sender()
        recorder = SQLiteAutomationRunRepository(self.connection)
        executor = ActionExecutor(
            mode="active", active_device_ids=("TH-01",),
            context_handlers={AlarmActionType.NOTIFY_ALARM: self._writer(sender).handle_notification_action},
            standards_ready_provider=lambda: True, recorder=recorder,
        )
        key = f"NOTIFY_ALARM:{self.event.event_id}"
        executor.execute((self._action(AlarmActionType.NOTIFY_ALARM, key),), context=self._context(), created_at=self.now)
        row = self.connection.execute(
            "SELECT automation_task_id, action_type, event_id, recipient, message_id, dedupe_key, "
            "standard_id, standard_revision, standard_source, result, sent_at FROM automation_runs "
            "WHERE action_type = 'NOTIFY_ALARM'"
        ).fetchone()
        self.assertEqual(row[1], "NOTIFY_ALARM")
        self.assertEqual(row[2], self.event.event_id)
        self.assertEqual(row[3], "ou_owner_1")
        self.assertEqual(row[4], "om_test_1")
        self.assertEqual(row[5], key)
        self.assertEqual(tuple(row[6:9]), ("STD-1", "R2", "feishu:tbl-standard"))
        self.assertEqual(row[9], "SUCCEEDED")
        self.assertTrue(row[10])

    def test_active_pipeline_creates_alarm_and_recovery_notifications_once(self) -> None:
        class AlarmRepository:
            def __init__(self) -> None:
                self.states = {}

            def get(self, device_id):
                return self.states.get(device_id)

            def save(self, state):
                self.states[state.device_id] = state

        class OperationProvider:
            def get(self, device):
                return OperationState(
                    area_id=device.area,
                    state=OperationStatus.OPERATING,
                    operation_type=None,
                    work_order=None,
                    started_at=None,
                    ended_at=None,
                )

        standard = EnvironmentStandard(
            standard_id="STD-1", revision="R2", area="仓库", device_id=None,
            operation_type=None, temperature_min=20.0, temperature_max=26.0,
            humidity_min=40.0, humidity_max=60.0,
            effective_from=self.now - timedelta(days=1), effective_to=None,
            source_document="SOP", clause="5.2", standard_source="feishu:test",
            enabled=True,
            control_type=ControlType.ALL_DAY,
        )
        connection = sqlite3.connect(":memory:")
        events = SQLiteEnvironmentEventRepository(connection)
        tasks = SQLiteAutomationTaskRepository(connection)
        sender = _Sender()
        source = _Source(
            {
                self.event_table: (
                    FeishuRawRecord("rec-event-active", {"责任人": [{"open_id": "ou_owner_1"}]}),
                ),
                self.device_table: (
                    FeishuRawRecord(
                        "rec-device-1",
                        {"设备编号": "TH-01", "默认异常责任人": "ou_device_1"},
                    ),
                ),
            }
        )
        notification_writer = FeishuNotificationWriter(
            sender=sender,
            source=source,
            event_table_id=self.event_table,
            device_table_id=self.device_table,
            event_repository=events,
        )

        def event_handler(action, context):
            action_type = action.action_type.value
            if action_type == "CREATE_ALARM_EVENT":
                events.bind_external_record(
                    context["event_id"], record_id="rec-event-active"
                )

        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-01",),
            handlers={
                AlarmActionType.CREATE_VERIFY_TASK: lambda action: None,
                AlarmActionType.CANCEL_VERIFY_TASK: lambda action: None,
                AlarmActionType.COMPLETE_VERIFY_TASK: lambda action: None,
            },
            context_handlers={
                AlarmActionType.CREATE_ALARM_EVENT: event_handler,
                AlarmActionType.UPDATE_ALARM_EVENT: event_handler,
                AlarmActionType.MARK_ALARM_RECOVERED: event_handler,
                AlarmActionType.NOTIFY_ALARM: notification_writer.handle_notification_action,
                AlarmActionType.NOTIFY_RECOVERY: notification_writer.handle_notification_action,
            },
            standards_ready_provider=lambda: True,
        )
        service = MonitorApplicationService(
            operation_state_provider=OperationProvider(),
            standard_resolver=StaticStandardResolver((standard,)),
            alarm_state_repository=AlarmRepository(),
            alarm_state_machine=AlarmStateMachine(
                verify_after=timedelta(0), recovery_after=timedelta(0)
            ),
            action_mapper=ApplicationActionMapper(emit_notifications=True),
            action_executor=executor,
            task_repository=tasks,
            event_repository=events,
        )
        device = DeviceContext("TH-01", "仓库", control_type=ControlType.ALL_DAY)
        with patch.object(config, "FEISHU_ALARM_NOTIFY_ENABLED", True), patch.object(
            config, "FEISHU_RECOVERY_NOTIFY_ENABLED", True
        ):
            service.handle_sample(
                device=device,
                sample=MonitorSample("TH-01", self.now, 31.0, 70.0),
                now=self.now,
            )
            service.handle_sample(
                device=device,
                sample=MonitorSample("TH-01", self.now + timedelta(minutes=5), 31.0, 70.0),
                now=self.now + timedelta(minutes=5),
            )
            service.handle_sample(
                device=device,
                sample=MonitorSample("TH-01", self.now + timedelta(minutes=6), 31.0, 70.0),
                now=self.now + timedelta(minutes=6),
            )
            service.handle_sample(
                device=device,
                sample=MonitorSample("TH-01", self.now + timedelta(minutes=7), 22.0, 50.0),
                now=self.now + timedelta(minutes=7),
            )
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(
            [call["receive_id"] for call in sender.calls],
            ["ou_owner_1", "ou_owner_1"],
        )
        self.assertIn("峰值温度/湿度：31.0 / 70.0", sender.calls[1]["text"])
        task_types = [row[0] for row in connection.execute(
            "SELECT task_type FROM automation_tasks WHERE task_type LIKE 'NOTIFY_%'"
        ).fetchall()]
        self.assertEqual(task_types, ["NOTIFY_ALARM", "NOTIFY_RECOVERY"])
        connection.close()


class FeishuIMClientTests(unittest.TestCase):
    def test_official_text_payload_uses_stable_uuid(self) -> None:
        response = SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": {"message_id": "om-1"}})
        with patch("services.feishu.get_token", return_value="tenant-token") as get_token, patch(
            "services.feishu.request_with_retry", return_value=response
        ) as request:
            result = FeishuIMClient().send_text(
                "ou_test", "open_id", "hello", idempotency_key="NOTIFY_ALARM:event-1", max_attempts=1
            )
        self.assertEqual(result["data"]["message_id"], "om-1")
        self.assertEqual(get_token.call_args.kwargs["attempts"], 1)
        url = request.call_args.args[1]
        self.assertIn("/open-apis/im/v1/messages", url)
        body = request.call_args.kwargs["json_data"]
        self.assertEqual(body["receive_id"], "ou_test")
        self.assertEqual(json.loads(body["content"]), {"text": "hello"})
        self.assertNotEqual(body["uuid"], "NOTIFY_ALARM:event-1")

    def test_429_is_retryable_but_403_is_not(self) -> None:
        for status, retryable in ((429, True), (403, False)):
            response = SimpleNamespace(status_code=status, json=lambda: {"code": 1, "msg": "x"})
            with patch("services.feishu.get_token", return_value="token"), patch(
                "services.feishu.request_with_retry", return_value=response
            ):
                with self.assertRaises(FeishuIMError) as raised:
                    FeishuIMClient().send_text("ou_test", "open_id", "hello", max_attempts=1)
            self.assertEqual(raised.exception.retryable, retryable)


if __name__ == "__main__":
    unittest.main()
