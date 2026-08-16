from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from app import create_app


class TemperatureRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = create_app().test_client()
        config.DEVICE_NAME_MAP = {"WAREHOUSE-TEMP": "DEV-01"}
        config.DEVICES = {}

    def test_uses_mapped_name_for_record_discovery(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01") as resolve,
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
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


if __name__ == "__main__":
    unittest.main()
