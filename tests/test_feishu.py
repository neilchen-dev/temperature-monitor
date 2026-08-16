from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import config
from services import feishu


class RecordDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        feishu._record_id_cache.clear()
        config.APP_TOKEN = "app_token"
        config.TABLE_ID = "table_id"
        config.DEVICE_ID_FIELD = "设备编号"

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

    def test_manual_mapping_bypasses_lookup(self) -> None:
        with patch.object(feishu, "request_with_retry") as request:
            self.assertEqual(
                feishu.resolve_record_id("DEV-01", "rec_manual"), "rec_manual"
            )
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
