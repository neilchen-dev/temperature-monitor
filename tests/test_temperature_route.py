from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from app import create_app


class TemperatureRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        config.SQLITE_ENABLED = False
        self.client = create_app().test_client()
        config.SQLITE_ENABLED = True
        config.DEVICE_NAME_MAP = {"WAREHOUSE-TEMP": "DEV-01"}
        config.DEVICES = {}

    def test_uses_mapped_name_for_record_discovery(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01") as resolve,
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
            patch("routes.temperature.devices.record_sample") as record,
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
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["device"], "DEV-01")
        self.assertEqual(record.call_args.kwargs["source"], "home_assistant")

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
                patch("routes.temperature.devices.record_sample"),
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


if __name__ == "__main__":
    unittest.main()
