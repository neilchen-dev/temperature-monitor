from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from app import create_app
from services import db, devices


TEST_KEY = "unit-test-secret-key-0123456789"


class ApiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "HISTORY_API_KEY": config.HISTORY_API_KEY,
            "EVENT_TEMPERATURE_HIGH_C": config.EVENT_TEMPERATURE_HIGH_C,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "api.db"
        config.HISTORY_API_KEY = ""
        config.EVENT_TEMPERATURE_HIGH_C = 30.0
        self.addCleanup(self._restore)

        devices.record_sample("TH-01", "home_assistant", 24.6, 52.0, "online", 1755000000000)
        devices.record_sample("PLC-01", "modbus", 31.2, 48.1, "online", 1755000001000)

        # create_app() 不启动采集线程；collector 状态默认为未启用。
        self.client = create_app().test_client()
        self.headers = {"X-History-Key": TEST_KEY}

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def _set_key(self) -> None:
        config.HISTORY_API_KEY = TEST_KEY

    def test_unconfigured_key_returns_503_for_all_read_endpoints(self) -> None:
        # 与 analytics 接口一致：未配置密钥必须 503，绝不未鉴权放行；
        # 升级到本版本的既有部署不会因此新增公开数据面。
        self.assertEqual(self.client.get("/api/devices").status_code, 503)
        self.assertEqual(self.client.get("/api/events").status_code, 503)
        self.assertEqual(self.client.get("/api/devices/PLC-01").status_code, 503)
        # system status 与 /health 同类，保持开放
        self.assertEqual(self.client.get("/api/system/status").status_code, 200)

    def test_mirror_disabled_returns_503_not_empty_list(self) -> None:
        # SQLite 故障必须显式 503，不能伪装成"设备不存在"
        self._set_key()
        with patch("routes.api.db.is_enabled", return_value=False):
            self.assertEqual(
                self.client.get("/api/devices", headers=self.headers).status_code,
                503,
            )
            self.assertEqual(
                self.client.get("/api/events", headers=self.headers).status_code,
                503,
            )
            self.assertEqual(
                self.client.get(
                    "/api/devices/PLC-01", headers=self.headers
                ).status_code,
                503,
            )

    def test_devices_auth_and_payload(self) -> None:
        self._set_key()
        self.assertEqual(self.client.get("/api/devices").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/devices", headers={"X-History-Key": "wrong"}
            ).status_code,
            401,
        )
        response = self.client.get("/api/devices", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 2)
        sources = {item["device_id"]: item["source"] for item in payload["items"]}
        self.assertEqual(sources["TH-01"], "home_assistant")
        self.assertEqual(sources["PLC-01"], "modbus")
        plc = next(i for i in payload["items"] if i["device_id"] == "PLC-01")
        self.assertEqual(plc["status"], "online")
        # Bearer 头同样可用
        bearer = self.client.get(
            "/api/devices", headers={"Authorization": f"Bearer {TEST_KEY}"}
        )
        self.assertEqual(bearer.status_code, 200)

    def test_device_detail_and_unknown_404(self) -> None:
        self._set_key()
        response = self.client.get("/api/devices/plc-01", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["device"]["device_id"], "PLC-01")
        self.assertEqual(payload["device"]["temperature"], 31.2)
        self.assertGreaterEqual(payload["sample_count"], 1)

        self.assertEqual(
            self.client.get("/api/devices/NOPE-99", headers=self.headers).status_code,
            404,
        )

    def test_device_detail_requires_source_when_multiple(self) -> None:
        # reviewer 复现场景：TH-01 双来源，HA 在线(较早) + modbus 离线(较新)。
        # 不带 source 不得静默任取一条；带 source 后最新状态与样本都按源过滤。
        self._set_key()
        devices.record_sample(
            "TH-01", "modbus", None, None, "offline", 1755000002000
        )

        ambiguous = self.client.get("/api/devices/TH-01", headers=self.headers)
        self.assertEqual(ambiguous.status_code, 400)
        body = ambiguous.get_json()
        self.assertEqual(sorted(body["sources"]), ["home_assistant", "modbus"])

        modbus_detail = self.client.get(
            "/api/devices/TH-01?source=modbus", headers=self.headers
        )
        self.assertEqual(modbus_detail.status_code, 200)
        payload = modbus_detail.get_json()
        self.assertEqual(payload["device"]["source"], "modbus")
        self.assertEqual(payload["device"]["status"], "offline")
        self.assertGreaterEqual(payload["sample_count"], 1)
        self.assertTrue(
            all(s["source"] == "modbus" for s in payload["samples"])
        )

        ha_detail = self.client.get(
            "/api/devices/TH-01?source=home_assistant", headers=self.headers
        )
        self.assertEqual(ha_detail.status_code, 200)
        payload = ha_detail.get_json()
        self.assertEqual(payload["device"]["status"], "online")
        self.assertTrue(
            all(s["source"] == "home_assistant" for s in payload["samples"])
        )

        missing = self.client.get(
            "/api/devices/TH-01?source=opcua", headers=self.headers
        )
        self.assertEqual(missing.status_code, 404)

        # 单来源设备仍可省略 source 参数
        single = self.client.get("/api/devices/PLC-01", headers=self.headers)
        self.assertEqual(single.status_code, 200)

    def test_events_list_and_filter(self) -> None:
        self._set_key()
        # TH-01 baseline(1755000000000) -> 越限(1755000002000)
        devices.record_sample(
            "TH-01", "home_assistant", 31.0, 52.0, "online", 1755000002000
        )

        response = self.client.get("/api/events", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        event = payload["items"][0]
        self.assertEqual(event["device_id"], "TH-01")
        self.assertEqual(event["source"], "home_assistant")
        self.assertEqual(event["new_state"], "TEMPERATURE_HIGH")

        filtered = self.client.get(
            "/api/events?device=PLC-01", headers=self.headers
        ).get_json()
        self.assertEqual(filtered["count"], 0)

    def test_system_status_shape_and_no_sensitive_fields(self) -> None:
        with patch("routes.api.get_collector_status") as status:
            status.return_value = {
                "modbus": {"enabled": True, "running": False,
                           "last_success": None, "last_error_summary": None}
            }
            response = self.client.get("/api/system/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        # setUp 播种了两个设备、两个身份：device_count 按设备编号去重，
        # identity_count 与 /api/devices 的 count 语义一致
        self.assertEqual(payload["device_count"], 2)
        self.assertEqual(payload["identity_count"], 2)
        self.assertIsNotNone(payload["last_sample_time_ms"])
        self.assertIn("sqlite", payload)
        self.assertIn("collectors", payload)
        modbus = payload["collectors"]["modbus"]
        # 健康摘要不得泄漏端点/端口/密钥等配置细节
        self.assertNotIn("host", modbus)
        self.assertNotIn("port", modbus)

    def test_system_status_counts_diverge_for_multi_source_device(self) -> None:
        # 同一设备双来源时：device_count=1（设备编号），identity_count=2（身份对）
        devices.record_sample(
            "TH-01", "modbus", 25.0, 50.0, "online", 1755000002000
        )
        payload = self.client.get("/api/system/status").get_json()
        self.assertEqual(payload["device_count"], 2)  # TH-01 + PLC-01
        self.assertEqual(payload["identity_count"], 3)


if __name__ == "__main__":
    unittest.main()
