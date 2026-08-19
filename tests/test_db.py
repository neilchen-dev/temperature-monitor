from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from services import db
from services import history as history_service


class SqliteMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self._original_values = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "mirror.db"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._original_values.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def test_save_temperature_report_persists_row(self) -> None:
        db.save_temperature_report(
            device="DEV-01",
            temperature_c=24.6,
            humidity=52.0,
            status="在线",
            feishu_code=0,
            feishu_message="success",
        )

        connection = db._get_connection()
        rows = connection.execute(
            "SELECT device, temperature_c, humidity, status, feishu_code"
            " FROM temperature_reports"
        ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device"], "DEV-01")
        self.assertEqual(rows[0]["temperature_c"], 24.6)
        self.assertEqual(rows[0]["humidity"], 52.0)
        self.assertEqual(rows[0]["status"], "在线")
        self.assertEqual(rows[0]["feishu_code"], 0)

    def test_save_temperature_report_ignores_non_numeric_values(self) -> None:
        db.save_temperature_report(
            device="DEV-01",
            temperature_c="",
            humidity="",
            status="离线",
            feishu_code=0,
            feishu_message="success",
        )

        connection = db._get_connection()
        row = connection.execute(
            "SELECT temperature_c, humidity FROM temperature_reports"
        ).fetchone()

        self.assertIsNone(row["temperature_c"])
        self.assertIsNone(row["humidity"])

    def test_disabled_flag_skips_writes(self) -> None:
        config.SQLITE_ENABLED = False

        db.save_temperature_report(
            device="DEV-01",
            temperature_c=24.6,
            humidity=52.0,
            status="在线",
            feishu_code=0,
            feishu_message="success",
        )
        db.save_history_snapshot(
            "TH-01",
            datetime(2026, 8, 18, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
            {"采集时间": 1755489000000},
        )

        self.assertFalse(config.SQLITE_DB_PATH.exists())

    def test_save_history_snapshot_is_idempotent(self) -> None:
        sample_time = datetime(2026, 8, 18, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        fields = history_service.build_history_fields(
            {
                "设备编号": "TH-01",
                "区域": "区域-A",
                "当前温度": 24.6,
                "当前湿度": 52,
                "在线状态": "在线",
                "温度判定": "正常",
                "湿度判定": "正常",
                "当前工艺": "N/A",
                "当前判定状态": "仅监测",
                "当前作业状态": "N/A",
                "警报状态": "未触发",
            },
            sample_time,
        )

        db.save_history_snapshot("TH-01", sample_time, fields)
        db.save_history_snapshot("TH-01", sample_time, fields)

        rows = db.fetch_history_snapshots(device="TH-01")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["area"], "区域-A")
        self.assertEqual(row["temperature"], 24.6)
        self.assertEqual(row["humidity"], 52.0)
        self.assertEqual(row["online_status"], "在线")
        self.assertEqual(row["temp_judgment"], "正常")
        self.assertEqual(row["humidity_judgment"], "正常")
        self.assertEqual(row["overall_judgment"], "仅监测")

    def test_fetch_history_snapshots_filters_by_time_range(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        base = datetime(2026, 8, 18, 10, 0, tzinfo=timezone)
        for index in range(3):
            sample_time = base.replace(minute=10 * index)
            db.save_history_snapshot(
                "TH-01",
                sample_time,
                {"采集时间": int(sample_time.timestamp() * 1000)},
            )

        start = int(base.replace(minute=10).timestamp() * 1000)
        rows = db.fetch_history_snapshots(
            device="TH-01", start_ms=start, end_ms=start + 600_000
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_time_ms"], start)

    def test_offline_snapshot_stores_null_temperature(self) -> None:
        sample_time = datetime(2026, 8, 18, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        fields = history_service.build_history_fields(
            {"设备编号": "TH-01", "在线状态": "离线"},
            sample_time,
        )

        db.save_history_snapshot("TH-01", sample_time, fields)

        rows = db.fetch_history_snapshots(device="TH-01")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["temperature"])
        self.assertIsNone(rows[0]["humidity"])
        self.assertEqual(rows[0]["online_status"], "离线")


if __name__ == "__main__":
    unittest.main()
