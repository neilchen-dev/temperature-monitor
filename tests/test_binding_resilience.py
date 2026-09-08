"""Fault injection for durable cycle projection; no production I/O."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sqlite3

import pytest

from application.action_executor import ActionExecutor
from application.action_executor import ActionExecution, ActionExecutionStatus, AutomationMode
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.standard_resolver import StaticStandardResolver
from domain.models import EnvironmentStandard, MonitorSample, OperationState, OperationStatus
from domain.models import AlarmAction, AlarmActionType, DeviceContext
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import FeishuEnvironmentEventWriter, FeishuWriteError
from repositories import SQLiteAlarmStateRepository, SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.automation_runs import SQLiteAutomationRunRepository
from scheduler.worker import TaskScheduler
from runtime.shadow_runner import ShadowRuntime
from services.event_identity import epoch_milliseconds


NOW = datetime(2026, 9, 2, 13, 41, 47, 162671, tzinfo=timezone.utc)


class Remote:
    def __init__(self):
        self.records = []
        self.visible = True
        self.posts = 0
        self.updates = []
        self.error = None
        self.update_error = False

    def read_records(self, table_id):
        if table_id == "devices":
            return (FeishuRawRecord("device", {"设备编号": "TH-01", "默认异常责任人": [{"id": "ou-owner"}]}),)
        return tuple(self.records) if self.visible else ()

    def create(self, table_id, fields, *, client_token=None):
        self.posts += 1
        record = FeishuRawRecord(f"rec{self.posts}", dict(fields))
        self.records.append(record)
        if self.error:
            raise self.error
        return {"code": 0, "data": {"record": {"record_id": record.record_id}}}

    def update(self, table_id, record_id, fields):
        record = next((r for r in self.records if r.record_id == record_id), None)
        if record is None:
            raise RuntimeError("remote record deleted")
        record.fields.update(fields)
        self.updates.append((record_id, dict(fields)))
        if self.update_error:
            self.update_error = False
            raise TimeoutError("UPDATE response lost")
        return {"code": 0}


def setup(connection=None):
    connection = connection or sqlite3.connect(":memory:", check_same_thread=False)
    repo = SQLiteEnvironmentEventRepository(connection)
    event = repo.create_or_get_active(device_id="TH-01", event_key=f"ENV:TH-01:{NOW.isoformat()}",
                                      opened_at=NOW, payload={"feishu_binding_status": "PENDING", "feishu_create_attempted": False})
    remote = Remote()
    writer = FeishuEnvironmentEventWriter(writer=remote, source=remote, event_table_id="events",
                                          device_table_id="devices", event_repository=repo)
    context = {"device_id": "TH-01", "created_at": NOW.isoformat(), "sample_time": NOW.isoformat(),
               "sample": {"temperature": 35}, "operation_state": {"area_id": "仓库"},
               "python_alarm_transition": {"violation_started_at": NOW.isoformat(), "active_alarm_id": event.event_id}}
    return connection, repo, event, remote, writer, context


def action(event, kind=AlarmActionType.CREATE_ALARM_EVENT):
    return AlarmAction(action_type=kind, device_id="TH-01", alarm_id=event.event_id)


def service(connection, repo, writer):
    return MonitorApplicationService(
        operation_state_provider=SimpleNamespace(), standard_resolver=SimpleNamespace(),
        alarm_state_machine=SimpleNamespace(), action_mapper=ApplicationActionMapper(),
        alarm_state_repository=SQLiteAlarmStateRepository(connection), event_repository=repo,
        task_repository=SQLiteAutomationTaskRepository(connection),
        action_executor=ActionExecutor(mode="active", active_device_ids=("TH-01",),
            standards_ready_provider=lambda: True,
            context_handlers={kind: writer.handle_alarm_action for kind in (
                AlarmActionType.CREATE_ALARM_EVENT, AlarmActionType.UPDATE_ALARM_EVENT,
                AlarmActionType.MARK_ALARM_RECOVERED)}))


@pytest.mark.parametrize("error", [TimeoutError("response lost"), RuntimeError("1254608")])
def test_uncertain_post_delayed_visibility_closed_recovery_and_same_task(error):
    connection, repo, event, remote, writer, context = setup()
    try:
        remote.visible = False
        remote.error = error
        with pytest.raises(type(error)):
            writer.handle_alarm_action(action(event), context)
        assert remote.posts == 1
        recovered_at = NOW + timedelta(minutes=8)
        repo.patch_external_projection(event.event_id, feishu_recovery_pending=True,
                                       feishu_recovered_at=recovered_at.isoformat())
        repo.mark_recovered(event.event_id, recovered_at=recovered_at)
        app = service(connection, repo, writer)
        task = app.task_repository.create_or_get_unfinished(
            task_type="RECONCILE_ALARM_EVENT", entity_type="DEVICE", entity_id="TH-01", due_at=recovered_at,
            payload={"local_event_id": event.event_id, "violation_started_at": NOW.isoformat(), "temperature": 35},
            dedupe_key=f"RECONCILE_ALARM_EVENT:{event.event_key}", created_at=NOW)
        device = DeviceContext("TH-01", "仓库")
        clock = recovered_at
        scheduler = TaskScheduler(repository=app.task_repository, handlers={
            "RECONCILE_ALARM_EVENT": lambda t: app.reconcile_alarm_event_task(task=t, device=device, now=clock)})
        delays = []
        for _ in range(3):
            assert scheduler.run_once(now=clock).failed == 1
            retry = app.task_repository.get(task.task_id)
            assert retry.status.value == "PENDING"
            assert retry.dedupe_key == task.dedupe_key
            delays.append((retry.due_at - clock).total_seconds())
            # Fresh samples/rearms cannot reset the durable backoff.
            rearmed = app.task_repository.create_or_get_unfinished(
                task_type=task.task_type, entity_type="DEVICE", entity_id="TH-01", due_at=clock,
                payload=task.payload, dedupe_key=task.dedupe_key, created_at=clock)
            assert rearmed.due_at == retry.due_at
            clock = retry.due_at
        assert delays[1] == delays[0] * 2
        assert delays[2] == delays[1] * 2
        assert remote.posts == 1
        remote.visible = True
        remote.update_error = True
        assert scheduler.run_once(now=clock).failed == 1
        clock = app.task_repository.get(task.task_id).due_at
        assert scheduler.run_once(now=clock).succeeded == 1
        final = repo.get(event.event_id)
        assert final.status == "CLOSED" and final.closed_at == recovered_at
        assert final.event_key == event.event_key
        assert final.payload["feishu_record_id"] == "rec1"
        assert final.payload["feishu_recovery_pending"] is False
        assert remote.records[0].fields["恢复时间"] == epoch_milliseconds(recovered_at)
        assert "闭环状态" not in remote.records[0].fields
        assert "异常原因" not in remote.records[0].fields
        assert remote.posts == 1
    finally:
        connection.close()


def test_concurrent_ten_paths_and_expired_lease_cannot_send_second_post():
    connection, repo, event, remote, writer, context = setup()
    try:
        remote.visible = False
        remote.error = TimeoutError("lost")
        def invoke(index):
            current = dict(context, created_at=(NOW + timedelta(days=index)).isoformat())
            try:
                writer.handle_alarm_action(action(event), current)
            except (FeishuWriteError, TimeoutError):
                pass
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(invoke, range(10)))
        assert remote.posts == 1
        assert not repo.get(event.event_id).payload.get("feishu_record_id")
        assert repo.get(event.event_id).payload["feishu_create_attempted"] is True
    finally:
        connection.close()


def test_duplicate_cycle_rejected_but_distinct_unclosed_cycles_are_independent():
    connection, repo, event, remote, writer, context = setup()
    try:
        writer.handle_alarm_action(action(event), context)
        repo.mark_recovered(event.event_id, recovered_at=NOW + timedelta(minutes=1))
        later = NOW + timedelta(minutes=10)
        second = repo.create_or_get_active(device_id="TH-01", event_key=f"ENV:TH-01:{later.isoformat()}", opened_at=later)
        other = dict(context, created_at=later.isoformat(), python_alarm_transition={"violation_started_at": later.isoformat()})
        writer.handle_alarm_action(action(second), other)
        assert repo.get(second.event_id).payload["feishu_record_id"] == "rec2"
        assert repo.get(event.event_id).payload["feishu_record_id"] == "rec1"
        remote.records.append(FeishuRawRecord("duplicate", dict(remote.records[1].fields)))
        with pytest.raises(FeishuWriteError, match="EVENT_DUPLICATED"):
            writer._find_event_by_business_key("TH-01", later)
    finally:
        connection.close()


@pytest.mark.parametrize("bound", [True, False])
def test_manual_deletion_never_rebinds_other_cycle_or_recreates(bound):
    connection, repo, event, remote, writer, context = setup()
    try:
        writer.handle_alarm_action(action(event), context)
        if not bound:
            # Model a crash before binding commit: retain POST evidence.
            repo.patch_external_projection(event.event_id, feishu_record_id=None, feishu_binding_status="PENDING")
        remote.records[0] = FeishuRawRecord("other-cycle", {"监测点": "TH-01", "开始时间": epoch_milliseconds(NOW - timedelta(days=1))})
        context["created_at"] = (NOW + timedelta(days=1)).isoformat()
        context["python_alarm_transition"]["reason"] = "alarm_event_reconciliation"
        with pytest.raises((RuntimeError, FeishuWriteError)):
            writer.handle_alarm_action(action(event, AlarmActionType.UPDATE_ALARM_EVENT), context)
        assert remote.posts == 1
        assert repo.get(event.event_id).payload.get("feishu_record_id") != "other-cycle"
    finally:
        connection.close()


def test_wal_restart_after_remote_post_before_bind_commit(tmp_path, monkeypatch):
    path = tmp_path / "isolated.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection, repo, event, remote, writer, context = setup(connection)
    original = repo.bind_external_record
    monkeypatch.setattr(repo, "bind_external_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before bind")))
    with pytest.raises(RuntimeError, match="crash before bind"):
        writer.handle_alarm_action(action(event), context)
    monkeypatch.setattr(repo, "bind_external_record", original)
    connection.close()
    with sqlite3.connect(path) as reopened:
        restored = SQLiteEnvironmentEventRepository(reopened)
        SQLiteEnvironmentEventRepository(reopened)  # migration idempotency
        writer.event_repository = restored
        context["created_at"] = (NOW + timedelta(days=1)).isoformat()
        writer.handle_alarm_action(action(event), context)
        writer.handle_alarm_action(action(event, AlarmActionType.UPDATE_ALARM_EVENT), context)
        assert restored.get(event.event_id).payload["feishu_record_id"] == "rec1"
        assert remote.posts == 1


def test_real_state_machine_recovery_before_binding_is_rearmed_by_runtime():
    connection, repo, unused, remote, writer, context = setup()
    try:
        # Use an unrelated historical cycle to prove it is left untouched.
        repo.patch_external_projection(unused.event_id, feishu_binding_status=None)
        repo.mark_recovered(unused.event_id, recovered_at=NOW)
        app = service(connection, repo, writer)
        app.alarm_state_machine = AlarmStateMachine()
        app.operation_state_provider = SimpleNamespace(get=lambda device: OperationState(
            "仓库", OperationStatus.NOT_APPLICABLE, None, None, None, None))
        app.standard_resolver = StaticStandardResolver((EnvironmentStandard(
            "std", "R1", "仓库", None, 20, 30, 30, 60, NOW - timedelta(days=1), None,
            "Feishu", None, control_type="ALL_DAY"),))
        device = DeviceContext("TH-01", "仓库", control_type="MONITOR_ONLY")
        remote.visible = False
        remote.error = TimeoutError("lost response")
        def sample(minutes, temperature):
            at = NOW + timedelta(minutes=minutes)
            return app.handle_sample(device=device, sample=MonitorSample(
                "TH-01", at, temperature, 50, "online"), now=at)
        assert sample(1, 35).transition.next.state.value == "PENDING"
        alarm = sample(6, 35)
        event_id = alarm.transition.next.active_alarm_id
        assert event_id != unused.event_id
        assert sample(7, 25).transition.next.state.value == "RECOVERY"
        assert sample(8, 25).transition.next.state.value == "NORMAL"
        event = repo.get(event_id)
        assert event.status == "CLOSED" and event.payload["feishu_recovery_pending"]
        runtime = SimpleNamespace(active_canary_enabled=True, active_device_ids=("TH-01",),
                                  devices={"TH-01": device}, event_repository=repo,
                                  task_repository=app.task_repository)
        now = NOW + timedelta(minutes=9)
        remote.visible = True
        ShadowRuntime._ensure_event_reconciliation_tasks(runtime, now=now)
        tasks = app.task_repository.claim_due(now=now, worker_id="restart")
        task = next(t for t in tasks if t.task_type == "RECONCILE_ALARM_EVENT")
        app.reconcile_alarm_event_task(task=task, device=device, now=now)
        final = repo.get(event_id)
        assert final.payload["feishu_record_id"] == "rec1"
        assert final.payload["feishu_recovery_pending"] is False
        assert final.closed_at == event.closed_at
        assert remote.records[0].fields["恢复时间"] == epoch_milliseconds(event.closed_at)
        assert app.alarm_state_repository.get("TH-01").state.value == "NORMAL"
        assert not repo.get(unused.event_id).payload.get("feishu_record_id")
        assert remote.posts == 1
    finally:
        connection.close()


def test_bound_update_response_loss_survives_restart_and_replays_snapshot():
    connection, repo, event, remote, writer, context = setup()
    try:
        writer.handle_alarm_action(action(event), context)
        remote.update_error = True
        with pytest.raises(TimeoutError):
            writer.handle_alarm_action(action(event, AlarmActionType.UPDATE_ALARM_EVENT), context)
        assert repo.get(event.event_id).payload["feishu_update_pending"]
        app = service(connection, repo, writer)
        device = DeviceContext("TH-01", "仓库")
        runtime = SimpleNamespace(active_canary_enabled=True, active_device_ids=("TH-01",),
                                  devices={"TH-01": device}, event_repository=repo,
                                  task_repository=app.task_repository)
        ShadowRuntime._ensure_event_reconciliation_tasks(runtime, now=NOW)
        task = app.task_repository.claim_due(now=NOW, worker_id="restart")[0]
        app.reconcile_alarm_event_task(task=task, device=device, now=NOW)
        assert remote.updates[-1] == remote.updates[-2]
        assert not repo.get(event.event_id).payload["feishu_update_pending"]
        assert remote.posts == 1
    finally:
        connection.close()


def test_concurrent_old_schema_migration_and_post_reservation(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""CREATE TABLE environment_events (
            event_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, event_key TEXT UNIQUE,
            status TEXT, opened_at TEXT, closed_at TEXT, payload_json TEXT)""")
        connection.execute("INSERT INTO environment_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                           ("legacy", "TH-01", "ENV:legacy", "CLOSED", NOW.isoformat(), NOW.isoformat(), "{}"))
    def migrate_and_reserve(_):
        with sqlite3.connect(path, timeout=5) as connection:
            repository = SQLiteEnvironmentEventRepository(connection)
            assert repository.list_pending_external_bindings() == ()
            return repository.reserve_external_post("legacy")
    with ThreadPoolExecutor(max_workers=10) as pool:
        reservations = list(pool.map(migrate_and_reserve, range(10)))
    assert sum(reservations) == 1
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(environment_events)")}
        assert {"external_create_owner", "external_create_lease_until"} <= columns


@pytest.mark.parametrize("remote_present", [True, False])
def test_historical_event_b_audit_evidence_recovers_without_touching_event_a(remote_present):
    connection = sqlite3.connect(":memory:")
    try:
        repo = SQLiteEnvironmentEventRepository(connection)
        event_b = "f4038bd52a934e6cacf2fb0e7f863ce5"
        event_a = "6ad1d30a1cfb4587aaa753339c429cf6"
        closed_at = "2026-09-02T13:59:08.873779+00:00"
        for event_id, start in ((event_a, NOW - timedelta(days=1)), (event_b, NOW)):
            connection.execute("""INSERT INTO environment_events
                (event_id, device_id, event_key, status, opened_at, closed_at, payload_json)
                VALUES (?, 'TH-01', ?, 'CLOSED', ?, ?, '{}')""",
                (event_id, f"ENV:TH-01:{start.isoformat()}", start.isoformat(), closed_at))
        connection.commit()
        SQLiteAutomationRunRepository(connection).record(ActionExecution(
            action=AlarmAction(action_type=AlarmActionType.CREATE_ALARM_EVENT, device_id="TH-01"),
            mode=AutomationMode.ACTIVE, status=ActionExecutionStatus.SUCCEEDED,
            context={"python_alarm_transition": {"active_alarm_id": event_b}}, created_at=NOW))
        remote = Remote()
        remote.records = [FeishuRawRecord("recA", {"监测点": "TH-01", "开始时间": epoch_milliseconds(NOW - timedelta(days=1))})]
        if remote_present:
            remote.records.append(FeishuRawRecord("recB", {"监测点": "TH-01", "开始时间": epoch_milliseconds(NOW)}))
        writer = FeishuEnvironmentEventWriter(writer=remote, source=remote, event_table_id="events",
                                              device_table_id="devices", event_repository=repo)
        app = service(connection, repo, writer)
        device = DeviceContext("TH-01", "仓库")
        runtime = SimpleNamespace(active_canary_enabled=True, active_device_ids=("TH-01",),
                                  devices={"TH-01": device}, event_repository=repo,
                                  task_repository=app.task_repository)
        clock = NOW + timedelta(days=1)
        scheduler = TaskScheduler(repository=app.task_repository, handlers={
            "RECONCILE_ALARM_EVENT": lambda t: app.reconcile_alarm_event_task(task=t, device=device, now=clock)})
        for _ in range(2):
            ShadowRuntime._ensure_event_reconciliation_tasks(runtime, now=clock)
            scheduler.run_once(now=clock)
            clock += timedelta(minutes=2)
        assert repo.get(event_a).payload == {}
        final = repo.get(event_b)
        assert final.status == "CLOSED" and final.closed_at.isoformat() == closed_at
        assert final.event_key == f"ENV:TH-01:{NOW.isoformat()}"
        assert remote.posts == 0
        if remote_present:
            assert final.payload["feishu_record_id"] == "recB"
            assert not final.payload["feishu_recovery_pending"]
            assert remote.records[1].fields["恢复时间"] == epoch_milliseconds(datetime.fromisoformat(closed_at))
        else:
            assert not final.payload.get("feishu_record_id")
            assert final.payload["feishu_binding_status"] == "PENDING"
    finally:
        connection.close()
