from __future__ import annotations

import unittest
from unittest.mock import patch

from flask import Flask

import config
from routes.api import api_bp


class _FakeOperationWriter:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def create_registration(self, **kwargs):
        self.kwargs["payload"] = kwargs
        return {"record_id": "rec-operation"}


class FeishuWriteRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            "HISTORY_API_KEY": config.HISTORY_API_KEY,
            "AUTOMATION_MODE": config.AUTOMATION_MODE,
            "FEISHU_WRITE_ENABLED": config.FEISHU_WRITE_ENABLED,
        }
        config.HISTORY_API_KEY = "k" * 32
        config.AUTOMATION_MODE = "disabled"
        config.FEISHU_WRITE_ENABLED = False
        app = Flask(__name__)
        app.register_blueprint(api_bp)
        self.client = app.test_client()

    def tearDown(self) -> None:
        for name, value in self.original.items():
            setattr(config, name, value)

    def test_write_routes_are_closed_by_default(self) -> None:
        response = self.client.post(
            "/api/operations",
            headers={"X-History-Key": "k" * 32},
            json={},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("FEISHU_WRITE_ENABLED=true", response.get_json()["error"])

    @patch("routes.api.FeishuOperationRecordWriter", _FakeOperationWriter)
    def test_operation_route_requires_both_switches_and_maps_payload(self) -> None:
        config.AUTOMATION_MODE = "active"
        config.FEISHU_WRITE_ENABLED = True

        response = self.client.post(
            "/api/operations",
            headers={"X-History-Key": "k" * 32},
            json={
                "device_id": "TH-03",
                "area": "精密装配间",
                "action": "开始作业",
                "status_recorded_at": "2026-08-31T10:00:00+08:00",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["feishu"]["record_id"], "rec-operation")


if __name__ == "__main__":
    unittest.main()
