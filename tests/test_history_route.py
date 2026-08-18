from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from app import create_app


class HistoryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = create_app().test_client()

    def test_rejects_request_when_api_key_is_not_configured(self) -> None:
        with patch.object(config, "HISTORY_API_KEY", ""):
            response = self.client.post("/history/sample")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["cleanup_enabled"])

    def test_rejects_invalid_history_key(self) -> None:
        with patch.object(config, "HISTORY_API_KEY", "expected-key"):
            response = self.client.post(
                "/history/sample",
                headers={"X-History-Key": "wrong-key"},
            )

        self.assertEqual(response.status_code, 401)

    def test_rejects_configured_key_shorter_than_32_bytes(self) -> None:
        with patch.object(config, "HISTORY_API_KEY", "short-key"):
            response = self.client.post(
                "/history/sample",
                headers={"X-History-Key": "short-key"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("32", response.get_json()["error"])

    def test_returns_history_service_result_for_valid_key(self) -> None:
        service_result = {
            "status": "success",
            "created": ["TH-01"],
            "cleanup_enabled": False,
        }
        with (
            patch.object(config, "HISTORY_API_KEY", "x" * 32),
            patch("routes.history.validate_history_config"),
            patch("routes.history.sample_history", return_value=(service_result, 200)),
        ):
            response = self.client.post(
                "/history/sample",
                headers={"X-History-Key": "x" * 32},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), service_result)


if __name__ == "__main__":
    unittest.main()
