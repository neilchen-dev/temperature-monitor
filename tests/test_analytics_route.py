from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from app import create_app
from services import db


class AnalyticsRouteTests(unittest.TestCase):
    HEADERS = {"X-History-Key": "k" * 32}

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self._original_values = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "HISTORY_API_KEY": config.HISTORY_API_KEY,
            "HISTORY_INTERVAL_MINUTES": config.HISTORY_INTERVAL_MINUTES,
            "HISTORY_TIMEZONE": config.HISTORY_TIMEZONE,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "analytics.db"
        config.HISTORY_API_KEY = "k" * 32
        config.HISTORY_INTERVAL_MINUTES = 10
        config.HISTORY_TIMEZONE = "Asia/Shanghai"

        self.client = create_app().test_client()
        self._seed_data()

    def tearDown(self) -> None:
        for name, value in self._original_values.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def _seed_data(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        base = datetime(2026, 8, 18, 10, 0, tzinfo=timezone)

        def snapshot(device: str, minute: int, temperature, humidity,
                     online: str = "在线", temp_judgment: str = "正常"):
            sample_time = base.replace(minute=minute)
            fields = {
                "设备编号": device,
                "区域": "区域-A",
                "当前温度": temperature,
                "当前湿度": humidity,
                "在线状态": online,
                "温度判定": temp_judgment if online == "在线" else "离线",
                "湿度判定": "正常",
                "当前工艺": "N/A",
                "当前判定状态": "正常",
                "当前作业状态": "N/A",
                "警报状态": "未触发",
            }
            db.save_history_snapshot(device, sample_time, fields)

        snapshot("TH-01", 0, 24.0, 50.0)
        snapshot("TH-01", 10, 26.0, 60.0, temp_judgment="超上限")
        snapshot("TH-01", 20, None, None, online="离线")
        snapshot("TH-02", 0, 22.0, 40.0)
        db.save_temperature_report(
            device="TH-01", temperature_c=24.0, humidity=50.0,
            status="在线", feishu_code=0, feishu_message="success",
        )

    def test_query_requires_auth(self) -> None:
        response = self.client.get("/history/query")

        self.assertEqual(response.status_code, 401)

    def test_query_returns_configured_key_error_when_missing(self) -> None:
        config.HISTORY_API_KEY = ""

        response = self.client.get("/history/query", headers=self.HEADERS)

        self.assertEqual(response.status_code, 503)

    def test_query_filters_by_device(self) -> None:
        response = self.client.get(
            "/history/query?device=th-01",
            headers=self.HEADERS,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["count"], 3)
        self.assertTrue(all(item["device"] == "TH-01" for item in payload["items"]))

    def test_query_filters_by_time_range(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        start = int(datetime(2026, 8, 18, 10, 0, tzinfo=timezone).timestamp() * 1000)
        end = start + 20 * 60_000  # covers the 10:00 and 10:10 buckets

        response = self.client.get(
            f"/history/query?start={start}&end={end}",
            headers=self.HEADERS,
        )

        payload = response.get_json()
        self.assertEqual(payload["count"], 3)  # TH-01 x2 + TH-02 x1

    def test_query_rejects_invalid_time(self) -> None:
        response = self.client.get(
            "/history/query?start=not-a-time",
            headers=self.HEADERS,
        )

        self.assertEqual(response.status_code, 400)

    def test_daily_stats_aggregates_and_excludes_offline_from_abnormal(self) -> None:
        response = self.client.get(
            "/history/stats/daily?device=TH-01",
            headers=self.HEADERS,
        )

        self.assertEqual(response.status_code, 200)
        items = response.get_json()["items"]
        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row["local_date"], "2026-08-18")
        self.assertEqual(row["sample_count"], 3)
        self.assertAlmostEqual(row["avg_temperature"], 25.0)
        self.assertEqual(row["min_temperature"], 24.0)
        self.assertEqual(row["max_temperature"], 26.0)
        # The offline sample (NULL temperature) must not count as abnormal.
        self.assertEqual(row["temp_abnormal_count"], 1)
        self.assertEqual(row["offline_count"], 1)

    def test_daily_stats_accepts_iso_time_params(self) -> None:
        response = self.client.get(
            "/history/stats/daily"
            "?start=2026-08-18T00:00:00%2B08:00&end=2026-08-19T00:00:00%2B08:00",
            headers=self.HEADERS,
        )

        payload = response.get_json()
        self.assertEqual(payload["count"], 2)  # TH-01 and TH-02

    def test_device_stats_reports_last_snapshot_and_offline_estimate(self) -> None:
        response = self.client.get(
            "/history/stats/devices",
            headers=self.HEADERS,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["interval_minutes"], 10)
        devices = {item["device"]: item for item in payload["items"]}

        th01 = devices["TH-01"]
        # Last snapshot is the 10:20 offline bucket.
        self.assertIsNone(th01["last_temperature"])
        self.assertEqual(th01["last_online_status"], "离线")
        self.assertEqual(th01["snapshot_count"], 3)
        self.assertEqual(th01["estimated_offline_duration_sec"], 600)
        self.assertEqual(th01["report_count"], 1)

        th02 = devices["TH-02"]
        self.assertEqual(th02["last_temperature"], 22.0)
        self.assertEqual(th02["last_online_status"], "在线")

    def test_mirror_disabled_returns_503(self) -> None:
        db.close()
        db._init_failed = True

        response = self.client.get(
            "/history/query",
            headers=self.HEADERS,
        )

        self.assertEqual(response.status_code, 503)

    def test_dashboard_requires_key(self) -> None:
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 401)

    def test_dashboard_renders_with_valid_key(self) -> None:
        response = self.client.get(
            "/dashboard?key=" + "k" * 32 + "&days=7",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        body = response.get_data(as_text=True)
        self.assertIn("本地分析看板", body)
        self.assertIn("TH-01", body)
        self.assertIn("估算离线时长", body)
        # Embedded JSON must have ``<`` escaped so it cannot close the
        # script tag early.
        self.assertNotIn("</script>\"}", body)

    def test_health_includes_sqlite_stats(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["sqlite"]["enabled"])
        self.assertEqual(payload["sqlite"]["write_failures"], 0)
        self.assertEqual(payload["sqlite"]["history_snapshot_count"], 4)


if __name__ == "__main__":
    unittest.main()
