from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
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

    def test_addon_environment_defaults_data_dir_to_persistent_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            options_file = Path(tmp_dir) / "options.json"
            options_file.write_text("{}", encoding="utf-8")
            try:
                with patch.dict(
                    os.environ,
                    {
                        "HASSIO_OPTIONS_PATH": str(options_file),
                        "DATA_DIR": "",
                    },
                    clear=False,
                ):
                    reloaded_config = importlib.reload(config)

                self.assertTrue(reloaded_config.IS_HOME_ASSISTANT_ADDON)
                self.assertEqual(reloaded_config.DATA_DIR, Path("/data"))
            finally:
                # Reload again with a non-Add-on environment so later tests
                # keep seeing the normal container defaults.
                with patch.dict(
                    os.environ,
                    {"HASSIO_OPTIONS_PATH": str(Path(tmp_dir) / "missing.json")},
                    clear=False,
                ):
                    importlib.reload(config)

    def test_non_addon_environment_defaults_data_dir_to_app_data(self) -> None:
        with patch.dict(
            os.environ,
            {"HASSIO_OPTIONS_PATH": str(Path("Z:/definitely/not/here/options.json"))},
            clear=False,
        ):
            reloaded_config = importlib.reload(config)

        self.assertFalse(reloaded_config.IS_HOME_ASSISTANT_ADDON)
        self.assertEqual(reloaded_config.DATA_DIR, config.BASE_DIR / "data")


if __name__ == "__main__":
    unittest.main()
