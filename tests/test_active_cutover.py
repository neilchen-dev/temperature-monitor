"""Active 切换三开关保护测试（P1）。

AUTOMATION_MODE=active + FEISHU_WRITE_ENABLED=true +
ACTIVE_CUTOVER_ACK=I_HAVE_DISABLED_LEGACY_FEISHU_WORKFLOWS 必须同时满足；该确认
只覆盖 ACTIVE_DEVICE_IDS 中设备的 legacy owner，Canary 阶段不要求关闭其他设备工作流；
缺任何一项 Runtime 降级为 disabled，且 /api/system/status 的 runtime 段
给出 active_block_reason。
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import runtime.bootstrap as bootstrap
from integrations.feishu_records import FeishuRawRecord
from runtime.bootstrap import build_runtime, runtime_status


class _NoDeviceSource:
    def __init__(self) -> None:
        business_timezone = ZoneInfo(config.HISTORY_TIMEZONE)
        self.records = {
            config.FEISHU_STANDARD_TABLE_ID: (
                FeishuRawRecord(
                    record_id="standard-10",
                    fields={
                        "标准编号": "ENV-TH-10",
                        "版本": "Rev.A",
                        "适用区域": "PE仓库",
                        "适用设备": "TH-10",
                        "适用作业类型": None,
                        "温度下限（°C）": 20,
                        "温度上限（°C）": 26,
                        "湿度下限（%RH）": 40,
                        "湿度上限（%RH）": 60,
                        "生效时间": "2026-01-01T00:00:00",
                        "失效时间": None,
                        "优先级": 1,
                        "是否启用": True,
                        "来源文件": "SOP-001",
                        "条款": "5.2.3",
                    },
                    created_at=datetime(2026, 1, 1, tzinfo=business_timezone),
                    updated_at=datetime(2026, 1, 1, tzinfo=business_timezone),
                ),
            ),
            config.FEISHU_OPERATION_TABLE_ID: (),
            config.FEISHU_EVENT_TABLE_ID: (),
            "device-table": (),
        }

    def read_records(self, table_id: str):
        return self.records.get(table_id, ())


class ActiveCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            name: getattr(config, name)
            for name in (
                "AUTOMATION_MODE",
                "SHADOW_DEVICE_IDS",
                "ACTIVE_DEVICE_IDS",
                "APP_ID",
                "APP_SECRET",
                "APP_TOKEN",
                "FEISHU_DEVICE_TABLE_ID",
                "SQLITE_ENABLED",
                "FEISHU_WRITE_ENABLED",
                "ACTIVE_CUTOVER_ACK",
                "HISTORY_TIMEZONE",
                "AUTOMATION_RUN_RETENTION_DAYS",
            )
        }
        config.AUTOMATION_MODE = "active"
        config.SHADOW_DEVICE_IDS = ("TH-10",)
        config.ACTIVE_DEVICE_IDS = ("TH-10",)
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.FEISHU_WRITE_ENABLED = False
        config.ACTIVE_CUTOVER_ACK = ""
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self.original.items():
            setattr(config, name, value)
        bootstrap._last_components = None

    def _build(self):
        return build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_NoDeviceSource(),
            now_provider=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    def test_active_without_write_and_ack_is_disabled(self) -> None:
        components = self._build()
        try:
            self.assertFalse(components.status()["available"])
            self.assertEqual(
                components.runtime.monitor_service.action_executor.mode.value,
                "disabled",
            )
            self.assertIn("FEISHU_WRITE_ENABLED=true", components.status()["reason"])
        finally:
            components.stop()

    def test_active_with_write_but_wrong_ack_is_disabled(self) -> None:
        config.FEISHU_WRITE_ENABLED = True
        config.ACTIVE_CUTOVER_ACK = "yes-i-promise"  # 不匹配的确认串
        components = self._build()
        try:
            self.assertFalse(components.status()["available"])
            self.assertEqual(
                components.runtime.monitor_service.action_executor.mode.value,
                "disabled",
            )
            reason = components.status()["reason"]
            self.assertIn("ACTIVE_CUTOVER_ACK", reason)
            self.assertIn(
                config.ACTIVE_CUTOVER_ACK_EXPECTED,
                reason,
            )
        finally:
            components.stop()

    def test_active_with_all_three_switches_is_active(self) -> None:
        config.FEISHU_WRITE_ENABLED = True
        config.ACTIVE_CUTOVER_ACK = config.ACTIVE_CUTOVER_ACK_EXPECTED
        components = self._build()
        try:
            self.assertTrue(components.status()["available"])
            self.assertEqual(
                components.runtime.monitor_service.action_executor.mode.value,
                "active",
            )
            self.assertTrue(components.status()["feishu_write_enabled"])
            status = runtime_status()
            self.assertNotIn("active_block_reason", status)
        finally:
            components.stop()
            bootstrap._last_components = None

    def test_shadow_mode_ignores_ack_requirement(self) -> None:
        """Shadow 模式不要求 ACK：确认串只约束 Active 写回。"""
        config.AUTOMATION_MODE = "shadow"
        config.ACTIVE_CUTOVER_ACK = ""
        components = self._build()
        try:
            self.assertTrue(components.status()["available"])
            self.assertEqual(components.status()["mode"], "shadow")
        finally:
            components.stop()
            bootstrap._last_components = None

    def test_runtime_status_exposes_active_block_reason(self) -> None:
        config.FEISHU_WRITE_ENABLED = True
        config.ACTIVE_CUTOVER_ACK = ""
        components = self._build()
        try:
            status = runtime_status()
            self.assertIn("active_block_reason", status)
            self.assertIn("ACTIVE_CUTOVER_ACK", status["active_block_reason"])
            self.assertIn(
                "legacy owner for ACTIVE_DEVICE_IDS must be disabled or excluded",
                status["active_block_reason"],
            )
        finally:
            components.stop()
            bootstrap._last_components = None


if __name__ == "__main__":
    unittest.main()
