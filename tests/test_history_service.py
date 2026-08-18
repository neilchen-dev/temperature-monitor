from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import call, patch
from zoneinfo import ZoneInfo

import config
from services import history


class HistoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_values = {
            "HISTORY_API_KEY": config.HISTORY_API_KEY,
            "HISTORY_INTERVAL_MINUTES": config.HISTORY_INTERVAL_MINUTES,
            "HISTORY_TIMEZONE": config.HISTORY_TIMEZONE,
            "HISTORY_CLEANUP_ENABLED": config.HISTORY_CLEANUP_ENABLED,
            "HISTORY_RETENTION_DAYS": config.HISTORY_RETENTION_DAYS,
            "HISTORY_CLEANUP_HOUR": config.HISTORY_CLEANUP_HOUR,
            "HISTORY_TABLE_MAP": config.HISTORY_TABLE_MAP,
        }
        config.HISTORY_API_KEY = "x" * 32
        config.HISTORY_INTERVAL_MINUTES = 10
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.HISTORY_CLEANUP_ENABLED = False
        config.HISTORY_RETENTION_DAYS = 90
        config.HISTORY_CLEANUP_HOUR = 2
        config.HISTORY_TABLE_MAP = {
            device: f"tbl{index:02d}"
            for index, device in enumerate(history.EXPECTED_HISTORY_DEVICES, start=1)
        }
        history._latest_sample_cache.clear()
        history._cleanup_date_by_table.clear()

    def tearDown(self) -> None:
        for name, value in self.original_values.items():
            setattr(config, name, value)
        history._latest_sample_cache.clear()
        history._cleanup_date_by_table.clear()

    @staticmethod
    def _snapshot_records(status: str = "在线") -> list[dict]:
        records = []
        for device in history.EXPECTED_HISTORY_DEVICES:
            records.append({
                "fields": {
                    "设备编号": device,
                    "区域": f"区域-{device}",
                    "当前温度": 24.6,
                    "当前湿度": 52,
                    "在线状态": status,
                    "温度判定": "正常" if status == "在线" else "离线",
                    "湿度判定": "正常" if status == "在线" else "离线",
                    "当前工艺": ["N/A"],
                    "当前判定状态": "正常" if status == "在线" else "设备离线",
                    "当前作业状态": ["N/A"],
                    "警报状态": ["未触发"],
                }
            })
        return records

    def test_floors_all_devices_to_same_ten_minute_bucket(self) -> None:
        now = datetime(2026, 8, 18, 10, 17, 49, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(
            history.floor_sample_time(now),
            datetime(2026, 8, 18, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    def test_offline_snapshot_clears_temperature_and_humidity(self) -> None:
        sample_time = datetime(2026, 8, 18, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        fields = self._snapshot_records(status="离线")[0]["fields"]

        result = history.build_history_fields(fields, sample_time)

        self.assertIsNone(result["当前温度"])
        self.assertIsNone(result["当前湿度"])
        self.assertEqual(result["在线状态"], "离线")
        self.assertEqual(result["当前判定状态"], "设备离线")
        self.assertEqual(result["当前工艺"], "N/A")

    def test_writes_one_record_to_each_history_table(self) -> None:
        now = datetime(2026, 8, 18, 1, 7, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch.object(history, "list_realtime_snapshots", return_value=self._snapshot_records()),
            patch.object(history, "get_latest_history_timestamp", return_value=None),
            patch.object(history, "create_history_record", return_value={"code": 0}) as create,
            patch.object(history, "run_cleanup_if_due", return_value={"status": "not_due"}),
        ):
            payload, status_code = history.sample_history(now)

        self.assertEqual(status_code, 200)
        self.assertEqual(len(payload["created"]), 11)
        self.assertEqual(create.call_count, 11)
        timestamps = {
            history_call.args[1]["采集时间"] for history_call in create.call_args_list
        }
        self.assertEqual(len(timestamps), 1)

    def test_latest_online_timestamp_prevents_duplicate_after_restart(self) -> None:
        now = datetime(2026, 8, 18, 10, 7, tzinfo=ZoneInfo("Asia/Shanghai"))
        bucket_ms = int(
            datetime(2026, 8, 18, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
            * 1000
        )
        with (
            patch.object(history, "list_realtime_snapshots", return_value=self._snapshot_records()),
            patch.object(history, "get_latest_history_timestamp", return_value=bucket_ms),
            patch.object(history, "create_history_record") as create,
            patch.object(history, "run_cleanup_if_due", return_value={"status": "not_due"}),
        ):
            payload, status_code = history.sample_history(now)

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["skipped_duplicates"], list(history.EXPECTED_HISTORY_DEVICES))
        create.assert_not_called()

    def test_one_table_failure_returns_partial_without_stopping_others(self) -> None:
        now = datetime(2026, 8, 18, 1, 7, tzinfo=ZoneInfo("Asia/Shanghai"))
        create_results = [{"code": 1254291, "msg": "conflict"}] + [
            {"code": 0} for _ in range(10)
        ]
        with (
            patch.object(history, "list_realtime_snapshots", return_value=self._snapshot_records()),
            patch.object(history, "get_latest_history_timestamp", return_value=None),
            patch.object(history, "create_history_record", side_effect=create_results),
            patch.object(history, "run_cleanup_if_due", return_value={"status": "not_due"}),
        ):
            payload, status_code = history.sample_history(now)

        self.assertEqual(status_code, 207)
        self.assertIn("TH-01", payload["failures"])
        self.assertEqual(len(payload["created"]), 10)

    def test_disabled_cleanup_only_preflights_and_never_deletes(self) -> None:
        sample_time = datetime(2026, 8, 18, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch.object(
                history,
                "find_expired_history_record_ids",
                return_value=["rec_old"],
            ) as find,
            patch.object(history, "delete_history_records") as delete,
        ):
            result = history.run_cleanup_if_due(sample_time)

        self.assertEqual(result["status"], "disabled_preflight")
        self.assertEqual(find.call_count, 11)
        delete.assert_not_called()
        self.assertEqual(
            find.call_args_list[0],
            call("tbl01", "2026-05-20"),
        )

    def test_enabled_cleanup_deletes_in_batches_of_500(self) -> None:
        config.HISTORY_CLEANUP_ENABLED = True
        sample_time = datetime(2026, 8, 18, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        expired_ids = [f"rec_{index}" for index in range(501)]
        with (
            patch.object(
                history,
                "find_expired_history_record_ids",
                return_value=expired_ids,
            ),
            patch.object(
                history,
                "delete_history_records",
                return_value={"code": 0},
            ) as delete,
        ):
            result = history.run_cleanup_if_due(sample_time)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(delete.call_count, 22)
        first_table_calls = delete.call_args_list[:2]
        self.assertEqual(len(first_table_calls[0].args[1]), 500)
        self.assertEqual(len(first_table_calls[1].args[1]), 1)


if __name__ == "__main__":
    unittest.main()
