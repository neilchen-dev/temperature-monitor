from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from app import create_app
from services import db


class TemperatureRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "DEVICE_NAME_MAP": config.DEVICE_NAME_MAP,
            "DEVICES": config.DEVICES,
            "TEMPERATURE_DEDUPE_WINDOW_MS": config.TEMPERATURE_DEDUPE_WINDOW_MS,
        }
        # 镜像指向临时目录：patch 之外的投影状态读写不得触碰真实生产库。
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "route-tests.db"
        config.SQLITE_ENABLED = False
        self.client = create_app().test_client()
        config.SQLITE_ENABLED = True
        config.DEVICE_NAME_MAP = {"WAREHOUSE-TEMP": "DEV-01"}
        config.DEVICES = {}
        config.TEMPERATURE_DEDUPE_WINDOW_MS = 5000
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        db._init_failed = False
        db.close()
        for name, value in self._original.items():
            setattr(config, name, value)
        self._tmp_dir.cleanup()

    def test_uses_mapped_name_for_record_discovery(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01") as resolve,
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
            patch("routes.temperature.devices.persist_sample") as persist,
        ):
            response = self.client.post(
                "/temperature",
                json={
                    "device": "warehouse-temp",
                    "temperature": 24.6,
                    "humidity": 52.0,
                    "status": "online",
                },
            )

        self.assertEqual(response.status_code, 200)
        resolve.assert_called_once_with("DEV-01", None)
        # 统一设备模型使用映射后的设备编号（与飞书/历史采样同一身份），
        # 而不是 HA 上报的原始名称
        persist.assert_called_once()
        self.assertEqual(persist.call_args.kwargs["device"], "DEV-01")
        self.assertEqual(persist.call_args.kwargs["source"], "home_assistant")

    def test_rejects_wrong_key_when_temperature_key_configured(self) -> None:
        config.TEMPERATURE_API_KEY = "unit-test-secret-key-0123456789"
        try:
            with (
                patch("routes.temperature.resolve_record_id") as resolve,
                patch("routes.temperature.update_feishu_fields"),
            ):
                response = self.client.post(
                    "/temperature",
                    headers={"X-Temperature-Key": "wrong"},
                    json={"device": "DEV-01", "temperature": 24.6, "humidity": 52.0},
                )
        finally:
            config.TEMPERATURE_API_KEY = ""

        self.assertEqual(response.status_code, 401)
        resolve.assert_not_called()

    def test_accepts_valid_temperature_key(self) -> None:
        config.TEMPERATURE_API_KEY = "unit-test-secret-key-0123456789"
        try:
            with (
                patch("routes.temperature.resolve_record_id", return_value="rec_01"),
                patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
                patch("routes.temperature.save_history"),
                patch("routes.temperature.devices.persist_sample"),
            ):
                response = self.client.post(
                    "/temperature",
                    headers={"X-Temperature-Key": "unit-test-secret-key-0123456789"},
                    json={"device": "DEV-01", "temperature": 24.6, "humidity": 52.0},
                )
        finally:
            config.TEMPERATURE_API_KEY = ""

        self.assertEqual(response.status_code, 200)

    def test_rejects_oversized_body(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id") as resolve,
            patch("routes.temperature.update_feishu_fields"),
        ):
            response = self.client.post(
                "/temperature",
                json={"device": "DEV-01", "temperature": 24.6, "humidity": 52.0,
                      "padding": "x" * 64 * 1024},
            )

        self.assertEqual(response.status_code, 413)
        resolve.assert_not_called()

    def test_success_response_shape_unchanged(self) -> None:
        """回归：飞书成功路径的响应字段与旧版完全一致。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0, "msg": "ok"}),
            patch("routes.temperature.save_history"),
            patch("routes.temperature.devices.persist_sample"),
        ):
            response = self.client.post(
                "/temperature",
                json={"device": "DEV-01", "temperature": 24.6, "humidity": 52.0},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.get_json()),
            {"status", "device", "online_status", "temperature_c", "humidity"},
        )
        body = response.get_json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["device"], "DEV-01")
        self.assertEqual(body["online_status"], "在线")

    def test_invalid_temperature_still_400_before_any_side_effect(self) -> None:
        """回归：无效温度仍是 400，且不发生持久化/飞书调用。"""
        with (
            patch("routes.temperature.resolve_record_id") as resolve,
            patch("routes.temperature.update_feishu_fields") as update,
            patch("routes.temperature.devices.persist_sample") as persist,
        ):
            response = self.client.post(
                "/temperature",
                json={"device": "DEV-01", "temperature": "not-a-number", "humidity": 52.0},
            )

        self.assertEqual(response.status_code, 400)
        resolve.assert_not_called()
        update.assert_not_called()
        persist.assert_not_called()

    def test_legacy_502_when_local_mirror_unavailable(self) -> None:
        """SQLITE_ENABLED=false（本地持久化不可用）保持旧版 502 语义。"""
        config.SQLITE_ENABLED = False
        try:
            with (
                patch(
                    "routes.temperature.resolve_record_id",
                    side_effect=RuntimeError("feishu down"),
                ),
                patch("routes.temperature.update_feishu_fields"),
                patch("routes.temperature.save_history"),
            ):
                response = self.client.post(
                    "/temperature",
                    json={"device": "DEV-01", "temperature": 24.6, "humidity": 52.0},
                )
        finally:
            config.SQLITE_ENABLED = True

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["status"], "error")


if __name__ == "__main__":
    unittest.main()
