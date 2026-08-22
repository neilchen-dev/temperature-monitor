from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import config
from services import feishu


class RecordDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        feishu._record_id_cache.clear()
        feishu._record_not_found_until.clear()
        config.APP_TOKEN = "app_token"
        config.TABLE_ID = "table_id"
        config.DEVICE_ID_FIELD = "设备编号"
        config.REQUEST_RETRY_TIMES = 3
        config.REQUEST_RETRY_BACKOFF_SECONDS = 0

    def test_discovers_and_caches_record_id(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "items": [
                    {
                        "record_id": "rec_discovered",
                        "fields": {"设备编号": "DEV-01"},
                    }
                ],
                "has_more": False,
            },
        }

        with (
            patch.object(feishu, "get_token", return_value="token"),
            patch.object(feishu, "request_with_retry", return_value=response) as request,
        ):
            self.assertEqual(feishu.resolve_record_id("dev-01"), "rec_discovered")
            self.assertEqual(feishu.resolve_record_id("DEV-01"), "rec_discovered")

        self.assertEqual(request.call_count, 1)

    def test_not_found_is_negative_cached_to_avoid_full_scans(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0, "data": {"items": [], "has_more": False}}

        with (
            patch.object(feishu, "get_token", return_value="token"),
            patch.object(feishu, "request_with_retry", return_value=response) as request,
        ):
            with self.assertRaisesRegex(RuntimeError, "未在飞书表中找到设备 DEV-99"):
                feishu.resolve_record_id("DEV-99")
            with self.assertRaisesRegex(RuntimeError, "负缓存生效中"):
                feishu.resolve_record_id("DEV-99")

        self.assertEqual(request.call_count, 1)

    def test_normalizes_formula_display_value(self) -> None:
        formula_value = {
            "type": 1,
            "value": [{"text": "正常", "type": "text"}],
        }

        self.assertEqual(feishu.normalize_field_value(formula_value), "正常")

    def test_normalizes_formula_current_status_display_value(self) -> None:
        formula_value = {
            "type": 1,
            "value": [{"text": "仅监测", "type": "text"}],
        }

        self.assertEqual(feishu.normalize_field_value(formula_value), "仅监测")

    def test_normalizes_plain_text_array(self) -> None:
        self.assertEqual(
            feishu.normalize_field_value([{"text": "TH-04", "type": "text"}]),
            "TH-04",
        )

    def test_normalizes_number_and_empty_value_compatibly(self) -> None:
        self.assertEqual(feishu.normalize_field_value(27.1), "27.1")
        self.assertEqual(feishu.normalize_field_value(None), "")

    def test_manual_mapping_bypasses_lookup(self) -> None:
        with patch.object(feishu, "request_with_retry") as request:
            self.assertEqual(
                feishu.resolve_record_id("DEV-01", "rec_manual"), "rec_manual"
            )
        request.assert_not_called()

    def test_rejects_duplicate_device_identifiers(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "items": [
                    {"record_id": "rec_a", "fields": {"设备编号": "DEV-01"}},
                    {"record_id": "rec_b", "fields": {"设备编号": "DEV-01"}},
                ],
                "has_more": False,
            },
        }

        with (
            patch.object(feishu, "get_token", return_value="token"),
            patch.object(feishu, "request_with_retry", return_value=response),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"设备编号 DEV-01 重复.*rec_a, rec_b"
            ):
                feishu.resolve_record_id("DEV-01")

    def test_retries_bitable_write_conflict(self) -> None:
        conflict = Mock(status_code=200)
        conflict.json.return_value = {"code": 1254291, "msg": "write conflict"}
        success = Mock(status_code=200)
        success.json.return_value = {"code": 0, "data": {}}

        with (
            patch.object(feishu, "get_token", return_value="token"),
            patch.object(
                feishu,
                "request_with_retry",
                side_effect=[conflict, success],
            ) as request,
            patch.object(feishu.time, "sleep"),
        ):
            result = feishu.create_history_record(
                "tbl_history",
                {"设备编号": "TH-01"},
            )

        self.assertEqual(result["code"], 0)
        self.assertEqual(request.call_count, 2)

    def test_reads_latest_history_timestamp_with_descending_sort(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "items": [{"fields": {"采集时间": 1787018400000}}],
            },
        }

        with (
            patch.object(feishu, "get_token", return_value="token"),
            patch.object(feishu, "request_with_retry", return_value=response) as request,
        ):
            timestamp = feishu.get_latest_history_timestamp("tbl_history")

        self.assertEqual(timestamp, 1787018400000)
        body = request.call_args.kwargs["json_data"]
        self.assertEqual(
            body["sort"],
            [{"field_name": "采集时间", "desc": True}],
        )

    def test_paginates_cloud_filtered_expired_records(self) -> None:
        first = Mock(status_code=200)
        first.json.return_value = {
            "code": 0,
            "data": {
                "items": [{"record_id": "rec_old_1"}],
                "has_more": True,
                "page_token": "next-page",
            },
        }
        second = Mock(status_code=200)
        second.json.return_value = {
            "code": 0,
            "data": {
                "items": [{"record_id": "rec_old_2"}],
                "has_more": False,
            },
        }

        with (
            patch.object(feishu, "get_token", return_value="token"),
            patch.object(
                feishu,
                "request_with_retry",
                side_effect=[first, second],
            ) as request,
        ):
            record_ids = feishu.find_expired_history_record_ids(
                "tbl_history",
                1779206400000,
            )

        self.assertEqual(record_ids, ["rec_old_1", "rec_old_2"])
        self.assertIn("page_token=next-page", request.call_args_list[1].args[1])
        condition = request.call_args_list[0].kwargs["json_data"]["filter"][
            "conditions"
        ][0]
        self.assertEqual(condition["operator"], "isLess")
        self.assertEqual(condition["value"], ["ExactDate", "1779206400000"])


if __name__ == "__main__":
    unittest.main()
