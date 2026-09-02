from __future__ import annotations

import unittest
from unittest.mock import patch

from flask import Flask

import config
from integrations.feishu_records import FeishuRawRecord
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
            "ACTIVE_DEVICE_IDS": config.ACTIVE_DEVICE_IDS,
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

    def _enable_active(self, *device_ids: str) -> None:
        config.AUTOMATION_MODE = "active"
        config.FEISHU_WRITE_ENABLED = True
        config.ACTIVE_DEVICE_IDS = device_ids

    @patch("routes.api.FeishuOperationRecordWriter", _FakeOperationWriter)
    def test_operation_route_requires_both_switches_and_maps_payload(self) -> None:
        self._enable_active("TH-03")

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

    def test_allowlisted_device_can_write_all_write_routes(self) -> None:
        self._enable_active("TH-10")
        headers = {"X-History-Key": "k" * 32}

        with patch("routes.api.FeishuOperationRecordWriter") as writer_class:
            writer_class.return_value.create_registration.return_value = {
                "record_id": "rec-operation"
            }
            response = self.client.post(
                "/api/operations",
                headers=headers,
                json={"device_id": " th-10 ", "area": "A", "action": "开始作业"},
            )
            self.assertEqual(response.status_code, 201)
            writer_class.return_value.create_registration.assert_called_once()

        with (
            patch("routes.api.FeishuEnvironmentEventWriter") as writer_class,
            patch("routes.api.FeishuBitableRecordWriter"),
        ):
            writer_class.return_value.create_event.return_value = {
                "record_id": "rec-event"
            }
            response = self.client.post(
                "/api/environment-events",
                headers=headers,
                json={
                    "device_id": "TH-10",
                    "area": "A",
                    "start_time": "2026-09-02T10:00:00+08:00",
                },
            )
            self.assertEqual(response.status_code, 201)
            writer_class.return_value.create_event.assert_called_once()

        with (
            patch("routes.api.FeishuInspectionRecordWriter") as writer_class,
            patch("routes.api.FeishuBitableRecordWriter"),
        ):
            writer_class.return_value.create_snapshot.return_value = {
                "record_id": "rec-inspection"
            }
            response = self.client.post(
                "/api/inspections",
                headers=headers,
                json={
                    "device_id": "TH-10",
                    "area": "A",
                    "inspected_at": "2026-09-02T10:00:00+08:00",
                },
            )
            self.assertEqual(response.status_code, 201)
            writer_class.return_value.create_snapshot.assert_called_once()

        with (
            patch("routes.api.FeishuBitableRecordSource") as source_class,
            patch("routes.api.FeishuEnvironmentEventWriter") as writer_class,
            patch("routes.api.FeishuBitableRecordWriter"),
        ):
            source_class.return_value.read_records.return_value = (
                FeishuRawRecord(record_id="rec-event", fields={"监测点": "TH-10"}),
            )
            writer_class.return_value.close_event.return_value = {
                "record_id": "rec-event"
            }
            response = self.client.patch(
                "/api/environment-events/rec-event",
                headers=headers,
                json={
                    "closed_at": "2026-09-02T11:00:00+08:00",
                    "cause": "cause",
                    "measure": "measure",
                    "product_impact": "none",
                },
            )
            self.assertEqual(response.status_code, 200)
            writer_class.return_value.close_event.assert_called_once()

    def test_non_allowlisted_device_returns_403_without_writer(self) -> None:
        self._enable_active("TH-10")
        headers = {"X-History-Key": "k" * 32}
        cases = (
            ("/api/operations", "routes.api.FeishuOperationRecordWriter"),
            ("/api/environment-events", "routes.api.FeishuEnvironmentEventWriter"),
            ("/api/inspections", "routes.api.FeishuInspectionRecordWriter"),
        )

        for path, writer_target in cases:
            with self.subTest(path=path):
                with (
                    patch(writer_target) as writer_class,
                    patch("routes.api.FeishuBitableRecordWriter") as base_writer,
                    patch("routes.api.FeishuBitableRecordSource") as source_class,
                ):
                    response = self.client.post(
                        path,
                        headers=headers,
                        json={"device_id": "TH-09"},
                    )

                self.assertEqual(response.status_code, 403)
                writer_class.assert_not_called()
                base_writer.assert_not_called()
                source_class.assert_not_called()

    def test_empty_allowlist_returns_403_without_writer(self) -> None:
        self._enable_active()
        with (
            patch("routes.api.FeishuOperationRecordWriter") as writer_class,
            patch("routes.api.FeishuBitableRecordWriter") as base_writer,
        ):
            response = self.client.post(
                "/api/operations",
                headers={"X-History-Key": "k" * 32},
                json={"device_id": "TH-10"},
            )

        self.assertEqual(response.status_code, 403)
        writer_class.assert_not_called()
        base_writer.assert_not_called()

    def test_patch_non_allowlisted_event_returns_403_without_writer(self) -> None:
        self._enable_active("TH-10")
        with (
            patch("routes.api.FeishuBitableRecordSource") as source_class,
            patch("routes.api.FeishuEnvironmentEventWriter") as writer_class,
            patch("routes.api.FeishuBitableRecordWriter") as base_writer,
        ):
            source_class.return_value.read_records.return_value = (
                FeishuRawRecord(record_id="rec-event", fields={"监测点": "TH-09"}),
            )
            response = self.client.patch(
                "/api/environment-events/rec-event",
                headers={"X-History-Key": "k" * 32},
                json={},
            )

        self.assertEqual(response.status_code, 403)
        source_class.assert_called_once_with()
        writer_class.assert_not_called()
        base_writer.assert_not_called()

    def test_patch_event_without_resolvable_device_fails_closed(self) -> None:
        self._enable_active("TH-10")
        with (
            patch("routes.api.FeishuBitableRecordSource") as source_class,
            patch("routes.api.FeishuEnvironmentEventWriter") as writer_class,
            patch("routes.api.FeishuBitableRecordWriter") as base_writer,
        ):
            source_class.return_value.read_records.return_value = (
                FeishuRawRecord(record_id="rec-event", fields={"处理状态": "处理中"}),
            )
            response = self.client.patch(
                "/api/environment-events/rec-event",
                headers={"X-History-Key": "k" * 32},
                json={},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("无法确定", response.get_json()["error"])
        writer_class.assert_not_called()
        base_writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
