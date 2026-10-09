"""Fault-injection regressions for the repository audit."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sqlite3

import pytest
import requests

import config
from app import create_app
from application.action_executor import ActionExecutor
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    ControlType, DeviceContext, EnvironmentStandard, MonitorSample,
    OperationState, OperationStatus,
)
from domain.standard_resolver import StaticStandardResolver
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.runtime_state import SQLiteAlarmStateRepository, SQLiteLatestSampleRepository
from repositories.sqlite import run_sqlite_write_with_retry, sqlite_unit_of_work
from services import collector, db, devices, feishu, projection
from services.modbus_client import ModbusPoller

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    db.close()
    monkeypatch.setattr(config, "SQLITE_ENABLED", True)
    monkeypatch.setattr(config, "SQLITE_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(db, "_init_failed", False)
    db.init_db()
    yield
    db.close()


def test_failed_presence_write_cannot_be_committed_by_next_operation(mirror):
    with patch.object(db, "_upsert_device_presence_locked", side_effect=sqlite3.OperationalError("injected")):
        assert db.save_device_sample("AUDIT", "home_assistant", 1000, NOW.isoformat(), 24, 50, "online") is False
    assert not db.peek_connection().in_transaction
    db.save_temperature_report("OTHER", 24, 50, "online", 0, "")
    assert db.fetch_device_samples("AUDIT") == []


def test_non_lock_failure_rolls_back_repository_transaction():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE effects (value INTEGER)")
        def write():
            connection.execute("INSERT INTO effects VALUES (1)")
            raise ValueError("injected")
        with pytest.raises(ValueError):
            run_sqlite_write_with_retry(connection, "audit", write)
        assert not connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    finally:
        connection.close()


def test_nested_unit_preserves_outer_transaction():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE effects (value INTEGER)")
        with sqlite_unit_of_work(connection):
            connection.execute("INSERT INTO effects VALUES (1)")
            with pytest.raises(ValueError):
                with sqlite_unit_of_work(connection):
                    connection.execute("INSERT INTO effects VALUES (2)")
                    raise ValueError("injected")
        assert connection.execute("SELECT value FROM effects").fetchall() == [(1,)]
    finally:
        connection.close()


def test_alarm_transition_rolls_back_task_and_latest_sample():
    connection = sqlite3.connect(":memory:")
    try:
        alarm = SQLiteAlarmStateRepository(connection)
        tasks = SQLiteAutomationTaskRepository(connection)
        latest = SQLiteLatestSampleRepository(connection)
        standard = EnvironmentStandard(
            standard_id="AUDIT", revision="1", area="AUDIT", device_id="AUDIT",
            operation_type=None, temperature_min=20, temperature_max=26,
            humidity_min=40, humidity_max=60, effective_from=NOW - timedelta(days=1),
            effective_to=None, source_document="audit", clause="audit", control_type=ControlType.ALL_DAY,
        )
        operation = SimpleNamespace(get=lambda device: OperationState(
            area_id="AUDIT", state=OperationStatus.OPERATING, operation_type=None,
            work_order=None, started_at=None, ended_at=None,
        ))
        service = MonitorApplicationService(
            operation_state_provider=operation, standard_resolver=StaticStandardResolver((standard,)),
            alarm_state_repository=alarm, alarm_state_machine=AlarmStateMachine(),
            action_mapper=ApplicationActionMapper(), action_executor=ActionExecutor(mode="shadow"),
            task_repository=tasks, latest_sample_repository=latest,
        )
        service.handle_sample(device=DeviceContext("AUDIT", "AUDIT"), sample=MonitorSample("AUDIT", NOW, 30, 50), now=NOW)
        pending_id = alarm.get("AUDIT").pending_task_id
        later = NOW + timedelta(seconds=30)
        with patch.object(alarm, "save", side_effect=sqlite3.OperationalError("injected")):
            with pytest.raises(sqlite3.OperationalError):
                service.handle_sample(device=DeviceContext("AUDIT", "AUDIT"), sample=MonitorSample("AUDIT", later, 24, 50), now=later)
        assert alarm.get("AUDIT").state.value == "PENDING"
        assert tasks.get(pending_id).status.value == "PENDING"
        assert latest.get("AUDIT").sample_time == NOW
        assert not connection.in_transaction
        old = MonitorSample("AUDIT", NOW - timedelta(minutes=10), 24, 50)
        assert service.handle_sample(device=DeviceContext("AUDIT", "AUDIT"), sample=old, now=later) is None
        latest.save(old)
        assert latest.get("AUDIT").temperature == 30
    finally:
        connection.close()


def test_required_listener_failure_keeps_dispatch_watermark(mirror, monkeypatch):
    sample = MonitorSample("ACK-AUDIT", NOW, 30, 50)
    ms = int(NOW.timestamp() * 1000)
    db.note_projection_sample(sample.device_id, ms)
    db.mark_projection_projected(sample.device_id, ms)
    listener = Mock(side_effect=RuntimeError("injected"))
    monkeypatch.setattr(devices, "_sample_listeners", [listener])
    monkeypatch.setattr(devices, "_sample_listener_required", {})
    assert projection.dispatch_projected_sample(sample.device_id, sample) is False
    assert not db.fetch_projection_state(sample.device_id)["last_dispatched_sample_time_ms"]
    listener.side_effect = None
    listener.return_value = None
    assert projection.dispatch_projected_sample(sample.device_id, sample) is True
    assert db.fetch_projection_state(sample.device_id)["last_dispatched_sample_time_ms"] == ms


def test_heartbeat_outbox_survives_restart_and_consumer_failure(mirror, monkeypatch):
    db.save_device_heartbeat(device="AUDIT", source="home_assistant", heartbeat_at_ms=int(NOW.timestamp()*1000), availability="online", temperature=24, queue_for_dispatch=True)
    db.close()
    monkeypatch.setattr(devices, "_sample_listeners", [])
    projection.recover_pending_heartbeats()
    assert len(db.fetch_pending_heartbeats()) == 1
    listener = Mock(side_effect=RuntimeError("injected"))
    monkeypatch.setattr(devices, "_sample_listeners", [listener])
    monkeypatch.setattr(devices, "_sample_listener_required", {})
    projection.recover_pending_heartbeats()
    assert len(db.fetch_pending_heartbeats()) == 1
    listener.side_effect = None
    listener.return_value = None
    projection.recover_pending_heartbeats()
    assert db.fetch_pending_heartbeats() == []
    assert listener.call_args.args[0].record_type == "HEARTBEAT"
    assert listener.call_args.args[0].temperature == 24


def test_bounded_invalid_token_is_evicted_without_extra_call():
    response = Mock(status_code=200)
    response.json.return_value = {"code": 99991663}
    with patch.object(feishu, "get_token", return_value="bad"), patch.object(feishu, "request_with_retry", return_value=response) as request, patch.object(feishu, "clear_token") as clear:
        feishu._request_bitable_json("PUT", "https://example.invalid", operation="audit", lock_key="audit", max_attempts=1)
    assert request.call_count == 1
    clear.assert_called_once()


@pytest.mark.parametrize("network_failure", [False, True])
def test_bitable_retries_have_one_shared_budget(network_failure):
    response = Mock(status_code=503)
    response.json.return_value = {"code": -503}
    effect = requests.Timeout("injected") if network_failure else None
    with patch.object(feishu, "get_token", return_value="token"), patch.object(feishu, "request_with_retry", return_value=response, side_effect=effect) as request, patch.object(feishu.time, "sleep"):
        if network_failure:
            with pytest.raises(requests.Timeout):
                feishu._request_bitable_json("POST", "https://example.invalid", operation="audit", lock_key="audit", max_attempts=3)
        else:
            feishu._request_bitable_json("POST", "https://example.invalid", operation="audit", lock_key="audit", max_attempts=3)
    assert request.call_count == 3
    assert all(call.kwargs["attempts"] == 1 for call in request.call_args_list)


def test_history_token_is_stable_across_separate_invocations(monkeypatch):
    monkeypatch.setattr(config, "APP_TOKEN", "test")
    with patch.object(feishu, "_request_bitable_json", return_value={"code": 0}) as request:
        fields = {"设备编号": "TH-01", "采集时间": 1791547200000}
        feishu.create_history_record("table", fields)
        feishu.create_history_record("table", fields)
        feishu.create_history_record("table", {**fields, "采集时间": fields["采集时间"] + 1})
    urls = [call.args[1] for call in request.call_args_list]
    assert urls[0] == urls[1]
    assert urls[0] != urls[2]


def test_public_status_never_reads_device_runtime_details(mirror):
    client = create_app().test_client()
    with patch("routes.api.runtime_status", side_effect=AssertionError("private")), patch("routes.api.active_canary_status", side_effect=AssertionError("private")):
        result = client.get("/api/system/status")
    assert result.status_code == 200
    payload = result.get_json()
    assert "device_states" not in payload["runtime"]
    assert "active_device_ids" not in payload
    assert "device_model" not in payload


def test_collector_retains_live_worker_after_shutdown_timeout(monkeypatch):
    thread, poller = Mock(), Mock()
    thread.is_alive.return_value = True
    monkeypatch.setattr(collector, "_modbus_thread", thread)
    monkeypatch.setattr(collector, "_modbus_poller", poller)
    monkeypatch.setattr(collector, "_started", True)
    monkeypatch.setattr(collector, "_modbus_error", None)
    collector.stop_collectors()
    assert collector._modbus_thread is thread
    assert collector._modbus_poller is poller
    assert collector._started
    collector.start_collectors()
    assert collector._modbus_thread is thread


def test_poller_does_not_publish_when_stop_arrives_during_poll():
    import threading
    poller = object.__new__(ModbusPoller)
    poller.device_id = "AUDIT"
    poller.endpoint = SimpleNamespace(transport="tcp", describe=lambda: "test")
    poller.unit_id = 1
    poller.poll_interval = 1
    poller._stop = threading.Event()
    poller._record_sample = Mock()
    poller._safe_close_client = Mock()
    def poll():
        poller._stop.set()
        return {"device": "AUDIT", "source": "modbus", "temperature": 24, "humidity": 50, "status": "online"}
    poller.poll_once = poll
    poller.run_forever()
    poller._record_sample.assert_not_called()
    poller._safe_close_client.assert_called_once()


def test_latest_sample_compares_naive_and_aware_as_instants(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_TIMEZONE", "Asia/Shanghai")
    connection = sqlite3.connect(":memory:")
    try:
        latest = SQLiteLatestSampleRepository(connection)
        latest.save(MonitorSample("AUDIT", NOW, 24, 50))
        latest.save(MonitorSample("AUDIT", NOW.replace(tzinfo=None), 40, 50))
        assert latest.get("AUDIT").temperature == 24
    finally:
        connection.close()


def test_ambiguous_history_retry_rechecks_remote_and_reuses_payload(mirror, monkeypatch):
    from services import history
    monkeypatch.setattr(history, "_latest_sample_cache", {})
    monkeypatch.setattr(history, "EXPECTED_HISTORY_DEVICES", ("TH-01",))
    monkeypatch.setattr(config, "HISTORY_TABLE_MAP", {"TH-01": "tblaudit"})
    monkeypatch.setattr(config, "HISTORY_API_KEY", "k" * 32)
    monkeypatch.setattr(config, "HISTORY_CLEANUP_ENABLED", False)
    records = [{"fields": {"设备编号": "TH-01", "在线状态": "在线", "当前温度": 24, "当前湿度": 50}}]
    with patch.object(history, "list_realtime_snapshots", return_value=records), patch.object(history, "get_latest_history_timestamp", return_value=None) as latest, patch.object(history, "create_history_record", side_effect=[requests.Timeout("response lost"), {"code": 0}]) as create:
        assert history.sample_history(NOW)[1] == 502
        records[0]["fields"]["当前温度"] = 40
        db.close()
        assert history.sample_history(NOW)[1] == 200
    assert latest.call_count == 2
    assert create.call_args_list[0].args[1] == create.call_args_list[1].args[1]
    assert create.call_args.args[1]["当前温度"] == 24


def test_dashboard_weights_samples_and_excludes_missing_values(mirror, monkeypatch):
    import json
    monkeypatch.setattr(config, "HISTORY_API_KEY", "k" * 32)
    current = datetime.now(timezone.utc)
    for index in range(10):
        at = current - timedelta(days=2 if index == 0 else 1, minutes=index)
        assert db.save_history_snapshot("AUDIT", at, {"采集时间": int(at.timestamp()*1000), "当前温度": 10 if index == 0 else 30, "当前湿度": 50 if index == 0 else None})
    response = create_app().test_client().get("/dashboard", headers={"Authorization": "Bearer " + "k"*32})
    assert response.status_code == 200
    embedded = response.get_data(as_text=True).partition("const data = ")[2].partition(";\n")[0]
    payload = json.loads(embedded)
    assert payload["avg"]["temperature"] == [28.0]
    assert payload["avg"]["humidity"] == [50.0]
