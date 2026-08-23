from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

import config
from services import collector, db
from tests.helpers import ModbusTestServer


class CollectorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "MODBUS_ENABLED": config.MODBUS_ENABLED,
            "MODBUS_TRANSPORT": config.MODBUS_TRANSPORT,
            "MODBUS_HOST": config.MODBUS_HOST,
            "MODBUS_PORT": config.MODBUS_PORT,
            "MODBUS_SERIAL_PORT": config.MODBUS_SERIAL_PORT,
            "MODBUS_BAUDRATE": config.MODBUS_BAUDRATE,
            "MODBUS_PARITY": config.MODBUS_PARITY,
            "MODBUS_STOPBITS": config.MODBUS_STOPBITS,
            "MODBUS_BYTESIZE": config.MODBUS_BYTESIZE,
            "MODBUS_TIMEOUT_SECONDS": config.MODBUS_TIMEOUT_SECONDS,
            "MODBUS_UNIT_ID": config.MODBUS_UNIT_ID,
            "MODBUS_DEVICE_ID": config.MODBUS_DEVICE_ID,
            "MODBUS_POLL_INTERVAL_SECONDS": config.MODBUS_POLL_INTERVAL_SECONDS,
            "MODBUS_REGISTER_MAP": config.MODBUS_REGISTER_MAP,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "collector.db"
        config.MODBUS_ENABLED = False
        config.MODBUS_TRANSPORT = "tcp"
        self.addCleanup(self._restore)
        self.addCleanup(collector.stop_collectors)

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def test_disabled_by_default_starts_no_thread(self) -> None:
        before = threading.active_count()
        collector.start_collectors()
        self.assertEqual(threading.active_count(), before)
        status = collector.get_collector_status()
        self.assertEqual(status, {"modbus": {"enabled": False}})

    def test_repeated_start_is_noop(self) -> None:
        self.server = ModbusTestServer([252, 481, 1])
        self.server.start()
        self.addCleanup(self.server.stop)
        config.MODBUS_ENABLED = True
        config.MODBUS_HOST = "127.0.0.1"
        config.MODBUS_PORT = self.server.port
        config.MODBUS_POLL_INTERVAL_SECONDS = 1.0

        collector.start_collectors()
        thread_first = collector._modbus_thread
        collector.start_collectors()
        self.assertIs(collector._modbus_thread, thread_first)
        self.assertEqual(thread_first.name, "modbus-collector")
        self.assertTrue(thread_first.daemon)

    def test_invalid_register_map_disables_collector_not_app(self) -> None:
        config.MODBUS_ENABLED = True
        config.MODBUS_REGISTER_MAP = "{broken json"
        collector.start_collectors()
        status = collector.get_collector_status()["modbus"]
        self.assertTrue(status["enabled"])
        self.assertFalse(status["running"])
        self.assertIn("ModbusConfigError", status["error_summary"])
        self.assertIsNone(collector._modbus_thread)

    def test_rtu_without_serial_port_disables_collector(self) -> None:
        config.MODBUS_ENABLED = True
        config.MODBUS_TRANSPORT = "rtu"
        config.MODBUS_SERIAL_PORT = ""
        collector.start_collectors()
        status = collector.get_collector_status()["modbus"]
        self.assertFalse(status["running"])
        self.assertIn("MODBUS_SERIAL_PORT", status["error_summary"])
        self.assertIsNone(collector._modbus_thread)

    def test_invalid_unit_id_disables_collector(self) -> None:
        config.MODBUS_ENABLED = True
        config.MODBUS_TRANSPORT = "tcp"
        config.MODBUS_UNIT_ID = 0
        collector.start_collectors()
        status = collector.get_collector_status()["modbus"]
        self.assertFalse(status["running"])
        self.assertIn("unit id", status["error_summary"])
        self.assertIsNone(collector._modbus_thread)

    def test_end_to_end_thread_records_samples(self) -> None:
        self.server = ModbusTestServer([252, 481, 1])
        self.server.start()
        self.addCleanup(self.server.stop)
        config.MODBUS_ENABLED = True
        config.MODBUS_HOST = "127.0.0.1"
        config.MODBUS_PORT = self.server.port
        config.MODBUS_POLL_INTERVAL_SECONDS = 1.0

        collector.start_collectors()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            states = db.fetch_latest_device_states()
            if states:
                break
            time.sleep(0.2)
        states = db.fetch_latest_device_states()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["device"], "PLC-01")
        self.assertEqual(states[0]["source"], "modbus")
        self.assertEqual(states[0]["temperature"], 25.2)
        self.assertEqual(states[0]["status"], "online")
        status = collector.get_collector_status()["modbus"]
        self.assertTrue(status["running"])
        self.assertEqual(status["transport"], "tcp")
        self.assertIsNotNone(status["last_success"])


if __name__ == "__main__":
    unittest.main()
