from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from app import create_app
from services import db


TEST_KEY = "unit-test-secret-key-0123456789"


class ThresholdApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "HISTORY_API_KEY": config.HISTORY_API_KEY,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "thresholds.db"
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

    # -- 读写 --

    def test_get_empty_then_put_then_get(self) -> None:
        response = self.client.get("/api/thresholds", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], [])

        response = self._put("TH-01", {
            "temp_min": 18.0, "temp_max": 26.0,
            "humidity_min": 40.0, "humidity_max": 60.0,
        })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["device"], "TH-01")
        self.assertEqual(body["threshold"]["temp_min"], 18.0)
        self.assertEqual(body["threshold"]["humidity_max"], 60.0)

        items = self.client.get(
            "/api/thresholds", headers=self.headers,
        ).get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["device"], "TH-01")

    def test_put_normalizes_device_id(self) -> None:
        response = self._put(" th-02 ", {
            "temp_min": None, "temp_max": None,
            "humidity_min": None, "humidity_max": None,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["device"], "TH-02")

    def test_put_replaces_previous_band(self) -> None:
        self._put("TH-01", {
            "temp_min": 18.0, "temp_max": 26.0,
            "humidity_min": 40.0, "humidity_max": 60.0,
        })
        response = self._put("TH-01", {
            "temp_min": 20.0, "temp_max": None,
            "humidity_min": None, "humidity_max": 70.0,
        })
        self.assertEqual(response.status_code, 200)
        items = self.client.get(
            "/api/thresholds", headers=self.headers,
        ).get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["temp_max"])
        self.assertIsNone(items[0]["humidity_min"])
        self.assertEqual(items[0]["humidity_max"], 70.0)

    def test_write_failure_returns_503(self) -> None:
        # 阈值是本地权威数据：写入失败必须显式失败，不能伪装成功
        with patch(
            "routes.api.db.save_device_threshold", return_value=False,
        ):
            response = self._put("TH-01", {
                "temp_min": 18.0, "temp_max": 26.0,
                "humidity_min": 40.0, "humidity_max": 60.0,
            })
        self.assertEqual(response.status_code, 503)

    # -- 入参校验 --

    def test_put_rejects_missing_fields(self) -> None:
        response = self._put("TH-01", {"temp_min": 18.0})
        self.assertEqual(response.status_code, 400)
        self.assertIn("缺少字段", response.get_json()["error"])

    def test_put_rejects_non_json_body(self) -> None:
        response = self.client.put(
            "/api/thresholds/TH-01",
            data="not-json",
            content_type="application/json",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_put_rejects_non_numeric_values(self) -> None:
        for bad in ("18", True, [18], {"v": 1}):
            response = self._put("TH-01", {
                "temp_min": bad, "temp_max": 26.0,
                "humidity_min": 40.0, "humidity_max": 60.0,
            })
            self.assertEqual(response.status_code, 400, msg=repr(bad))

    def test_put_rejects_out_of_physical_range(self) -> None:
        response = self._put("TH-01", {
            "temp_min": -120.0, "temp_max": 26.0,
            "humidity_min": 40.0, "humidity_max": 60.0,
        })
        self.assertEqual(response.status_code, 400)
        response = self._put("TH-01", {
            "temp_min": 18.0, "temp_max": 26.0,
            "humidity_min": -1.0, "humidity_max": 60.0,
        })
        self.assertEqual(response.status_code, 400)

    def test_put_rejects_min_not_below_max(self) -> None:
        response = self._put("TH-01", {
            "temp_min": 26.0, "temp_max": 26.0,
            "humidity_min": 40.0, "humidity_max": 60.0,
        })
        self.assertEqual(response.status_code, 400)
        response = self._put("TH-01", {
            "temp_min": 18.0, "temp_max": 26.0,
            "humidity_min": 70.0, "humidity_max": 60.0,
        })
        self.assertEqual(response.status_code, 400)


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
