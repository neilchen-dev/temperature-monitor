"""/api/shadow/summary 汇总接口测试（P1）+ schema 守卫测试（P2）。

- 汇总接口：鉴权、时间窗口聚合、by_device、devices_with_no_compare、
  OBSERVATION_ERROR 单独计数。
- schema 守卫：半旧 schema（缺列且无迁移路径）必须显式拒绝启动 Runtime，
  不得静默运行。
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask

import config
import runtime.bootstrap as bootstrap
from integrations.feishu_records import FeishuRawRecord
from runtime.bootstrap import RuntimeBootstrapError, build_runtime, shadow_summary_snapshot
from routes.api import api_bp


TEST_KEY = "unit-test-shadow-summary-key-0123456789"
TZ = ZoneInfo("Asia/Shanghai")


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


class ShadowSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._run_sequence = 0
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=TZ)
        self.original = {
            name: getattr(config, name)
            for name in (
                "AUTOMATION_MODE",
                "SHADOW_DEVICE_IDS",
                "APP_ID",
                "APP_SECRET",
                "APP_TOKEN",
                "FEISHU_DEVICE_TABLE_ID",
                "SQLITE_ENABLED",
                "HISTORY_API_KEY",
                "HISTORY_TIMEZONE",
                "AUTOMATION_RUN_RETENTION_DAYS",
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = ("TH-10", "TH-03", "TH-05")
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.HISTORY_API_KEY = TEST_KEY
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        self.addCleanup(self._restore)

        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.connection.close)
        self.components = build_runtime(
            connection=self.connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: self.now,
        )
        self.addCleanup(self._cleanup_runtime)

        app = Flask(__name__)
        app.register_blueprint(api_bp)
        self.client = app.test_client()
        self.headers = {"X-History-Key": TEST_KEY}

    def _restore(self) -> None:
        for name, value in self.original.items():
            setattr(config, name, value)
        bootstrap._last_components = None

    def _cleanup_runtime(self) -> None:
        bootstrap._last_components = None
        try:
            self.components.stop()
        except Exception:  # noqa: BLE001 - cleanup must not mask test result
            pass

    def _insert_run(
        self,
        *,
        device_id: str,
        matched: int,
        difference_type: str | None,
        created_at: datetime,
    ) -> None:
        self._run_sequence += 1
        self.connection.execute(
            """
            INSERT INTO automation_runs (
                id, device_id, sample_time, mode, action_type, action_status,
                feishu_observed_state_json, matched, difference_type,
                details_json, context_json, created_at
            ) VALUES (?, ?, ?, 'shadow', 'SHADOW_COMPARE', 'COMPARED',
                      '{}', ?, ?, '{}', '{}', ?)
            """,
            (
                f"run-{self._run_sequence}",
                device_id,
                created_at.isoformat(),
                matched,
                difference_type,
                created_at.isoformat(),
            ),
        )

    def test_summary_aggregates_within_window(self) -> None:
        recent = self.now - timedelta(hours=1)
        old = self.now - timedelta(hours=25)
        self._insert_run(device_id="TH-10", matched=1, difference_type=None, created_at=recent)
        self._insert_run(device_id="TH-10", matched=1, difference_type=None, created_at=recent)
        self._insert_run(device_id="TH-10", matched=0, difference_type="ALARM_STATE_MISMATCH", created_at=recent)
        self._insert_run(device_id="TH-03", matched=0, difference_type="OBSERVATION_ERROR", created_at=recent)
        # 窗口外的数据不计入。
        self._insert_run(device_id="TH-10", matched=1, difference_type=None, created_at=old)
        self.connection.commit()

        summary = self.components.runtime.shadow_summary(hours=24)
        self.assertEqual(summary["hours"], 24)
        self.assertEqual(summary["total_compare"], 4)
        self.assertEqual(summary["matched"], 2)
        self.assertEqual(summary["mismatch"], 1)
        self.assertEqual(summary["observation_error_count"], 1)
        self.assertEqual(summary["match_rate"], 0.5)
        self.assertEqual(
            summary["by_difference_type"],
            {"MATCH": 2, "ALARM_STATE_MISMATCH": 1, "OBSERVATION_ERROR": 1},
        )
        self.assertEqual(summary["by_device"]["TH-10"]["total"], 3)
        self.assertEqual(summary["by_device"]["TH-10"]["matched"], 2)
        self.assertEqual(summary["by_device"]["TH-10"]["mismatch"], 1)
        self.assertEqual(summary["by_device"]["TH-03"]["observation_error"], 1)
        # 白名单内但窗口内无比对的设备要显式列出（Shadow 盲区可见）。
        self.assertEqual(summary["devices_with_no_compare"], ["TH-05"])
        self.assertIsNotNone(summary["last_compare_time"])
        self.assertFalse(summary["scheduler_running"])

    def test_summary_empty_window(self) -> None:
        summary = self.components.runtime.shadow_summary(hours=24)
        self.assertEqual(summary["total_compare"], 0)
        self.assertIsNone(summary["match_rate"])
        self.assertEqual(summary["by_difference_type"], {})
        self.assertEqual(summary["devices_with_no_compare"], ["TH-03", "TH-05", "TH-10"])
        self.assertIsNone(summary["standard_sync_age_seconds"])

    def test_route_requires_auth(self) -> None:
        config.HISTORY_API_KEY = ""
        response = self.client.get("/api/shadow/summary")
        self.assertEqual(response.status_code, 503)
        config.HISTORY_API_KEY = TEST_KEY
        self.assertEqual(
            self.client.get("/api/shadow/summary").status_code, 401
        )
        self.assertEqual(
            self.client.get(
                "/api/shadow/summary", headers={"X-History-Key": "wrong"}
            ).status_code,
            401,
        )

    def test_route_returns_aggregates(self) -> None:
        recent = self.now - timedelta(hours=1)
        self._insert_run(device_id="TH-10", matched=1, difference_type=None, created_at=recent)
        self._insert_run(device_id="TH-03", matched=0, difference_type="EVENT_DUPLICATED", created_at=recent)
        self.connection.commit()

        response = self.client.get("/api/shadow/summary", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        summary = payload["summary"]
        self.assertTrue(summary["available"])
        self.assertEqual(summary["total_compare"], 2)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["mismatch"], 1)
        self.assertEqual(summary["by_difference_type"]["EVENT_DUPLICATED"], 1)
        # 不泄露 record 级 payload / 凭据。
        self.assertNotIn("app_secret", str(summary).lower())

    def test_route_hours_param_is_clamped(self) -> None:
        response = self.client.get(
            "/api/shadow/summary?hours=99999", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"]["hours"], 24 * 30)
        response = self.client.get(
            "/api/shadow/summary?hours=0", headers=self.headers
        )
        self.assertEqual(response.get_json()["summary"]["hours"], 1)

    def test_summary_snapshot_without_runtime(self) -> None:
        bootstrap._last_components = None
        snapshot = shadow_summary_snapshot(hours=24)
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["reason"], "runtime not built")
        response = self.client.get("/api/shadow/summary", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["summary"]["available"])


class SchemaGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            name: getattr(config, name)
            for name in (
                "AUTOMATION_MODE",
                "SHADOW_DEVICE_IDS",
                "APP_ID",
                "APP_SECRET",
                "APP_TOKEN",
                "FEISHU_DEVICE_TABLE_ID",
                "SQLITE_ENABLED",
                "HISTORY_TIMEZONE",
                "AUTOMATION_RUN_RETENTION_DAYS",
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = ("TH-10",)
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self.original.items():
            setattr(config, name, value)
        bootstrap._last_components = None

    def test_fresh_schema_passes(self) -> None:
        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_NoDeviceSource(),
            now_provider=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=TZ),
        )
        try:
            self.assertTrue(components.status()["available"])
        finally:
            components.stop()
            bootstrap._last_components = None

    def test_partial_schema_blocks_runtime_startup(self) -> None:
        """半旧 schema（缺列且无迁移路径）必须显式拒绝启动。"""
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(connection.close)
        # environment_events 缺 payload_json（无迁移路径）。
        connection.execute(
            """
            CREATE TABLE environment_events (
                event_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                event_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                created_at TEXT
            )
            """
        )
        with self.assertRaises(RuntimeBootstrapError) as ctx:
            build_runtime(
                connection=connection,
                record_source=_NoDeviceSource(),
                now_provider=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=TZ),
            )
        self.assertIn("environment_events.payload_json", str(ctx.exception))

    def test_partial_automation_runs_blocks_runtime_startup(self) -> None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(connection.close)
        # 旧版 automation_runs：索引覆盖的列都在，仅缺后来才加的 error 列
        # ——仓储构造（含建索引）成功，但 schema 校验必须拒绝启动。
        connection.execute(
            """
            CREATE TABLE automation_runs (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                sample_time TEXT,
                mode TEXT NOT NULL,
                action_type TEXT NOT NULL,
                action_status TEXT NOT NULL,
                alarm_id TEXT,
                planned_run_at TEXT,
                python_monitor_result_json TEXT,
                python_alarm_transition_json TEXT,
                feishu_observed_state_json TEXT,
                matched INTEGER,
                difference_type TEXT,
                details_json TEXT,
                context_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        with self.assertRaises(RuntimeBootstrapError) as ctx:
            build_runtime(
                connection=connection,
                record_source=_NoDeviceSource(),
                now_provider=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=TZ),
            )
        self.assertIn("automation_runs.error", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
