"""生产审计复现测试：Shadow 白名单、比对失败可观测、重试上限与历史清理。

这些用例对应 2026-09 审计发现：
1. 只有 TH-10 产生 SHADOW_COMPARE 的根因（SHADOW_DEVICE_IDS 白名单静默丢弃）。
2. 比对失败以前不留任何 automation_runs 记录，无法与"被跳过"区分。
3. SHADOW_RETRY 无上限时永久性错误会无限累积任务。
4. automation_runs / automation_tasks 无保留期，长跑无限增长。
5. 调度线程单次 tick 异常会杀死整个 Shadow Runtime。
6. Active 写回的 datetime 需要换算业务时区，否则飞书侧偏移 8 小时。
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from domain.models import MonitorSample
from integrations.feishu_records import FeishuRawRecord
from repositories.automation_runs import purge_automation_runs
from repositories.automation_tasks import purge_finished_automation_tasks
from runtime.bootstrap import build_runtime, runtime_status


class _NoDeviceSource:
    """设备表为空：任何 observe() 都会抛 RuntimeError（模拟飞书缺记录）。"""

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


class ShadowAuditTests(unittest.TestCase):
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
                "AUTOMATION_RUN_RETENTION_DAYS",
                "HISTORY_TIMEZONE",
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = ("TH-10",)
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        config.HISTORY_TIMEZONE = "Asia/Shanghai"

    def tearDown(self) -> None:
        for name, value in self.original.items():
            setattr(config, name, value)

    def test_non_whitelist_device_never_schedules_compare(self) -> None:
        """根因复现：白名单外设备静默丢弃，不产生任何 SHADOW_COMPARE。"""
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        components = build_runtime(
            connection=connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: now,
        )
        components.runtime.handle_standard_sync(object())
        components.runtime._accepting_samples = True
        # TH-03 持续上报但不在 SHADOW_DEVICE_IDS 中。
        self.assertIsNone(
            components.handle_sample(
                MonitorSample("TH-03", now, 25.0, 50.0, online_status="online")
            )
        )
        rows = connection.execute(
            "SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'SHADOW_COMPARE'"
        ).fetchone()
        self.assertEqual(rows[0], 0)
        # 跳过要留痕：一次性日志集合包含该设备。
        self.assertIn("TH-03", components.runtime._skipped_device_log)
        components.stop()

    def test_unconfigured_device_logs_structured_reason_once(self) -> None:
        """白名单外设备的忽略日志必须含 device/reason/configured，且限频。"""
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        components = build_runtime(
            connection=connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: now,
        )
        components.runtime._accepting_samples = True
        with self.assertLogs("temperature_monitor", level="INFO") as logs:
            self.assertIsNone(
                components.handle_sample(
                    MonitorSample("TH-03", now, 25.0, 50.0, online_status="online")
                )
            )
            # 第二条样本不得重复刷屏（限频：每设备一次）。
            self.assertIsNone(
                components.handle_sample(
                    MonitorSample("TH-03", now, 25.1, 50.1, online_status="online")
                )
            )
        ignored = [line for line in logs.output if "sample ignored" in line]
        self.assertEqual(len(ignored), 1)
        self.assertIn("device=TH-03", ignored[0])
        self.assertIn("reason=not_configured_in_shadow_whitelist", ignored[0])
        self.assertIn("configured_devices=TH-10", ignored[0])
        components.stop()

    def test_unconfigured_device_does_not_break_temperature_route(self) -> None:
        """白名单外设备照常走统一设备模型：record_sample 正常入库返回。"""
        from unittest.mock import patch

        devices_module = __import__("services.devices", fromlist=["record_sample"])
        with (
            patch.object(devices_module.db, "fetch_previous_device_sample", return_value=None),
            patch.object(devices_module.db, "save_device_sample") as save_mock,
            patch.object(devices_module.db, "save_device_event"),
        ):
            transitions = devices_module.record_sample(
                "TH-03", devices_module.SOURCE_HOME_ASSISTANT, 25.0, 50.0, "在线"
            )
        self.assertEqual(transitions, [])
        save_mock.assert_called_once()

    def test_failed_compare_records_error_run_and_bounded_retry(self) -> None:
        """比对失败必须落 OBSERVATION_ERROR run，且重试有上限。"""
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        components = build_runtime(
            connection=connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: now,
        )
        payload = {
            "sample_time": now.isoformat(),
            "expected": {
                "device_id": "TH-10",
                "alarm_state": "NORMAL",
                "operation_state": "NOT_APPLICABLE",
                "event_exists": False,
            },
        }
        task = components.task_repository.create_or_get(
            task_type="SHADOW_COMPARE",
            entity_type="DEVICE",
            entity_id="TH-10",
            due_at=now,
            payload=payload,
            dedupe_key="AUDIT:1",
            created_at=now,
        )
        with self.assertRaises(RuntimeError):
            components.runtime.handle_shadow_compare(task)
        # 失败落 run，区分“无比对”与“比对失败”。
        run_row = connection.execute(
            "SELECT difference_type FROM automation_runs WHERE action_type = 'SHADOW_COMPARE'"
        ).fetchone()
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row[0], "OBSERVATION_ERROR")
        # 第一次失败：产生 1 条重试任务（compare_attempt=1）。
        retry_rows = connection.execute(
            "SELECT payload_json FROM automation_tasks WHERE dedupe_key LIKE 'SHADOW_RETRY:%'"
        ).fetchall()
        self.assertEqual(len(retry_rows), 1)

        # 模拟第 3 次尝试（payload 携带累计次数 2）：不再产生新重试。
        final_task = components.task_repository.create_or_get(
            task_type="SHADOW_COMPARE",
            entity_type="DEVICE",
            entity_id="TH-10",
            due_at=now + timedelta(seconds=60),
            payload={**payload, "compare_attempt": 2},
            dedupe_key="AUDIT:3",
            created_at=now,
        )
        with self.assertRaises(RuntimeError):
            components.runtime.handle_shadow_compare(final_task)
        retry_rows = connection.execute(
            "SELECT payload_json FROM automation_tasks WHERE dedupe_key LIKE 'SHADOW_RETRY:%'"
        ).fetchall()
        self.assertEqual(len(retry_rows), 1)
        components.stop()

    def test_scheduler_tick_failure_does_not_kill_loop(self) -> None:
        """单次 tick 的底层异常（如 SQLite busy）不得杀死调度线程。"""
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        components = build_runtime(
            connection=connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: now,
        )
        components.runtime.scheduler.poll_interval = 0.0
        stop_event = threading.Event()
        calls: list[int] = []

        def flaky_run_once() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            if len(calls) >= 3:
                stop_event.set()

        components.runtime.scheduler.run_once = flaky_run_once  # type: ignore[method-assign]
        components.runtime._run_scheduler(stop_event)
        self.assertEqual(len(calls), 3)
        components.stop()

    def test_purge_automation_history_bounds_growth(self) -> None:
        """清理只删除保留期前的终态数据，RUNNING 任务与近期数据保留。"""
        now = datetime(2026, 8, 31, 12, 0)
        connection = sqlite3.connect(":memory:")
        components = build_runtime(
            connection=connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: now,
        )
        old = now - timedelta(days=45)
        fresh = now - timedelta(days=1)
        for i, (status, finished_at) in enumerate(
            (
                ("SUCCEEDED", old.isoformat()),
                ("FAILED", old.isoformat()),
                ("RUNNING", None),
                ("PENDING", None),
            )
        ):
            connection.execute(
                """
                INSERT INTO automation_tasks (
                    id, task_type, entity_type, entity_id, due_at, status,
                    payload_json, dedupe_key, created_at, updated_at,
                    started_at, finished_at, claimed_at, lease_until,
                    worker_id, attempt_count, last_error
                ) VALUES (?, 'SHADOW_COMPARE', 'DEVICE', 'TH-10', ?,
                          ?, '{}', ?, ?, ?, NULL, ?, NULL, NULL, 'w', 1, NULL)
                """,
                (
                    f"task-{i}",
                    old.isoformat(),
                    status,
                    f"dedupe-{i}",
                    old.isoformat(),
                    old.isoformat(),
                    finished_at,
                ),
            )
        for i, created_at in enumerate((old.isoformat(), fresh.isoformat())):
            connection.execute(
                """
                INSERT INTO automation_runs (
                    id, device_id, sample_time, mode, action_type, action_status,
                    feishu_observed_state_json, matched, difference_type,
                    details_json, context_json, created_at
                ) VALUES (?, 'TH-10', ?, 'shadow', 'SHADOW_COMPARE', 'COMPARED',
                          '{}', 1, NULL, '{}', '{}', ?)
                """,
                (f"run-{i}", old.isoformat(), created_at),
            )
        cutoff = now - timedelta(days=config.AUTOMATION_RUN_RETENTION_DAYS)
        purged_tasks = purge_finished_automation_tasks(connection, cutoff)
        purged_runs = purge_automation_runs(connection, cutoff)
        self.assertEqual(purged_tasks, 2)
        self.assertEqual(purged_runs, 1)
        remaining_status = connection.execute(
            "SELECT status FROM automation_tasks ORDER BY id"
        ).fetchall()
        self.assertEqual(
            sorted(row[0] for row in remaining_status),
            ["PENDING", "RUNNING"],
        )
        remaining_runs = connection.execute(
            "SELECT COUNT(*) FROM automation_runs"
        ).fetchone()[0]
        self.assertEqual(remaining_runs, 1)
        components.stop()

    def test_datetime_cell_converts_instants_to_epoch_milliseconds(self) -> None:
        """Aware values preserve their instant; naive values use business time."""
        import integrations.feishu_writers as writers

        writers._business_tz_cache = None
        utc_value = datetime(2026, 8, 31, 4, 30, 0, tzinfo=timezone.utc)
        expected = int(utc_value.timestamp() * 1000)
        self.assertEqual(writers._datetime_cell(utc_value), expected)
        business_value = utc_value.astimezone(writers._business_timezone())
        self.assertEqual(writers._datetime_cell(business_value), expected)
        naive_value = datetime(2026, 8, 31, 12, 30, 0)
        self.assertEqual(writers._datetime_cell(naive_value), expected)
        with_microseconds = utc_value.replace(microsecond=987654)
        self.assertEqual(writers._datetime_cell(with_microseconds), expected + 987)
        writers._business_tz_cache = None

    def test_runtime_status_reflects_built_runtime(self) -> None:
        """runtime_status() 暴露运行时健康，供 /api/system/status 使用。"""
        import runtime.bootstrap as bootstrap

        components = build_runtime(
            connection=sqlite3.connect(":memory:", check_same_thread=False),
            record_source=_NoDeviceSource(),
            now_provider=lambda: datetime(2026, 8, 31, 12, 0),
        )
        try:
            status = runtime_status()
            self.assertTrue(status["available"])
            self.assertEqual(status["mode"], "shadow")
            self.assertIn("configured_shadow_devices", status)
        finally:
            bootstrap._last_components = None
            components.stop()

    def test_maybe_purge_actually_deletes_history(self) -> None:
        """调度循环里的清理钩子真正执行：过期 run/task 被删、新鲜数据保留。"""
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        components = build_runtime(
            connection=connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: now,
        )
        old = (now - timedelta(days=45)).isoformat()
        fresh = now.isoformat()
        for i, created_at in enumerate((old, fresh)):
            connection.execute(
                """
                INSERT INTO automation_runs (
                    id, device_id, sample_time, mode, action_type, action_status,
                    feishu_observed_state_json, matched, difference_type,
                    details_json, context_json, created_at
                ) VALUES (?, 'TH-10', ?, 'shadow', 'SHADOW_COMPARE', 'COMPARED',
                          '{}', 1, NULL, '{}', '{}', ?)
                """,
                (f"run-{i}", old, created_at),
            )
        connection.execute(
            """
            INSERT INTO automation_tasks (
                id, task_type, entity_type, entity_id, due_at, status,
                payload_json, dedupe_key, created_at, updated_at,
                started_at, finished_at, claimed_at, lease_until,
                worker_id, attempt_count, last_error
            ) VALUES ('task-old', 'SHADOW_COMPARE', 'DEVICE', 'TH-10', ?,
                      'SUCCEEDED', '{}', 'dedupe-old', ?, ?, NULL, ?, NULL, NULL,
                      'w', 1, NULL)
            """,
            (old, old, old, old),
        )
        components.runtime._last_purge_time = None
        components.runtime._maybe_purge()
        remaining_runs = connection.execute(
            "SELECT id FROM automation_runs ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in remaining_runs], ["run-1"])
        remaining_tasks = connection.execute(
            "SELECT COUNT(*) FROM automation_tasks"
        ).fetchone()[0]
        self.assertEqual(remaining_tasks, 0)
        components.stop()

    def test_maybe_purge_respects_disabled_retention(self) -> None:
        """AUTOMATION_RUN_RETENTION_DAYS=0 时清理关闭：历史数据原样保留。"""
        original_retention = config.AUTOMATION_RUN_RETENTION_DAYS
        config.AUTOMATION_RUN_RETENTION_DAYS = 0
        now = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        components = build_runtime(
            connection=connection,
            record_source=_NoDeviceSource(),
            now_provider=lambda: now,
        )
        try:
            old = (now - timedelta(days=400)).isoformat()
            connection.execute(
                """
                INSERT INTO automation_runs (
                    id, device_id, sample_time, mode, action_type, action_status,
                    feishu_observed_state_json, matched, difference_type,
                    details_json, context_json, created_at
                ) VALUES ('run-old', 'TH-10', ?, 'shadow', 'SHADOW_COMPARE', 'COMPARED',
                          '{}', 1, NULL, '{}', '{}', ?)
                """,
                (old, old),
            )
            components.runtime._last_purge_time = None
            components.runtime._maybe_purge()
            count = connection.execute(
                "SELECT COUNT(*) FROM automation_runs"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            config.AUTOMATION_RUN_RETENTION_DAYS = original_retention
            components.stop()


if __name__ == "__main__":
    unittest.main()
