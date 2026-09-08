from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import config
from app import create_app
from services import db


TEST_KEY = "unit-test-secret-key-0123456789"


class ThresholdApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "HISTORY_API_KEY": config.HISTORY_API_KEY,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(":memory:")
        config.HISTORY_API_KEY = TEST_KEY
        self.addCleanup(self._restore)

        self.client = create_app().test_client()
        self.headers = {"X-History-Key": TEST_KEY}

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def _put(self, device: str, body, headers=None):
        return self.client.put(
            f"/api/thresholds/{device}",
            json=body,
            headers=headers if headers is not None else self.headers,
        )

    # -- 鉴权：与 /api/devices 完全同策略 --

    def test_unconfigured_key_returns_503(self) -> None:
        config.HISTORY_API_KEY = ""
        self.assertEqual(self.client.get("/api/thresholds").status_code, 503)
        self.assertEqual(
            self._put("TH-01", {"temp_min": 1}).status_code, 503,
        )

    def test_wrong_key_returns_401(self) -> None:
        self.assertEqual(
            self.client.get(
                "/api/thresholds", headers={"X-History-Key": "wrong"},
            ).status_code, 401,
        )
        self.assertEqual(
            self._put(
                "TH-01", {"temp_min": 1}, headers={"X-History-Key": "wrong"},
            ).status_code, 401,
        )

    def test_mirror_disabled_returns_503_not_silent_ok(self) -> None:
        with patch("routes.api.db.is_enabled", return_value=False):
            self.assertEqual(
                self.client.get(
                    "/api/thresholds", headers=self.headers,
                ).status_code, 503,
            )
            self.assertEqual(
                self._put(
                    "TH-01", {"temp_min": 1}, headers=self.headers,
                ).status_code, 503,
            )

    # -- Feishu authoritative read-only contract --

    def test_get_declares_feishu_authority_and_does_not_read_legacy_cache(self) -> None:
        db.save_device_threshold("TH-01", 18.0, 26.0, 40.0, 60.0)
        response = self.client.get("/api/thresholds", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["authoritative_source"], "feishu")
        self.assertEqual(response.get_json()["items"], [])

    def test_put_is_rejected_and_cannot_change_legacy_cache(self) -> None:
        db.save_device_threshold("TH-01", 18.0, 26.0, 40.0, 60.0)
        response = self._put(" th-02 ", {
            "temp_min": 20.0, "temp_max": 26.0,
            "humidity_min": 40.0, "humidity_max": 60.0,
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["authoritative_source"], "feishu")
        self.assertEqual(db.fetch_device_thresholds("TH-01")[0]["temp_min"], 18.0)


class ConsoleRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = create_app().test_client()

    def test_console_serves_spa_shell_without_key(self) -> None:
        # 页面壳不含数据，与 /health 同级开放；数据接口仍需密钥
        response = self.client.get("/console")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.mimetype == "text/html",
        )
        self.assertIn("工业监控台", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
