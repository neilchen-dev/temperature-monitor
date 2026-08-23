from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config
from services import db, devices


class DeviceModelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "EVENT_TEMPERATURE_HIGH_C": config.EVENT_TEMPERATURE_HIGH_C,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "devices.db"
        config.EVENT_TEMPERATURE_HIGH_C = None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def _samples(self, device: str) -> list[dict]:
        return db.fetch_device_samples(device, limit=100)

    def _events(self, device: str) -> list[dict]:
        return db.fetch_device_events(device_id=device, limit=100)

    def test_first_sample_is_baseline_without_events(self) -> None:
        transitions = devices.record_sample(
            "plc-01", "modbus", 25.2, 48.1, "online"
        )
        self.assertEqual(transitions, [])
        self.assertEqual(len(self._samples("PLC-01")), 1)
        self.assertEqual(self._events("PLC-01"), [])

    def test_steady_state_does_not_repeat_events(self) -> None:
        devices.record_sample("PLC-01", "modbus", 25.0, 50.0, "online", 1755000000000)
        devices.record_sample("PLC-01", "modbus", 25.1, 50.1, "online", 1755000001000)
        devices.record_sample("PLC-01", "modbus", 25.2, 50.2, "online", 1755000002000)
        self.assertEqual(len(self._samples("PLC-01")), 3)
        self.assertEqual(self._events("PLC-01"), [])

    def test_status_change_creates_single_event(self) -> None:
        devices.record_sample("PLC-01", "modbus", 25.0, 50.0, "online", 1755000000000)
        transitions = devices.record_sample(
            "PLC-01", "modbus", None, None, "offline", 1755000001000
        )
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["event_type"], "status_change")
        self.assertEqual(transitions[0]["old_state"], "ONLINE")
        self.assertEqual(transitions[0]["new_state"], "OFFLINE")

        # 再来一条 offline 样本不会产生第二个事件
        transitions = devices.record_sample(
            "PLC-01", "modbus", None, None, "offline", 1755000002000
        )
        self.assertEqual(transitions, [])
        self.assertEqual(len(self._events("PLC-01")), 1)

        # 恢复在线也只产生一次事件
        transitions = devices.record_sample("PLC-01", "modbus", 25.0, 50.0, "online", 1755000003000)
        self.assertEqual(
            [t["new_state"] for t in transitions], ["ONLINE"]
        )
        self.assertEqual(len(self._events("PLC-01")), 2)

    def test_same_device_id_two_sources_do_not_pollute_state(self) -> None:
        # 状态机身份是 (device, source)：同一 device_id 由 HA 与 Modbus
        # 同时上报时，一侧的在线/离线不得影响另一侧的事件判定
        devices.record_sample(
            "TH-01", "home_assistant", 24.0, 50.0, "online", 1755000000000
        )
        devices.record_sample(
            "TH-01", "modbus", 25.0, 51.0, "online", 1755000000500
        )
        # HA 侧离线
        transitions = devices.record_sample(
            "TH-01", "home_assistant", None, None, "offline", 1755000001000
        )
        self.assertEqual([t["new_state"] for t in transitions], ["OFFLINE"])
        # Modbus 侧继续在线采样：不得产生任何事件（它自己的状态没变）
        transitions = devices.record_sample(
            "TH-01", "modbus", 25.1, 51.1, "online", 1755000002000
        )
        self.assertEqual(transitions, [])
        # Modbus 侧的离线转移独立发生
        transitions = devices.record_sample(
            "TH-01", "modbus", None, None, "offline", 1755000003000
        )
        self.assertEqual([t["new_state"] for t in transitions], ["OFFLINE"])

        events = self._events("TH-01")
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {e["source"] for e in events}, {"home_assistant", "modbus"}
        )

        # 最新状态按 (device, source) 各一行
        states = {
            row["source"]: row for row in db.fetch_latest_device_states()
        }
        self.assertEqual(len(states), 2)
        self.assertEqual(states["home_assistant"]["status"], "offline")
        self.assertEqual(states["modbus"]["status"], "offline")

    def test_offline_failure_path_inserts_row_only_on_change(self) -> None:
        devices.record_sample("PLC-01", "modbus", 25.0, 50.0, "online", 1755000000000)
        # only_on_status_change=True：首次失败产生一条 offline 样本 + 事件
        devices.record_sample(
            "PLC-01", "modbus", None, None, "offline",
            sample_time_ms=1755000001000, only_on_status_change=True,
        )
        # 持续失败：不再插入重复 offline 行（防 5 秒轮询刷表）
        devices.record_sample(
            "PLC-01", "modbus", None, None, "offline",
            sample_time_ms=1755000002000, only_on_status_change=True,
        )
        devices.record_sample(
            "PLC-01", "modbus", None, None, "offline",
            sample_time_ms=1755000003000, only_on_status_change=True,
        )
        rows = self._samples("PLC-01")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "offline")
        self.assertEqual(len(self._events("PLC-01")), 1)

    def test_temperature_threshold_events(self) -> None:
        config.EVENT_TEMPERATURE_HIGH_C = 30.0
        devices.record_sample("TH-03", "home_assistant", 28.0, 55.0, "online", 1755000000000)
        # 越过阈值 -> TEMPERATURE_HIGH
        transitions = devices.record_sample(
            "TH-03", "home_assistant", 31.5, 55.0, "online", 1755000001000
        )
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["event_type"], "temperature_alert")
        self.assertEqual(transitions[0]["old_state"], "NORMAL")
        self.assertEqual(transitions[0]["new_state"], "TEMPERATURE_HIGH")
        self.assertEqual(transitions[0]["value"], 31.5)
        # 高位持续：无新事件
        transitions = devices.record_sample(
            "TH-03", "home_assistant", 32.0, 55.0, "online", 1755000002000
        )
        self.assertEqual(transitions, [])
        # 回落 -> 恢复事件
        transitions = devices.record_sample(
            "TH-03", "home_assistant", 29.9, 55.0, "online", 1755000003000
        )
        self.assertEqual(
            [(t["old_state"], t["new_state"]) for t in transitions],
            [("TEMPERATURE_HIGH", "NORMAL")],
        )

    def test_invalid_input_swallowed(self) -> None:
        # 未知数据源 / 未知状态 / 空设备名：不落库也不抛异常
        self.assertEqual(
            devices.record_sample("PLC-01", "mqtt", 25.0, 50.0, "online"), []
        )
        self.assertEqual(
            devices.record_sample("PLC-01", "modbus", 25.0, 50.0, "weird"), []
        )
        self.assertEqual(
            devices.record_sample("", "modbus", 25.0, 50.0, "online"), []
        )
        self.assertEqual(db.fetch_latest_device_states(), [])

    def test_multi_source_latest_state(self) -> None:
        devices.record_sample("TH-01", "home_assistant", 24.6, 52.0, "online", 1755000000000)
        devices.record_sample("PLC-01", "modbus", 25.2, 48.1, "online", 1755000001000)
        devices.record_sample("TH-01", "home_assistant", 24.8, 53.0, "online", 1755000002000)

        states = {
            row["device"]: row for row in db.fetch_latest_device_states()
        }
        self.assertEqual(set(states), {"PLC-01", "TH-01"})
        self.assertEqual(states["TH-01"]["source"], "home_assistant")
        self.assertEqual(states["TH-01"]["temperature"], 24.8)
        self.assertEqual(states["TH-01"]["sample_count"], 2)
        self.assertEqual(states["PLC-01"]["source"], "modbus")

    def test_duplicate_timestamp_replaces_row(self) -> None:
        devices.record_sample("PLC-01", "modbus", 25.0, 50.0, "online", 1755000000000)
        devices.record_sample("PLC-01", "modbus", 26.0, 51.0, "online", 1755000000000)
        rows = self._samples("PLC-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["temperature"], 26.0)


if __name__ == "__main__":
    unittest.main()
