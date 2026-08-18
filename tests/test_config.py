from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

import config


class DeviceConfigurationTests(unittest.TestCase):
    def test_default_source_temperature_unit_is_celsius(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            reloaded_config = importlib.reload(config)

        self.assertEqual(reloaded_config.SOURCE_TEMPERATURE_UNIT, "C")

    def test_device_name_map_normalizes_source_and_target(self) -> None:
        with patch.dict(
            os.environ,
            {"DEVICE_NAME_MAP": '{"sensor.warehouse_temp":"dev-01"}'},
            clear=False,
        ):
            reloaded_config = importlib.reload(config)

        self.assertEqual(
            reloaded_config.DEVICE_NAME_MAP,
            {"SENSOR.WAREHOUSE_TEMP": "DEV-01"},
        )

    def test_empty_record_map_enables_automatic_discovery(self) -> None:
        with patch.dict(os.environ, {"DEVICE_RECORD_MAP": ""}, clear=False):
            reloaded_config = importlib.reload(config)

        self.assertEqual(reloaded_config.DEVICES, {})

    def test_history_table_map_normalizes_device_names(self) -> None:
        with patch.dict(
            os.environ,
            {"HISTORY_TABLE_MAP": '{"th-01":"tbl_history_01"}'},
            clear=False,
        ):
            reloaded_config = importlib.reload(config)

        self.assertEqual(
            reloaded_config.HISTORY_TABLE_MAP,
            {"TH-01": "tbl_history_01"},
        )


if __name__ == "__main__":
    unittest.main()
