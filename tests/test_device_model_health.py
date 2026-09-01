"""统一设备模型断流可观测性测试（P0.2）。

record_sample 保持旁路隔离（异常吞掉、旧采集链路可用），但断流必须可见：
- 错误计数 / 最后错误 / 最后成功时间
- /temperature 仍有上报但统一样本停滞 → degraded
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from services import db, devices


class DeviceModelHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "DEVICE_MODEL_STALE_SECONDS": config.DEVICE_MODEL_STALE_SECONDS,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "health.db"
        config.DEVICE_MODEL_STALE_SECONDS = 300
        devices._reset_device_model_stats()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()
        devices._reset_device_model_stats()

    def test_record_sample_error_is_visible(self) -> None:
        """record_sample 异常被吞，但错误计数与最后错误必须暴露。"""
        with (
            patch(
                "services.devices.evaluate_transitions",
                side_effect=RuntimeError("sqlite mirror exploded"),
            ),
            patch(
                "services.devices.db.fetch_previous_device_sample", return_value=None
            ),
            patch("services.devices.db.save_device_sample"),
        ):
            result = devices.record_sample(
                "TH-01", devices.SOURCE_HOME_ASSISTANT, 25.0, 50.0, "online"
            )
        # 旁路隔离：返回空转移，不抛异常。
        self.assertEqual(result, [])
        health = devices.get_device_model_health()
        self.assertGreaterEqual(health["device_sample_error_count"], 1)
        self.assertIn("RuntimeError", health["device_sample_last_error"])
        self.assertIsNotNone(health["device_sample_last_error_time"])

    def test_successful_sample_updates_last_success(self) -> None:
        """正常样本会推进 last_successful_sample_time。"""
        devices.record_sample(
            "TH-01", devices.SOURCE_HOME_ASSISTANT, 24.0, 50.0, "online"
        )
        health = devices.get_device_model_health()
        self.assertIsNotNone(health["last_successful_sample_time"])
        self.assertEqual(health["device_sample_error_count"], 0)
        self.assertFalse(health["degraded"])

    def test_degraded_when_reports_active_but_no_samples(self) -> None:
        """/temperature 活着但没有任何统一样本落库 → degraded。"""
        now = time.time()
        with patch(
            "services.devices.db.fetch_device_summary",
            return_value={"last_sample_time_ms": None},
        ):
            devices.note_temperature_request("TH-01")
            health = devices.get_device_model_health(now=now + 10)
        self.assertTrue(health["degraded"])
        self.assertTrue(
            any("no unified sample persisted" in r for r in health["degraded_reasons"])
        )

    def test_degraded_when_unified_samples_are_stale(self) -> None:
        """/temperature 活着但统一样本超过阈值未更新 → degraded。"""
        now = time.time()
        with patch(
            "services.devices.db.fetch_device_summary",
            return_value={"last_sample_time_ms": (now - 600) * 1000},
        ):
            devices.note_temperature_request("TH-01")
            health = devices.get_device_model_health(now=now + 10)
        self.assertTrue(health["degraded"])
        self.assertTrue(
            any("stale" in r for r in health["degraded_reasons"])
        )

    def test_healthy_when_reports_and_samples_both_fresh(self) -> None:
        now = time.time()
        with patch(
            "services.devices.db.fetch_device_summary",
            return_value={"last_sample_time_ms": now * 1000},
        ):
            devices.note_temperature_request("TH-01")
            health = devices.get_device_model_health(now=now + 10)
        self.assertFalse(health["degraded"])
        self.assertEqual(health["degraded_reasons"], [])

    def test_no_degraded_without_recent_temperature_reports(self) -> None:
        """上报早已停止（HA 停发）不判 degraded——那是采集侧问题，由采集日志负责。"""
        devices._reset_device_model_stats()
        with patch(
            "services.devices.db.fetch_device_summary",
            return_value={"last_sample_time_ms": None},
        ):
            health = devices.get_device_model_health()
        self.assertFalse(health["degraded"])


if __name__ == "__main__":
    unittest.main()
