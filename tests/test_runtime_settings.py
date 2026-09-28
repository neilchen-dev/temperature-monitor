"""Runtime settings (console-editable hot parameters) contract tests.

Covers the full controlled-settings surface:

1.  default fallback (and dynamic config.py fallback, no import caching)
2.  SQLite override wins over config
3.  hot update takes effect immediately on the next read
4.  invalid floats rejected (string / bool / list / NaN / inf)
5.  out-of-range numbers rejected
6.  invalid booleans rejected (1 / 0 / "true" / ...)
7.  unknown / high-risk / secret keys rejected (whitelist only)
8.  unconfigured HISTORY_API_KEY stays fail-closed (503)
9.  wrong key returns 401
10. audit rows generated for every change
11. DB write failure rolls back without partial state
12. /api/thresholds stays read-only (PUT still 409)
13. Feishu validated standards never affected by settings changes
14. secrets never appear in the settings API
15. overrides survive a mirror re-open (restart persistence)
16. lock-conflict retry writes exactly one audit row per key
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from app import create_app
from application.action_executor import ActionExecutor
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from domain.alarm_state_machine import AlarmStateMachine
from domain.models import (
    ControlType,
    DeviceContext,
    EnvironmentStandard,
    MonitorSample,
)
from domain.standard_resolver import StaticStandardResolver
from repositories.runtime_state import SQLiteAlarmStateRepository
from services import db, runtime_settings

TEST_KEY = "unit-test-secret-key-0123456789"

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)

WHITELIST = {
    "temperature_prewarning_margin_c",
    "humidity_prewarning_margin_rh",
    "temperature_prewarning_exit_margin_c",
    "humidity_prewarning_exit_margin_rh",
    "feishu_alarm_notify_enabled",
    "feishu_recovery_notify_enabled",
    "feishu_prewarning_notify_enabled",
    "feishu_prewarning_recovery_notify_enabled",
}


class _MirrorIsolatedTestCase(unittest.TestCase):
    """Isolate the shared SQLite mirror on a fresh temp database."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "HISTORY_API_KEY": config.HISTORY_API_KEY,
            "APP_SECRET": config.APP_SECRET,
        }
        for definition in runtime_settings.SETTING_DEFINITIONS.values():
            self._original[definition.config_attr] = getattr(
                config, definition.config_attr
            )
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "settings.db"
        config.HISTORY_API_KEY = TEST_KEY
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()


class RuntimeSettingsServiceTests(_MirrorIsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        db.init_db()

    # 1. 默认值 fallback（含动态 config 读取与镜像禁用回退）
    def test_default_fallback_and_dynamic_config(self) -> None:
        self.assertEqual(
            runtime_settings.get_setting("temperature_prewarning_margin_c"),
            config.TEMPERATURE_PREWARNING_MARGIN_C,
        )
        # fallback 是运行时动态读取 config，不缓存 —— 既有 patch.config 的
        # 测试与部署语义保持不变
        with patch.object(config, "TEMPERATURE_PREWARNING_MARGIN_C", 0.75):
            self.assertEqual(
                runtime_settings.get_setting("temperature_prewarning_margin_c"),
                0.75,
            )
        # 镜像禁用时回退默认值，不抛异常、不影响主链路
        config.SQLITE_ENABLED = False
        try:
            self.assertEqual(
                runtime_settings.get_setting("feishu_alarm_notify_enabled"),
                config.FEISHU_ALARM_NOTIFY_ENABLED,
            )
            self.assertEqual(
                runtime_settings.prewarning_margins()["temperature"],
                config.TEMPERATURE_PREWARNING_MARGIN_C,
            )
        finally:
            config.SQLITE_ENABLED = True

    # 2. SQLite override 优先于 config
    def test_sqlite_override_wins_over_config(self) -> None:
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.5}, reason="试运行调整"
        )
        with patch.object(config, "TEMPERATURE_PREWARNING_MARGIN_C", 0.9):
            self.assertEqual(
                runtime_settings.get_setting("temperature_prewarning_margin_c"),
                0.5,
            )
        overview = runtime_settings.settings_overview()
        self.assertEqual(
            overview["temperature_prewarning_margin_c"]["source"], "override"
        )

    # 3. 热更新立即生效（下一次读取即为新值，无需重启）
    def test_hot_update_immediate(self) -> None:
        self.assertEqual(
            runtime_settings.prewarning_margins()["temperature"],
            config.TEMPERATURE_PREWARNING_MARGIN_C,
        )
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.5}, reason="提高灵敏度"
        )
        self.assertEqual(
            runtime_settings.prewarning_margins()["temperature"], 0.5
        )
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.8}, reason="再次调整"
        )
        self.assertEqual(
            runtime_settings.prewarning_margins()["temperature"], 0.8
        )

    def test_notify_flags_hot_update(self) -> None:
        flags = runtime_settings.feishu_notify_flags()
        self.assertEqual(
            flags,
            {
                "alarm": config.FEISHU_ALARM_NOTIFY_ENABLED,
                "recovery": config.FEISHU_RECOVERY_NOTIFY_ENABLED,
                "prewarning": config.FEISHU_PREWARNING_NOTIFY_ENABLED,
                "prewarning_recovery": (
                    config.FEISHU_PREWARNING_RECOVERY_NOTIFY_ENABLED
                ),
            },
        )
        runtime_settings.apply_changes(
            {"feishu_alarm_notify_enabled": True}, reason="开启报警通知"
        )
        self.assertTrue(runtime_settings.feishu_notify_flags()["alarm"])

    # 4. 非法 float
    def test_invalid_float_rejected(self) -> None:
        for bad in ("abc", "0.5", True, None, [0.5], {"v": 0.5}):
            with self.assertRaises(runtime_settings.SettingsValidationError):
                runtime_settings.apply_changes(
                    {"humidity_prewarning_margin_rh": bad}, reason="非法输入"
                )

    # 5. 超范围数值
    def test_out_of_range_rejected(self) -> None:
        for bad in (5.1, -0.01, 100):
            with self.assertRaises(runtime_settings.SettingsValidationError):
                runtime_settings.apply_changes(
                    {"temperature_prewarning_margin_c": bad}, reason="超范围"
                )
        self.assertEqual(
            runtime_settings.get_setting("temperature_prewarning_margin_c"),
            config.TEMPERATURE_PREWARNING_MARGIN_C,
        )

    # 6. 非法 boolean（必须严格 true/false）
    def test_invalid_bool_rejected(self) -> None:
        for bad in (1, 0, "true", "yes", "on", None, 1.0, []):
            with self.assertRaises(runtime_settings.SettingsValidationError):
                runtime_settings.apply_changes(
                    {"feishu_alarm_notify_enabled": bad}, reason="非法布尔"
                )

    # 7. 未知 / 高风险 / secret key 一律拒绝
    def test_unknown_and_forbidden_keys_rejected(self) -> None:
        for key in (
            "HISTORY_API_KEY",
            "AUTOMATION_MODE",
            "FEISHU_WRITE_ENABLED",
            "ACTIVE_DEVICE_IDS",
            "ACTIVE_CUTOVER_ACK",
            "temp_min",
            "standard_id",
            "revision",
        ):
            with self.assertRaises(runtime_settings.SettingsValidationError):
                runtime_settings.apply_changes(
                    {key: "anything"}, reason="越权写入"
                )

    def test_reason_and_changes_validation(self) -> None:
        cases = (
            {"changes": {"feishu_alarm_notify_enabled": True}, "reason": ""},
            {"changes": {"feishu_alarm_notify_enabled": True}, "reason": None},
            {"changes": {"feishu_alarm_notify_enabled": True}},
            {"changes": {}, "reason": "空变更集"},
            {"changes": "not-a-dict", "reason": "结构错误"},
            {"reason": "缺少 changes"},
        )
        for case in cases:
            with self.assertRaises(runtime_settings.SettingsValidationError):
                runtime_settings.apply_changes(
                    case.get("changes"), reason=case.get("reason")
                )

    def test_exit_margin_effective_never_below_entry(self) -> None:
        # 与 config.py 语义一致：只提高提前量时，退出裕量按 max() 归一化，
        # 绝不会出现比提前量更窄的回差带
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.5}, reason="只调提前量"
        )
        margins = runtime_settings.prewarning_margins()
        self.assertEqual(margins["temperature"], 0.5)
        self.assertEqual(margins["temperature_exit"], 0.5)
        overview = runtime_settings.settings_overview()
        self.assertEqual(
            overview["temperature_prewarning_exit_margin_c"]["value"], 0.5
        )
        # 显式设置更大的退出裕量照常生效
        runtime_settings.apply_changes(
            {
                "temperature_prewarning_margin_c": 0.5,
                "temperature_prewarning_exit_margin_c": 0.6,
            },
            reason="成对调整",
        )
        margins = runtime_settings.prewarning_margins()
        self.assertEqual(margins["temperature"], 0.5)
        self.assertEqual(margins["temperature_exit"], 0.6)

    # 10. audit 正常生成
    def test_audit_rows_written(self) -> None:
        runtime_settings.apply_changes(
            {"feishu_alarm_notify_enabled": True, "feishu_recovery_notify_enabled": True},
            reason="开启通知",
        )
        rows = runtime_settings.fetch_audit(limit=10)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["old_value"], "false")
            self.assertEqual(row["new_value"], "true")
            self.assertEqual(row["changed_by"], "console")
            self.assertEqual(row["reason"], "开启通知")
            self.assertTrue(row["changed_at"])

    # 11. DB 写入失败回滚（无部分写入）
    def test_write_failure_rolls_back(self) -> None:
        connection = db.peek_connection()
        assert connection is not None
        # 破坏审计表使事务中途失败：settings 行已插入、audit 插入报错
        connection.execute("DROP TABLE runtime_setting_audit")
        connection.commit()
        with self.assertRaises(runtime_settings.SettingsWriteError):
            runtime_settings.apply_changes(
                {"feishu_alarm_notify_enabled": True}, reason="会失败"
            )
        with connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM runtime_settings"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    # 16. SQLite 锁冲突重试：一次变更每个 key 仅一条审计行
    def test_lock_retry_does_not_duplicate_audit_rows(self) -> None:
        connection = db.peek_connection()
        assert connection is not None

        class _LockOnceOnAuditInsert:
            """首次 audit INSERT 抛 database is locked 的连接代理。

            run_sqlite_write_with_retry 捕获锁错误后回滚整个事务并重试
            callback；代理同时记录重试恢复时事务确已回滚。
            """

            def __init__(self) -> None:
                self.audit_insert_attempts = 0
                self.locked_raised = False
                self.in_transaction_when_retry_resumed = None

            def execute(self, sql, params=()):
                if (
                    sql.lstrip().upper().startswith("INSERT")
                    and "runtime_setting_audit" in sql
                ):
                    self.audit_insert_attempts += 1
                    if self.audit_insert_attempts == 1:
                        self.locked_raised = True
                        raise sqlite3.OperationalError("database is locked")
                if (
                    self.locked_raised
                    and self.in_transaction_when_retry_resumed is None
                ):
                    self.in_transaction_when_retry_resumed = (
                        connection.in_transaction
                    )
                return connection.execute(sql, params)

            def __getattr__(self, name):
                return getattr(connection, name)

        proxy = _LockOnceOnAuditInsert()
        with patch.object(db, "peek_connection", return_value=proxy):
            runtime_settings.apply_changes(
                {
                    "temperature_prewarning_margin_c": 0.5,
                    "feishu_alarm_notify_enabled": True,
                },
                reason="锁重试验证",
            )

        # 锁错误确实触发，且重试恢复前第一次尝试的事务已回滚
        self.assertTrue(proxy.locked_raised)
        self.assertIsNotNone(proxy.in_transaction_when_retry_resumed)
        self.assertFalse(proxy.in_transaction_when_retry_resumed)

        # 配置值正确写入并生效
        self.assertEqual(
            runtime_settings.get_setting("temperature_prewarning_margin_c"), 0.5
        )
        self.assertTrue(runtime_settings.get_setting("feishu_alarm_notify_enabled"))

        # 每个 key 仅 1 条审计行，不存在重复
        with connection:
            rows = connection.execute(
                "SELECT key, COUNT(*) FROM runtime_setting_audit GROUP BY key"
            ).fetchall()
        self.assertEqual(
            {row[0]: row[1] for row in rows},
            {
                "temperature_prewarning_margin_c": 1,
                "feishu_alarm_notify_enabled": 1,
            },
        )
        self.assertEqual(len(runtime_settings.fetch_audit(limit=100)), 2)

    # 15. 重启（重新打开镜像连接）后 override 仍然存在
    def test_override_survives_reconnect(self) -> None:
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.4}, reason="持久化验证"
        )
        db.close()
        db._init_failed = False
        db.init_db()
        self.assertEqual(
            runtime_settings.get_setting("temperature_prewarning_margin_c"), 0.4
        )

    def test_setting_back_to_default_clears_override(self) -> None:
        default = config.TEMPERATURE_PREWARNING_MARGIN_C
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.5}, reason="先覆盖"
        )
        overview = runtime_settings.settings_overview()
        self.assertEqual(
            overview["temperature_prewarning_margin_c"]["source"], "override"
        )
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": default}, reason="恢复默认"
        )
        overview = runtime_settings.settings_overview()
        self.assertEqual(
            overview["temperature_prewarning_margin_c"]["source"], "default"
        )
        self.assertEqual(
            overview["temperature_prewarning_margin_c"]["value"], default
        )

    def test_corrupt_stored_value_falls_back_to_default(self) -> None:
        connection = db.peek_connection()
        assert connection is not None
        with connection:
            connection.execute(
                "INSERT INTO runtime_settings (key, value, value_type,"
                " updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
                (
                    "temperature_prewarning_margin_c",
                    "not-a-number",
                    "float",
                    "2026-01-01T00:00:00",
                    "manual",
                ),
            )
        self.assertEqual(
            runtime_settings.get_setting("temperature_prewarning_margin_c"),
            config.TEMPERATURE_PREWARNING_MARGIN_C,
        )

    def test_audit_limit_clamped_to_100(self) -> None:
        for index in range(105):
            runtime_settings.apply_changes(
                {"temperature_prewarning_margin_c": 0.1 + (index % 10) * 0.07},
                reason=f"批次{index}",
            )
        self.assertEqual(len(runtime_settings.fetch_audit(limit=1000)), 100)


class _MonitorOperationProvider:
    """固定作业状态的操作上下文提供者（与 test_prewarning 相同的形态）。"""

    def get(self, device: DeviceContext):
        from domain.models import OperationState, OperationStatus

        return OperationState(
            area_id=device.area,
            state=OperationStatus.OPERATING,
            operation_type=None,
            work_order=None,
            started_at=None,
            ended_at=None,
        )


class MonitorHotUpdateTests(_MirrorIsolatedTestCase):
    """端到端：监控主链路的裕量/通知开关真正读取 runtime settings。

    证明「热更新立即生效」发生在业务评估路径上：同一个 service 实例、
    不重启不重建，SQLite override 提交后下一个样本即按新裕量判定。
    """

    def setUp(self) -> None:
        super().setUp()
        db.init_db()
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.states = SQLiteAlarmStateRepository(self.connection)
        executor = ActionExecutor(
            mode="shadow",
            active_device_ids=("TH-01",),
            standards_ready_provider=lambda: True,
        )
        self.service = MonitorApplicationService(
            operation_state_provider=_MonitorOperationProvider(),
            standard_resolver=StaticStandardResolver(
                (
                    EnvironmentStandard(
                        standard_id="STD-1",
                        revision="R1",
                        area="仓库",
                        operation_type=None,
                        temperature_min=20.0,
                        temperature_max=26.0,
                        humidity_min=40.0,
                        humidity_max=60.0,
                        effective_from=NOW - timedelta(days=1),
                        effective_to=None,
                        source_document="SOP",
                        clause="5.2",
                        control_type=ControlType.ALL_DAY,
                    ),
                )
            ),
            alarm_state_repository=self.states,
            alarm_state_machine=AlarmStateMachine(),
            action_mapper=ApplicationActionMapper(emit_notifications=True),
            action_executor=executor,
        )

    def _handle(self, device: str, temperature: float):
        return self.service.handle_sample(
            device=DeviceContext(device, "仓库"),
            sample=MonitorSample(device, NOW, temperature, 50.0),
            now=NOW,
        )

    def test_margin_override_changes_evaluation_immediately(self) -> None:
        # 默认提前量 0.2：25.9 距上限 26.0 仅 0.1 → 触发接近限值预警
        result = self._handle("TH-01", 25.9)
        self.assertIn(
            "temperature_high_near_limit",
            result.monitor_result.prewarning_reasons,
        )
        # 控制台把提前量覆盖为 0.05 后，无需重启：同一 service 实例对
        # 下一个（全新状态的）样本立即按新裕量判定，25.9 不再预警
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.05},
            reason="PE仓库预警灵敏度试运行调整",
        )
        result = self._handle("TH-02", 25.9)
        self.assertEqual(result.monitor_result.prewarning_reasons, ())
        # 提高到 0.4：25.7（距上限 0.3）也进入预警带
        runtime_settings.apply_changes(
            {"temperature_prewarning_margin_c": 0.4}, reason="提高灵敏度"
        )
        result = self._handle("TH-03", 25.7)
        self.assertIn(
            "temperature_high_near_limit",
            result.monitor_result.prewarning_reasons,
        )

    def test_monitor_reads_defaults_when_mirror_closed(self) -> None:
        # 镜像连接关闭（等价于未初始化）：评估回退 config.py 默认值，不抛异常
        db.close()
        result = self._handle("TH-01", 25.9)
        self.assertIn(
            "temperature_high_near_limit",
            result.monitor_result.prewarning_reasons,
        )


class SettingsApiTests(_MirrorIsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = create_app().test_client()
        self.headers = {"X-History-Key": TEST_KEY}

    # 8. HISTORY_API_KEY 未配置：继续 fail closed（503，绝不静默开放）
    def test_unconfigured_key_returns_503(self) -> None:
        config.HISTORY_API_KEY = ""
        self.assertEqual(self.client.get("/api/settings").status_code, 503)
        self.assertEqual(
            self.client.patch(
                "/api/settings", json={"changes": {}, "reason": "x"}
            ).status_code,
            503,
        )
        self.assertEqual(
            self.client.get("/api/settings/audit").status_code, 503
        )

    # 9. API 鉴权失败 → 401
    def test_wrong_key_returns_401(self) -> None:
        bad = {"X-History-Key": "wrong"}
        self.assertEqual(
            self.client.get("/api/settings", headers=bad).status_code, 401
        )
        self.assertEqual(
            self.client.patch(
                "/api/settings",
                headers=bad,
                json={"changes": {"feishu_alarm_notify_enabled": True}, "reason": "x"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/api/settings/audit", headers=bad).status_code, 401
        )

    def test_mirror_disabled_returns_503(self) -> None:
        with patch("routes.api.db.is_enabled", return_value=False):
            self.assertEqual(
                self.client.get("/api/settings", headers=self.headers).status_code,
                503,
            )
            self.assertEqual(
                self.client.patch(
                    "/api/settings",
                    headers=self.headers,
                    json={"changes": {"feishu_alarm_notify_enabled": True}, "reason": "x"},
                ).status_code,
                503,
            )

    def test_get_settings_shape_whitelist_and_runtime_summary(self) -> None:
        response = self.client.get("/api/settings", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload["settings"]), WHITELIST)
        item = payload["settings"]["temperature_prewarning_margin_c"]
        for field in ("value", "default", "type", "unit", "min", "max", "source", "label"):
            self.assertIn(field, item)
        self.assertEqual(item["type"], "float")
        self.assertEqual(item["unit"], "°C")
        self.assertEqual(item["value"], config.TEMPERATURE_PREWARNING_MARGIN_C)
        for field in (
            "automation_mode",
            "feishu_write_enabled",
            "active_device_ids",
            "active_cutover_ack_valid",
            "standards_ready",
            "shadow_available",
            "active_canary_enabled",
            "latest_sync_status",
        ):
            self.assertIn(field, payload["runtime"])

    # 14. secret 不出现在 settings API
    def test_secrets_never_exposed(self) -> None:
        config.APP_SECRET = "super-secret-app-secret-xyz"
        response = self.client.get("/api/settings", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn("super-secret-app-secret-xyz", text)
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn("HISTORY_API_KEY", text)
        self.assertNotIn("APP_SECRET", text)
        # secret/凭据类 key 也不允许写入
        for key in ("history_api_key", "app_secret", "app_token", "table_id"):
            response = self.client.patch(
                "/api/settings",
                headers=self.headers,
                json={"changes": {key: "new-value"}, "reason": "越权"},
            )
            self.assertEqual(response.status_code, 400, key)

    def test_patch_valid_change_applies_and_audits(self) -> None:
        response = self.client.patch(
            "/api/settings",
            headers=self.headers,
            json={
                "changes": {"temperature_prewarning_margin_c": 0.5},
                "reason": "PE仓库预警灵敏度试运行调整",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["updated"], ["temperature_prewarning_margin_c"])
        self.assertEqual(
            body["settings"]["temperature_prewarning_margin_c"]["value"], 0.5
        )

        payload = self.client.get("/api/settings", headers=self.headers).get_json()
        item = payload["settings"]["temperature_prewarning_margin_c"]
        self.assertEqual(item["value"], 0.5)
        self.assertEqual(item["source"], "override")
        # 热更新立即对业务读取层生效
        self.assertEqual(
            runtime_settings.get_setting("temperature_prewarning_margin_c"), 0.5
        )

        audit = self.client.get("/api/settings/audit", headers=self.headers).get_json()
        self.assertEqual(audit["count"], 1)
        row = audit["items"][0]
        self.assertEqual(row["key"], "temperature_prewarning_margin_c")
        self.assertEqual(
            row["old_value"], repr(float(config.TEMPERATURE_PREWARNING_MARGIN_C))
        )
        self.assertEqual(row["new_value"], "0.5")
        self.assertEqual(row["reason"], "PE仓库预警灵敏度试运行调整")

    # 4/5/6/7 的 API 层验证
    def test_patch_invalid_inputs_return_400(self) -> None:
        cases = [
            {"changes": {"temperature_prewarning_margin_c": True}, "reason": "x"},
            {"changes": {"temperature_prewarning_margin_c": "0.5"}, "reason": "x"},
            {"changes": {"temperature_prewarning_margin_c": 99.0}, "reason": "x"},
            {"changes": {"temperature_prewarning_margin_c": -1.0}, "reason": "x"},
            {"changes": {"feishu_alarm_notify_enabled": 1}, "reason": "x"},
            {"changes": {"feishu_alarm_notify_enabled": "true"}, "reason": "x"},
            {"changes": {"unknown_setting": 1}, "reason": "x"},
            {"changes": {"AUTOMATION_MODE": "active"}, "reason": "x"},
            {"changes": {"FEISHU_WRITE_ENABLED": True}, "reason": "x"},
            {"changes": {"ACTIVE_DEVICE_IDS": "TH-01"}, "reason": "x"},
            {"changes": {}, "reason": "x"},
            {"changes": "not-a-dict", "reason": "x"},
            {"changes": {"feishu_alarm_notify_enabled": True}, "reason": ""},
            {"changes": {"feishu_alarm_notify_enabled": True}},
        ]
        for body in cases:
            response = self.client.patch(
                "/api/settings", headers=self.headers, json=body
            )
            self.assertEqual(response.status_code, 400, body)

    def test_patch_nan_inf_rejected(self) -> None:
        for raw in (
            '{"changes": {"temperature_prewarning_margin_c": NaN}, "reason": "x"}',
            '{"changes": {"temperature_prewarning_margin_c": Infinity}, "reason": "x"}',
        ):
            response = self.client.patch(
                "/api/settings",
                headers=self.headers,
                data=raw,
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400, raw)

    def test_patch_margin_normalization_via_api(self) -> None:
        # 单独把提前量提高到 0.5：与 .env 语义一致，退出裕量自动按 max 归一
        response = self.client.patch(
            "/api/settings",
            headers=self.headers,
            json={
                "changes": {"temperature_prewarning_margin_c": 0.5},
                "reason": "只调提前量",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = self.client.get("/api/settings", headers=self.headers).get_json()
        margins = payload["settings"]
        self.assertEqual(
            margins["temperature_prewarning_margin_c"]["value"], 0.5
        )
        self.assertEqual(
            margins["temperature_prewarning_exit_margin_c"]["value"], 0.5
        )
        response = self.client.patch(
            "/api/settings",
            headers=self.headers,
            json={
                "changes": {
                    "temperature_prewarning_margin_c": 0.5,
                    "temperature_prewarning_exit_margin_c": 0.6,
                },
                "reason": "成对调整",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            runtime_settings.prewarning_margins()["temperature_exit"], 0.6
        )

    # 12. /api/thresholds 仍然只读
    def test_thresholds_put_still_409(self) -> None:
        response = self.client.put(
            "/api/thresholds/TH-01",
            headers=self.headers,
            json={"temp_min": 20.0, "temp_max": 26.0},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["authoritative_source"], "feishu")

    # 13. 正式 Feishu standard 不会被 runtime settings 覆盖
    def test_feishu_standards_untouched_by_settings_patch(self) -> None:
        before = self.client.get("/api/thresholds", headers=self.headers).get_json()
        self.assertEqual(before["authoritative_source"], "feishu")
        response = self.client.patch(
            "/api/settings",
            headers=self.headers,
            json={
                "changes": {
                    "temperature_prewarning_margin_c": 0.4,
                    "temperature_prewarning_exit_margin_c": 0.5,
                },
                "reason": "调整灵敏度",
            },
        )
        self.assertEqual(response.status_code, 200)
        after = self.client.get("/api/thresholds", headers=self.headers).get_json()
        self.assertEqual(before, after)
        # 标准相关 key 无法通过 settings 修改
        for key in ("temp_min", "temp_max", "humidity_min", "standard_id", "revision"):
            response = self.client.patch(
                "/api/settings",
                headers=self.headers,
                json={"changes": {key: 1}, "reason": "越权"},
            )
            self.assertEqual(response.status_code, 400, key)

    def test_audit_limit_parameter(self) -> None:
        for index in range(4):
            response = self.client.patch(
                "/api/settings",
                headers=self.headers,
                json={
                    "changes": {"feishu_alarm_notify_enabled": index % 2 == 0},
                    "reason": f"批次{index}",
                },
            )
            self.assertEqual(response.status_code, 200)
        two = self.client.get(
            "/api/settings/audit?limit=2", headers=self.headers
        ).get_json()
        self.assertEqual(two["count"], 2)
        self.assertEqual(two["items"][0]["reason"], "批次3")
        huge = self.client.get(
            "/api/settings/audit?limit=5000", headers=self.headers
        ).get_json()
        self.assertLessEqual(huge["count"], 100)


class ConsoleSettingsTabTests(unittest.TestCase):
    def test_console_shell_contains_settings_tab(self) -> None:
        # 页面壳保持开放；新增“系统配置”Tab 与全部数据接口的密钥语义不变
        client = create_app().test_client()
        response = client.get("/console")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("工业监控台", html)
        self.assertIn("系统配置", html)
        self.assertIn('id="tab-settings"', html)
        self.assertIn("正式环境标准由 Feishu validated standard 管理，本页面只读", html)


if __name__ == "__main__":
    unittest.main()
