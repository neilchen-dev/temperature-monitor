"""Measurement/heartbeat freshness contract tests."""

from __future__ import annotations

import tempfile
import time
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

import config
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    AlarmLifecycleState,
    AlarmState,
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
)
from domain.monitor_engine import MonitorEngine
from repositories.runtime_state import SQLiteLatestSampleRepository
from services import db, devices


class FreshnessModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "DEVICE_MODEL_STALE_SECONDS": config.DEVICE_MODEL_STALE_SECONDS,
            "SHADOW_DEVICE_IDS": config.SHADOW_DEVICE_IDS,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "freshness.db"
        config.DEVICE_MODEL_STALE_SECONDS = 300
        config.SHADOW_DEVICE_IDS = ("TH-01",)
        devices._reset_device_model_stats()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()
        devices._reset_device_model_stats()

    def _measurement(self, sample_ms: int, *, temperature: float = 25.0) -> None:
        devices.persist_sample(
            "TH-01",
            devices.SOURCE_HOME_ASSISTANT,
            temperature,
            50.0,
            "online",
            sample_time_ms=sample_ms,
        )

    def test_static_value_stays_fresh_with_one_minute_heartbeats(self) -> None:
        now = time.time()
        self._measurement(int((now - 1200) * 1000))
        for offset in range(0, 1201, 60):
            with patch("services.devices.time.time", return_value=now + offset):
                outcome = devices.persist_heartbeat(
                    "TH-01", devices.SOURCE_HOME_ASSISTANT, "online", 25.0, 50.0
                )
            self.assertTrue(outcome.persisted)

        health = devices.get_device_model_health(now=now + 1200)
        state = health["device_states"][0]
        self.assertTrue(state["heartbeat_fresh"])
        self.assertFalse(state["measurement_fresh"])
        self.assertTrue(state["effective_fresh"])
        self.assertFalse(health["degraded"])
        self.assertEqual(len(db.fetch_device_samples("TH-01")), 1)

    def test_measurement_and_heartbeat_timestamps_are_separate(self) -> None:
        now = time.time()
        old_ms = int((now - 600) * 1000)
        with patch("services.devices.time.time", return_value=now):
            self._measurement(old_ms)
            heartbeat = devices.persist_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "online", 25.0, 50.0
            )
        self.assertIsNotNone(heartbeat.sample)
        self.assertEqual(len(db.fetch_device_samples("TH-01")), 1)
        presence = db.fetch_latest_device_presence()[0]
        self.assertEqual(presence["last_measurement_at_ms"], old_ms)
        self.assertEqual(presence["last_heartbeat_at_ms"], int(now * 1000))
        assert heartbeat.sample is not None
        self.assertEqual(heartbeat.sample.record_type, "HEARTBEAT")
        self.assertEqual(
            int(heartbeat.sample.measurement_time.timestamp() * 1000), old_ms
        )

    def test_changed_measurement_advances_measurement_timestamp(self) -> None:
        now = time.time()
        self._measurement(int((now - 600) * 1000))
        current_ms = int(now * 1000)
        self._measurement(current_ms, temperature=25.1)
        presence = db.fetch_latest_device_presence()[0]
        self.assertEqual(presence["last_measurement_at_ms"], current_ms)
        self.assertGreaterEqual(presence["last_heartbeat_at_ms"], current_ms)

    def test_stopped_heartbeat_becomes_stale(self) -> None:
        now = time.time()
        with patch("services.devices.time.time", return_value=now):
            self._measurement(int((now - 600) * 1000))
            devices.persist_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "online", 25.0, 50.0
            )
        health = devices.get_device_model_health(now=now + 301)
        state = health["device_states"][0]
        self.assertFalse(state["heartbeat_fresh"])
        self.assertFalse(state["effective_fresh"])
        self.assertEqual(health["stale_devices"], ["TH-01"])

    def test_unavailable_is_immediate_and_unavailable_heartbeat_does_not_restore(self) -> None:
        now = time.time()
        with patch("services.devices.time.time", return_value=now):
            self._measurement(int(now * 1000))
            devices.persist_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "unavailable"
            )
        health = devices.get_device_model_health(now=now + 1)
        self.assertEqual(health["unavailable_devices"], ["TH-01"])
        self.assertFalse(health["device_states"][0]["effective_fresh"])

        with patch("services.devices.time.time", return_value=now + 2):
            devices.persist_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "unavailable"
            )
        health = devices.get_device_model_health(now=now + 2)
        self.assertEqual(health["unavailable_devices"], ["TH-01"])

    def test_available_heartbeat_recovers_presence(self) -> None:
        now = time.time()
        with patch("services.devices.time.time", return_value=now):
            self._measurement(int(now * 1000))
            devices.persist_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "unavailable"
            )
            devices.persist_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "online", 25.0, 50.0
            )
        health = devices.get_device_model_health(now=now + 1)
        self.assertEqual(health["unavailable_devices"], [])
        self.assertTrue(health["device_states"][0]["effective_fresh"])

    def test_heartbeat_dispatch_is_tagged_and_device_scoped(self) -> None:
        with patch.object(devices, "dispatch_sample") as dispatch:
            sample = devices.record_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "online", 25.0, 50.0
            )
        self.assertIsNotNone(sample)
        dispatch.assert_called_once()
        dispatched = dispatch.call_args.args[0]
        self.assertEqual(dispatched.device_id, "TH-01")
        self.assertEqual(dispatched.record_type, "HEARTBEAT")
        self.assertEqual(dispatched.availability, "online")

    def test_presence_rows_are_scoped_per_device(self) -> None:
        with patch("services.devices.time.time", return_value=time.time()):
            devices.persist_heartbeat(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, "online", 25.0, 50.0
            )
            devices.persist_heartbeat(
                "TH-02", devices.SOURCE_HOME_ASSISTANT, "online", 26.0, 51.0
            )
        rows = {
            row["device"]: row for row in db.fetch_latest_device_presence()
        }
        self.assertEqual(set(rows), {"TH-01", "TH-02"})
        self.assertEqual(rows["TH-01"]["temperature"], 25.0)
        self.assertEqual(rows["TH-02"]["temperature"], 26.0)

    def test_expired_verification_sample_cannot_advance_alarm(self) -> None:
        from datetime import datetime, timedelta, timezone
        from runtime.shadow_runner import ShadowRuntime

        now = datetime(2026, 9, 10, tzinfo=timezone.utc)
        runtime = object.__new__(ShadowRuntime)
        runtime.now_provider = lambda: now
        expired = MonitorSample(
            "TH-01",
            now - timedelta(seconds=301),
            27.0,
            50.0,
            online_status="online",
            record_type="MEASUREMENT",
            availability="online",
        )
        evaluated = runtime._fresh_sample_or_offline(expired)
        self.assertEqual(evaluated.data_quality.value, "OFFLINE")
        self.assertIsNone(evaluated.temperature)

        fresh_heartbeat = MonitorSample(
            "TH-01",
            now - timedelta(minutes=20),
            27.0,
            50.0,
            online_status="online",
            record_type="HEARTBEAT",
            heartbeat_time=now,
            availability="online",
        )
        self.assertIs(runtime._fresh_sample_or_offline(fresh_heartbeat), fresh_heartbeat)

    def test_static_overlimit_value_can_advance_alarm_timer_on_heartbeat(self) -> None:
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
        device = DeviceContext("TH-01", "warehouse")
        standard = EnvironmentStandard(
            standard_id="ENV-TH-01",
            revision="R1",
            area="warehouse",
            operation_type=None,
            temperature_min=20.0,
            temperature_max=26.0,
            humidity_min=40.0,
            humidity_max=60.0,
            effective_from=start - timedelta(days=1),
            effective_to=None,
            source_document="test",
            clause=None,
            control_type=ControlType.ALL_DAY,
        )
        machine = AlarmStateMachine()
        heartbeat = MonitorSample(
            "TH-01",
            start,
            27.0,
            50.0,
            online_status="online",
            record_type="HEARTBEAT",
            measurement_time=start - timedelta(minutes=20),
            heartbeat_time=start,
            availability="online",
        )
        first = MonitorEngine.evaluate(
            device=device, sample=heartbeat, standard=standard
        )
        pending = machine.apply(
            result=first, current_state=AlarmState.normal("TH-01"), now=start
        )
        self.assertEqual(pending.next.state, AlarmLifecycleState.PENDING)

        second_heartbeat = MonitorSample(
            "TH-01",
            start + timedelta(minutes=5),
            heartbeat.temperature,
            heartbeat.humidity,
            online_status="online",
            record_type="HEARTBEAT",
            measurement_time=heartbeat.measurement_time,
            heartbeat_time=start + timedelta(minutes=5),
            availability="online",
        )
        second = MonitorEngine.evaluate(
            device=device, sample=second_heartbeat, standard=standard
        )
        alarm = machine.apply(
            result=second,
            current_state=pending.next,
            now=start + timedelta(minutes=5),
        )
        self.assertEqual(alarm.next.state, AlarmLifecycleState.ALARM)

    def test_runtime_latest_sample_migration_preserves_old_rows(self) -> None:
        from datetime import datetime, timezone

        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE latest_monitor_samples ("
            "device_id TEXT PRIMARY KEY, sample_time TEXT NOT NULL, "
            "temperature REAL, humidity REAL, online_status TEXT, "
            "data_quality TEXT)"
        )
        repo = SQLiteLatestSampleRepository(connection)
        sample = MonitorSample(
            "TH-01",
            datetime(2026, 9, 10, tzinfo=timezone.utc),
            25.0,
            50.0,
            online_status="online",
            record_type="HEARTBEAT",
            measurement_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
            heartbeat_time=datetime(2026, 9, 10, tzinfo=timezone.utc),
            availability="online",
        )
        repo.save(sample)
        restored = repo.get("TH-01")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.record_type, "HEARTBEAT")
        self.assertEqual(restored.measurement_time, sample.measurement_time)
        self.assertEqual(restored.heartbeat_time, sample.heartbeat_time)
        self.assertEqual(
            connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
