from __future__ import annotations

import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import config

from application.action_executor import ActionExecutor
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from application.operation_sync import OperationObservationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmActionType,
    ControlType,
    DataQualityStatus,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
    OperationStatus,
)
from domain.operation import OperationAction
from domain.standard_resolver import StaticStandardResolver
from integrations.feishu_operation import (
    FeishuOperationAdapter,
    FeishuOperationFieldMap,
)
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import (
    FeishuEnvironmentEventWriter,
    FeishuInspectionRecordWriter,
    FeishuOperationRecordWriter,
)
from repositories import (
    SQLiteAlarmStateRepository,
    SQLiteAutomationTaskRepository,
    SQLiteEnvironmentEventRepository,
    SQLiteLatestSampleRepository,
    SQLiteOperationRepository,
    connect,
)


class _FeishuStore:
    """A deterministic in-memory Feishu transport for the E2E test."""

    def __init__(self, created_at: datetime) -> None:
        self.clock = created_at
        self.sequence = 0
        self.fail_event_creates = 0
        self.create_attempt_tokens: list[tuple[str, str | None]] = []
        self.tables: dict[str, list[FeishuRawRecord]] = {
            "tbl-device": [
                FeishuRawRecord(
                    record_id="rec-device",
                    fields={
                        "设备编号": "TH-03",
                        "默认异常责任人": [{"id": "ou-owner"}],
                        "要求来源": "执行适用标准",
                    },
                )
            ]
        }
        self.tokens: dict[str, str] = {}

    def read_records(self, table_id: str) -> tuple[FeishuRawRecord, ...]:
        return tuple(self.tables.get(table_id, ()))

    def create(
        self,
        table_id: str,
        fields: dict[str, Any],
        *,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        self.create_attempt_tokens.append((table_id, client_token))
        if client_token and client_token in self.tokens:
            return {"code": 0, "record_id": self.tokens[client_token], "deduped": True}
        if table_id == "tbl-event" and self.fail_event_creates:
            self.fail_event_creates -= 1
            # Simulate a transport failure before the remote record is
            # persisted.  The next attempt must recover through CREATE, not
            # keep issuing UPDATE against a non-existent record.
            raise TimeoutError("event create transport failure")
        self.sequence += 1
        record_id = f"rec-{self.sequence}"
        stored_fields = dict(fields)
        if table_id == "tbl-operation":
            # Simulate the formula calculated by the real ledger workflow.
            stored_fields["登记组合校验"] = "有效"
        self.tables.setdefault(table_id, []).append(
            FeishuRawRecord(
                record_id=record_id,
                fields=stored_fields,
                created_at=self.clock,
                updated_at=self.clock,
            )
        )
        self.clock += timedelta(seconds=1)
        if client_token:
            self.tokens[client_token] = record_id
        return {"code": 0, "record_id": record_id, "fields": stored_fields}

    def update(self, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        records = self.tables.get(table_id, [])
        for index, record in enumerate(records):
            if record.record_id == record_id:
                merged = dict(record.fields)
                merged.update(fields)
                records[index] = FeishuRawRecord(
                    record_id=record.record_id,
                    fields=merged,
                    created_at=record.created_at,
                    updated_at=self.clock,
                )
                self.clock += timedelta(seconds=1)
                return {"code": 0, "record_id": record_id, "fields": fields}
        raise AssertionError(f"unknown fake Feishu record: {table_id}/{record_id}")


class _OperationProvider:
    def __init__(self, repository: SQLiteOperationRepository) -> None:
        self.repository = repository

    def get(self, device: DeviceContext):
        return self.repository.get(device)


class FeishuWriteEndToEndTests(unittest.TestCase):
    def test_operation_registration_drives_alarm_and_inspection_write_chain(self) -> None:
        base_time = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        store = _FeishuStore(base_time)
        operation_writer = FeishuOperationRecordWriter(
            writer=store,
            operation_table_id="tbl-operation",
            interval_table_id="tbl-interval",
            device_table_id="tbl-device",
        )

        operation_writer.create_registration(
            device_id="TH-03",
            area="精密装配间",
            action=OperationAction.START,
            operation_type="未关联工艺文件（TH-03）",
            status_recorded_at=base_time,
            idempotency_key="op-e2e-1",
        )
        operation_adapter = FeishuOperationAdapter(
            source=store,
            table_id="tbl-operation",
            fields=FeishuOperationFieldMap(
                device_id="监测点",
                area_id="区域",
                action="状态变更",
                operation_type="当前工艺",
                validation="登记组合校验",
                valid_values=("有效",),
                allowed_device_ids=frozenset({"TH-03"}),
            ),
        )

        connection = connect(":memory:")
        self.addCleanup(connection.close)
        operation_repository = SQLiteOperationRepository(connection)
        observations = operation_adapter.fetch_observations(observed_at=base_time)
        self.assertEqual(len(observations), 1)
        self.assertTrue(
            OperationObservationService(store=operation_repository)
            .apply(observations[0])
            .accepted
        )

        device = DeviceContext(
            device_id="TH-03",
            area="精密装配间",
            control_type=ControlType.OPERATION_PERIOD,
        )
        self.assertEqual(operation_repository.get(device).state, OperationStatus.OPERATING)

        event_writer = FeishuEnvironmentEventWriter(
            writer=store,
            source=store,
            event_table_id="tbl-event",
            device_table_id="tbl-device",
        )
        alarm_repository = SQLiteAlarmStateRepository(connection)
        task_repository = SQLiteAutomationTaskRepository(connection)
        local_event_repository = SQLiteEnvironmentEventRepository(connection)
        latest_sample_repository = SQLiteLatestSampleRepository(connection)
        standard = EnvironmentStandard(
            standard_id="STD-E2E",
            revision="R1",
            area="精密装配间",
            operation_type="未关联工艺文件（TH-03）",
            temperature_min=20.0,
            temperature_max=30.0,
            humidity_min=30.0,
            humidity_max=60.0,
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            effective_to=None,
            source_document="E2E",
            clause="test",
        )
        action_executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-03",),
            handlers={
                AlarmActionType.CREATE_VERIFY_TASK: lambda action: None,
                AlarmActionType.COMPLETE_VERIFY_TASK: lambda action: None,
                AlarmActionType.CANCEL_VERIFY_TASK: lambda action: None,
            },
            context_handlers={
                AlarmActionType.CREATE_ALARM_EVENT: event_writer.handle_alarm_action,
                AlarmActionType.UPDATE_ALARM_EVENT: event_writer.handle_alarm_action,
                AlarmActionType.START_RECOVERY: event_writer.handle_alarm_action,
                AlarmActionType.CLOSE_ALARM_EVENT: event_writer.handle_alarm_action,
            },
        )
        service = MonitorApplicationService(
            operation_state_provider=_OperationProvider(operation_repository),
            standard_resolver=StaticStandardResolver((standard,)),
            alarm_state_repository=alarm_repository,
            alarm_state_machine=AlarmStateMachine(),
            action_mapper=ApplicationActionMapper(),
            action_executor=action_executor,
            task_repository=task_repository,
            event_repository=local_event_repository,
            latest_sample_repository=latest_sample_repository,
        )

        def sample(at: datetime, temperature: float, humidity: float = 50.0):
            return service.handle_sample(
                device=device,
                sample=MonitorSample(
                    device_id="TH-03",
                    sample_time=at,
                    temperature=temperature,
                    humidity=humidity,
                    online_status="在线",
                    data_quality=DataQualityStatus.GOOD,
                ),
                now=at,
            )

        self.assertEqual(sample(base_time, 35).transition.next.state.value, "PENDING")
        verified = sample(base_time + timedelta(minutes=5), 36)
        self.assertEqual(verified.transition.next.state.value, "ALARM")
        event_records = store.read_records("tbl-event")
        self.assertEqual(len(event_records), 1)
        self.assertEqual(event_records[0].fields["责任人"], [{"id": "ou-owner"}])
        self.assertEqual(event_records[0].fields["异常类型"], "温度高于上限")

        sample(base_time + timedelta(minutes=6), 37)
        self.assertEqual(store.read_records("tbl-event")[0].fields["峰值温度(°C)"], 37.0)
        sample(base_time + timedelta(minutes=7), 25)
        # 写回时间按业务时区（HISTORY_TIMEZONE）输出，不能用系统本地时区：
        # 容器/CI 的本地时区是 UTC，断言会整体偏移 8 小时。
        self.assertEqual(
            store.read_records("tbl-event")[0].fields["恢复时间"],
            (base_time + timedelta(minutes=7))
            .astimezone(ZoneInfo(config.HISTORY_TIMEZONE))
            .strftime("%Y-%m-%d %H:%M:%S"),
        )
        sample(base_time + timedelta(minutes=8), 25)
        self.assertEqual(local_event_repository.list_active(device_id="TH-03"), ())
        self.assertNotIn("关闭时间", store.read_records("tbl-event")[0].fields)

        inspection_writer = FeishuInspectionRecordWriter(
            writer=store,
            inspection_table_id="tbl-inspection",
            device_table_id="tbl-device",
        )
        inspection_writer.create_snapshot(
            area="设备区",
            inspected_at=base_time + timedelta(minutes=9),
            temperature=25,
            humidity=50,
            online_status="在线",
            environment_status="正常",
            temperature_status="正常",
            humidity_status="正常",
            alarm_status="未触发",
            monitoring_system_status="在线正常",
            site_storage_status="正常，无明显异常",
            idempotency_key="inspection-e2e-1",
        )
        inspection = store.read_records("tbl-inspection")[0]
        self.assertEqual(inspection.fields["仓库区域"], "设备区")
        self.assertNotIn("点检时间", inspection.fields)
        self.assertNotIn("点检人", inspection.fields)

    def test_failed_event_create_recovers_after_restart_and_retry_claim(self) -> None:
        base_time = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        store = _FeishuStore(base_time)
        operation_writer = FeishuOperationRecordWriter(
            writer=store,
            operation_table_id="tbl-operation",
            interval_table_id="tbl-interval",
            device_table_id="tbl-device",
        )
        operation_writer.create_registration(
            device_id="TH-03",
            area="精密装配间",
            action=OperationAction.START,
            operation_type="未关联工艺文件（TH-03）",
            status_recorded_at=base_time,
            idempotency_key="op-failure-e2e",
        )
        operation_repository = SQLiteOperationRepository(sqlite3.connect(":memory:"))
        # Use a single connection for all Python-owned state, as the runtime
        # does; the operation source remains the in-memory Feishu store.
        operation_connection = operation_repository.connection
        self.addCleanup(operation_connection.close)
        operation_adapter = FeishuOperationAdapter(
            source=store,
            table_id="tbl-operation",
            fields=FeishuOperationFieldMap(
                device_id="监测点",
                area_id="区域",
                action="状态变更",
                operation_type="当前工艺",
                validation="登记组合校验",
                valid_values=("有效",),
                allowed_device_ids=frozenset({"TH-03"}),
            ),
        )
        OperationObservationService(store=operation_repository).apply(
            operation_adapter.fetch_observations(observed_at=base_time)[0]
        )
        device = DeviceContext(
            device_id="TH-03",
            area="精密装配间",
            control_type=ControlType.OPERATION_PERIOD,
        )
        event_writer = FeishuEnvironmentEventWriter(
            writer=store,
            source=store,
            event_table_id="tbl-event",
            device_table_id="tbl-device",
        )
        standard = EnvironmentStandard(
            standard_id="STD-FAIL-E2E",
            revision="R1",
            area="精密装配间",
            operation_type="未关联工艺文件（TH-03）",
            temperature_min=20.0,
            temperature_max=30.0,
            humidity_min=30.0,
            humidity_max=60.0,
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            effective_to=None,
            source_document="E2E",
            clause="test",
        )

        def build_service() -> MonitorApplicationService:
            return MonitorApplicationService(
                operation_state_provider=_OperationProvider(operation_repository),
                standard_resolver=StaticStandardResolver((standard,)),
                alarm_state_repository=SQLiteAlarmStateRepository(operation_connection),
                alarm_state_machine=AlarmStateMachine(),
                action_mapper=ApplicationActionMapper(),
                action_executor=ActionExecutor(
                    mode="active",
                    active_device_ids=("TH-03",),
                    handlers={
                        AlarmActionType.CREATE_VERIFY_TASK: lambda action: None,
                        AlarmActionType.COMPLETE_VERIFY_TASK: lambda action: None,
                        AlarmActionType.CANCEL_VERIFY_TASK: lambda action: None,
                    },
                    context_handlers={
                        AlarmActionType.CREATE_ALARM_EVENT: event_writer.handle_alarm_action,
                        AlarmActionType.UPDATE_ALARM_EVENT: event_writer.handle_alarm_action,
                        AlarmActionType.START_RECOVERY: event_writer.handle_alarm_action,
                        AlarmActionType.CLOSE_ALARM_EVENT: event_writer.handle_alarm_action,
                    },
                ),
                task_repository=SQLiteAutomationTaskRepository(operation_connection),
                event_repository=SQLiteEnvironmentEventRepository(operation_connection),
                latest_sample_repository=SQLiteLatestSampleRepository(operation_connection),
            )

        def sample(service: MonitorApplicationService, at: datetime, temperature: float):
            return service.handle_sample(
                device=device,
                sample=MonitorSample(
                    device_id="TH-03",
                    sample_time=at,
                    temperature=temperature,
                    humidity=50.0,
                    online_status="在线",
                    data_quality=DataQualityStatus.GOOD,
                ),
                now=at,
            )

        service = build_service()
        self.assertEqual(sample(service, base_time, 35).transition.next.state.value, "PENDING")
        store.fail_event_creates = 2
        failed = sample(service, base_time + timedelta(minutes=5), 36)
        self.assertEqual(failed.transition.next.state.value, "ALARM")
        self.assertEqual(len(store.read_records("tbl-event")), 0)
        self.assertTrue(
            any(
                row[0] == "RECONCILE_ALARM_EVENT"
                and row[1] == "PENDING"
                for row in operation_connection.execute(
                    "SELECT task_type, status FROM automation_tasks"
                )
            )
        )

        # Simulate process restart: state and the recovery task are reloaded
        # from the same SQLite database, then a worker claims the persisted
        # task without a new sensor sample.
        restarted = build_service()
        reconciliation = next(
            task
            for task in restarted.task_repository.claim_due(
                now=base_time + timedelta(minutes=5),
                worker_id="restarted-worker",
            )
            if task.task_type == "RECONCILE_ALARM_EVENT"
        )
        first_retry_at = base_time + timedelta(minutes=5, seconds=1)
        with self.assertRaisesRegex(RuntimeError, "event create transport failure"):
            restarted.reconcile_alarm_event_task(
                task=reconciliation,
                device=device,
                now=first_retry_at,
            )
        restarted.task_repository.mark_failed(
            reconciliation.task_id,
            finished_at=first_retry_at,
            error="event create transport failure",
            worker_id="restarted-worker",
        )
        retry = next(
            task
            for task in restarted.task_repository.claim_due(
                now=first_retry_at
                + timedelta(seconds=config.ACTIVE_EVENT_RECONCILIATION_BACKOFF_SECONDS),
                worker_id="restarted-worker",
            )
            if task.task_type == "RECONCILE_ALARM_EVENT"
        )
        restarted.reconcile_alarm_event_task(
            task=retry,
            device=device,
            now=first_retry_at
            + timedelta(seconds=config.ACTIVE_EVENT_RECONCILIATION_BACKOFF_SECONDS),
        )
        restarted.task_repository.mark_succeeded(
            retry.task_id,
            finished_at=first_retry_at
            + timedelta(seconds=config.ACTIVE_EVENT_RECONCILIATION_BACKOFF_SECONDS),
            worker_id="restarted-worker",
        )
        self.assertEqual(len(store.read_records("tbl-event")), 1)
        event_tokens = tuple(
            token
            for table_id, token in store.create_attempt_tokens
            if table_id == "tbl-event"
        )
        self.assertEqual(len(event_tokens), 3)
        self.assertEqual(len(set(event_tokens)), 1)
        self.assertEqual(str(uuid.UUID(event_tokens[0] or "")), event_tokens[0])
        self.assertEqual(uuid.UUID(event_tokens[0] or "").version, 4)

        # The next ALARM sample is a normal UPDATE of the recovered event;
        # repeated retries remain idempotent and never create a second row.
        sample(restarted, base_time + timedelta(minutes=6), 37)
        sample(restarted, base_time + timedelta(minutes=7), 38)
        self.assertEqual(len(store.read_records("tbl-event")), 1)
        self.assertEqual(
            store.read_records("tbl-event")[0].fields["峰值温度(°C)"], 38.0
        )


if __name__ == "__main__":
    unittest.main()
