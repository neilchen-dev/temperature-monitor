"""上线前专项：projection crash-consistency 与 scheduler-blocking。

故障点注入矩阵：
1. Feishu write 成功 → mark projected → 崩溃（dispatch 前）→ 重启
   → 自动补 dispatch → 恰好一个 SHADOW_COMPARE。
2. dispatch listener 已执行 → 派发水位落库前崩溃 → 重启补 dispatch
   → 不产生业务重复副作用（at-least-once + downstream idempotency）。
3. projection_status=ok 但 projected > dispatched → scheduler tick 自动恢复。
4. pending + 重启 → projection retry 正常恢复（既有用例覆盖，此处走
   runtime tick 挂钩验证）。
5. 11 台设备 projection 同时失败 → SHADOW_COMPARE / SYNC 不被饿死；
   每个 tick 至多 1 个有界投影尝试；backoff 仍为 30/60/120/…；无 retry storm。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

import config
import runtime.bootstrap as bootstrap
from app import create_app
from integrations.feishu_records import FeishuRawRecord
from runtime.bootstrap import build_runtime
from services import db, devices, projection


TZ = ZoneInfo("Asia/Shanghai")


def _std_record(record_id: str, device: str, temp_max: float) -> FeishuRawRecord:
    return FeishuRawRecord(
        record_id=record_id,
        fields={
            "标准编号": f"ENV-{device}",
            "版本": "Rev.A",
            "适用区域": "PE仓库",
            "适用设备": device,
            "适用作业类型": None,
            "温度下限（°C）": 20,
            "温度上限（°C）": temp_max,
            "湿度下限（%RH）": 40,
            "湿度上限（%RH）": 60,
            "生效时间": "2026-01-01T00:00:00",
            "失效时间": None,
            "优先级": 1,
            "是否启用": True,
            "来源文件": "SOP-001",
            "条款": "5.2.3",
        },
        created_at=datetime(2026, 1, 1, tzinfo=TZ),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
    )


def _dev_record(record_id: str, device: str) -> FeishuRawRecord:
    return FeishuRawRecord(
        record_id=record_id,
        fields={
            "设备编号": device,
            "警报状态": "未触发",
            "当前作业状态": "N/A",
            "当前判定状态": "正常",
            "温度判定": "正常",
            "湿度判定": "正常",
            "在线状态": "在线",
        },
        created_at=datetime(2026, 1, 1, tzinfo=TZ),
        updated_at=datetime(2026, 9, 1, 12, 0, tzinfo=TZ),
    )


class _ChainSource:
    """最小只读源：TH-10 设备记录 + 一条标准。"""

    def __init__(self) -> None:
        self.records: dict[str, list[FeishuRawRecord]] = {
            config.FEISHU_STANDARD_TABLE_ID: [
                _std_record("std-10", "TH-10", 26),
            ],
            config.FEISHU_OPERATION_TABLE_ID: [],
            config.FEISHU_EVENT_TABLE_ID: [],
            "device-table": [_dev_record("dev-10", "TH-10")],
        }

    def read_records(self, table_id: str):
        return tuple(self.records.get(table_id, ()))


def _connection_error(*_args, **_kwargs):
    raise requests.exceptions.ConnectionError("feishu unreachable")


class CrashConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=TZ)
        self.source = _ChainSource()
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
                "SQLITE_DB_PATH",
                "HISTORY_TIMEZONE",
                "AUTOMATION_RUN_RETENTION_DAYS",
                "DEVICE_NAME_MAP",
                "DEVICES",
                "TEMPERATURE_API_KEY",
                "TEMPERATURE_DEDUPE_WINDOW_MS",
                "FEISHU_PROJECTION_MAX_RETRIES",
                "FEISHU_PROJECTION_BACKOFF_SECONDS",
                "FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS",
                "FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS",
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = ("TH-10",)
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "crash.db"
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        config.DEVICE_NAME_MAP = {}
        config.DEVICES = {}
        config.TEMPERATURE_API_KEY = ""
        config.TEMPERATURE_DEDUPE_WINDOW_MS = 5000
        config.FEISHU_PROJECTION_MAX_RETRIES = 5
        config.FEISHU_PROJECTION_BACKOFF_SECONDS = 30.0
        config.FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS = 0.0
        config.FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS = 5.0
        db.close()
        db._init_failed = False
        devices._reset_device_model_stats()
        self.addCleanup(self._restore)

        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.connection.close)
        self.components = build_runtime(
            connection=self.connection,
            record_source=self.source,
            now_provider=lambda: self.now,
        )
        self.addCleanup(self._cleanup_runtime)
        runtime = self.components.runtime
        runtime.handle_standard_sync(object())

        with patch.object(
            runtime, "_run_scheduler", side_effect=lambda stop_event: stop_event.wait()
        ):
            runtime.start()

        self.dispatched: list = []
        devices.register_sample_listener(self._capture_sample)
        self.client = create_app().test_client()

    def _restore(self) -> None:
        devices.unregister_sample_listener(self._capture_sample)
        devices._reset_device_model_stats()
        db._init_failed = False
        db.close()
        bootstrap._last_components = None
        for name, value in self.original.items():
            setattr(config, name, value)
        self._tmp_dir.cleanup()

    def _cleanup_runtime(self) -> None:
        bootstrap._last_components = None
        try:
            self.components.stop()
        except Exception:  # noqa: BLE001 - cleanup must not mask test result
            pass

    def _capture_sample(self, sample) -> None:
        self.dispatched.append(sample)

    def _clock(self, seconds: float = 0.0) -> datetime:
        """扫描/调度时钟：水位与 last_attempt_at 用真实墙钟，退避判定
        必须基于真实时间的未来偏移。"""
        return datetime.now().astimezone() + timedelta(seconds=seconds)

    def _task_rows(self, task_type: str, status: str | None = None):
        query = (
            "SELECT task_type, status, entity_id, payload_json, last_error"
            " FROM automation_tasks WHERE task_type = ?"
        )
        params: list = [task_type]
        if status:
            query += " AND status = ?"
            params.append(status)
        return self.connection.execute(query, params).fetchall()

    def _run_rows(self, action_type: str):
        return self.connection.execute(
            "SELECT device_id, matched, difference_type FROM automation_runs"
            " WHERE action_type = ?",
            (action_type,),
        ).fetchall()

    def _persist_and_mark_projected(
        self, temperature: float = 24.0, humidity: float = 50.0
    ) -> int:
        """落一份样本并推进到「投影已成功、派发未发生」的崩溃现场。"""
        outcome = devices.persist_sample(
            "TH-10",
            devices.SOURCE_HOME_ASSISTANT,
            temperature,
            humidity,
            "在线",
        )
        self.assertIsNotNone(outcome.sample)
        sample_ms = int(outcome.sample_time_ms)
        projection.note_sample_persisted("TH-10", sample_ms)
        # 模拟：飞书写入成功 → mark projected →（崩溃）→ 未派发
        projection.mark_projection_success("TH-10", sample_ms)
        return sample_ms

    # ------------------------------------------------------------------
    # 1. 崩溃点：projection success 之后、dispatch 之前
    # ------------------------------------------------------------------

    def test_crash_between_projection_success_and_dispatch(self) -> None:
        sample_ms = self._persist_and_mark_projected()
        state = db.fetch_projection_state("TH-10")
        self.assertEqual(state["projection_status"], "ok")
        self.assertEqual(int(state["last_projected_sample_time_ms"]), sample_ms)
        self.assertIsNone(state["last_dispatched_sample_time_ms"])
        # 恢复不变量：projected > dispatched ⇒ 有未完成派发（无论 status）
        undispatched = db.fetch_undispatched_projection_states()
        self.assertEqual([row["device"] for row in undispatched], ["TH-10"])
        self.assertEqual(self.dispatched, [])

        # 重启后第一个 scheduler tick：自动补派发
        runtime = self.components.runtime
        runtime._ensure_projection_tasks(now=self._clock(1))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0].device_id, "TH-10")
        self.assertEqual(db.fetch_undispatched_projection_states(), [])

        # 恰好一个 SHADOW_COMPARE 任务并被执行
        self.assertEqual(len(self._task_rows("SHADOW_COMPARE")), 1)
        runtime.scheduler.run_once(now=self._clock(2))
        self.assertEqual(len(self._run_rows("SHADOW_COMPARE")), 1)

        # 再次恢复扫描 / 再次执行：无新副作用（幂等收尾）
        runtime._ensure_projection_tasks(now=self._clock(3))
        runtime.scheduler.run_once(now=self._clock(4))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(len(self._task_rows("SHADOW_COMPARE")), 1)
        self.assertEqual(len(self._run_rows("SHADOW_COMPARE")), 1)

    # ------------------------------------------------------------------
    # 2. 崩溃点：dispatch listener 已执行、派发水位落库前
    # ------------------------------------------------------------------

    def test_crash_after_dispatch_before_watermark_no_duplicate_effects(self) -> None:
        sample_ms = self._persist_and_mark_projected(
            temperature=30.0  # 超过上限 26 → VIOLATION，触发 VERIFY 动作
        )
        # 模拟崩溃现场：listener 已经执行（Runtime 已收到样本），
        # 但 last_dispatched_sample_time_ms 未落库
        row = db.fetch_device_sample_at(
            "TH-10", devices.SOURCE_HOME_ASSISTANT, sample_ms
        )
        devices.dispatch_sample(devices.sample_from_row("TH-10", row, sample_ms))
        self.assertEqual(len(self.dispatched), 1)
        self.assertIsNone(
            db.fetch_projection_state("TH-10")["last_dispatched_sample_time_ms"]
        )

        # 重启恢复：会重复投递一次（at-least-once）……
        projection.recover_pending_dispatches(now=self._clock(1))
        self.assertEqual(len(self.dispatched), 2)

        # ……但下游幂等：恰好一个 SHADOW_COMPARE、恰好一个 VERIFY_ALARM
        # （dedupe key），latest 样本 upsert 不重复
        self.assertEqual(len(self._task_rows("SHADOW_COMPARE")), 1)
        self.assertEqual(len(self._task_rows("VERIFY_ALARM")), 1)
        latest_rows = self.connection.execute(
            "SELECT COUNT(*) AS n FROM latest_monitor_samples WHERE device_id = ?",
            ("TH-10",),
        ).fetchone()
        self.assertEqual(latest_rows["n"], 1)

        runtime = self.components.runtime
        runtime.scheduler.run_once(now=self._clock(2))
        self.assertEqual(len(self._run_rows("SHADOW_COMPARE")), 1)
        # 状态机安全：重复样本仍停在 PENDING（校验窗口内），未重复建任务
        self.assertEqual(len(self._task_rows("VERIFY_ALARM")), 1)

    # ------------------------------------------------------------------
    # 3. status=ok 但 projected > dispatched：恢复与 status 无关
    # ------------------------------------------------------------------

    def test_ok_status_with_undispatched_projection_recovers(self) -> None:
        self._persist_and_mark_projected()
        state = db.fetch_projection_state("TH-10")
        self.assertEqual(state["projection_status"], "ok")
        self.assertIsNotNone(state["last_projected_sample_time_ms"])
        self.assertIsNone(state["last_dispatched_sample_time_ms"])

        # scheduler tick 挂钩（recover + ensure）能自动发现并完成
        self.components.runtime._ensure_projection_tasks(now=self._clock(1))
        state = db.fetch_projection_state("TH-10")
        self.assertEqual(
            int(state["last_dispatched_sample_time_ms"]),
            int(state["last_projected_sample_time_ms"]),
        )
        self.assertEqual(len(self.dispatched), 1)

    # ------------------------------------------------------------------
    # 4. pending + 重启：通过 runtime tick 挂钩恢复重试
    # ------------------------------------------------------------------

    def test_pending_projection_restart_recovery_via_runtime_tick(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            response = self.client.post(
                "/temperature",
                json={"device": "TH-10", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            db.fetch_projection_state("TH-10")["projection_status"], "pending"
        )

        runtime = self.components.runtime
        with (
            patch("services.projection.resolve_record_id", return_value="dev-10"),
            patch(
                "services.projection.update_feishu_fields",
                return_value={"code": 0, "msg": "ok"},
            ),
        ):
            runtime._ensure_projection_tasks(now=self._clock(61))
            report = runtime.scheduler.run_once(now=self._clock(62))

        self.assertGreaterEqual(report.succeeded, 1)
        # retry handler 只 mark projected；派发由同一 tick 的 recovery 钩子补
        runtime._ensure_projection_tasks(now=self._clock(62.5))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(len(self._task_rows("SHADOW_COMPARE")), 1)
        self.assertEqual(
            db.fetch_projection_state("TH-10")["projection_status"], "ok"
        )

    # ------------------------------------------------------------------
    # 5. 11 台设备同时失败：无 scheduler 饿死、每 tick ≤1 投影尝试
    # ------------------------------------------------------------------

    def test_eleven_device_outage_does_not_starve_scheduler(self) -> None:
        runtime = self.components.runtime

        # 本用例需要完整的 backoff 时间轴：now_provider 是 lambda（动态读
        # self.now），把整个用例切到真实墙钟并由测试推进，保证
        # last_attempt_at / 扫描 / tick 三者同源（生产中同用真实墙钟）。
        base = datetime.now().astimezone()
        self.now = base

        # TH-10 先成功一次：HTTP 投影成功（不直接派发），由 recovery
        # 派发后产生一个待执行的 SHADOW_COMPARE 任务
        with (
            patch("routes.temperature.resolve_record_id", return_value="dev-10"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
        ):
            ok = self.client.post(
                "/temperature",
                json={"device": "TH-10", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(ok.status_code, 200)
        runtime._ensure_projection_tasks(now=base + timedelta(seconds=1))
        self.assertEqual(len(self._task_rows("SHADOW_COMPARE")), 1)

        # 11 台设备（含 TH-10 的第二个样本）同时进入 projection failure
        for index in range(1, 12):
            device = f"TH-{index:02d}"
            outcome = devices.persist_sample(
                device,
                devices.SOURCE_HOME_ASSISTANT,
                24.0,
                50.0,
                "在线",
            )
            projection.note_sample_persisted(device, int(outcome.sample_time_ms))
            projection.mark_projection_failure(device, "simulated outage")
        self.assertEqual(len(db.fetch_projection_states(status="pending")), 11)

        def _tick(seconds: float):
            self.now = base + timedelta(seconds=seconds)
            return self.now

        # 退避到期（30s）：扫描器错峰创建 11 个 durable 任务。
        # 错峰相对扫描时刻摊开（每台 +2s）：配合 1s poll，每个 tick 至多
        # 认领 1 个投影任务 —— 这是 scheduler 不被饿死的核心机制。
        runtime._ensure_projection_tasks(now=_tick(31))
        pending = self._task_rows("FEISHU_PROJECTION", "PENDING")
        self.assertEqual(len(pending), 11)

        with (
            patch("services.projection.resolve_record_id", side_effect=_connection_error),
            patch(
                "services.projection.update_feishu_fields",
                side_effect=_connection_error,
            ),
        ):
            # 第一个 tick：SYNC×2 + SHADOW_COMPARE + 1 个投影尝试，
            # 全部非投影任务正常执行 —— 无饿死
            report = runtime.scheduler.run_once(now=_tick(31))
            self.assertGreaterEqual(report.succeeded, 3)
            self.assertEqual(report.failed, 1)
            self.assertEqual(len(self._run_rows("SHADOW_COMPARE")), 1)

            # 后续 tick（每 +2s）：每个 tick 恰好 1 个有界投影尝试
            per_tick_attempts: list[int] = [1]  # tick 1 已尝试 1 个
            for tick in range(1, 11):
                before = len(self._task_rows("FEISHU_PROJECTION", "FAILED"))
                runtime.scheduler.run_once(now=_tick(31 + tick * 2))
                after = len(self._task_rows("FEISHU_PROJECTION", "FAILED"))
                per_tick_attempts.append(after - before)

        self.assertEqual(sum(per_tick_attempts), 11)
        self.assertEqual(max(per_tick_attempts), 1)
        self.assertEqual(self._task_rows("FEISHU_PROJECTION", "PENDING"), [])
        # SYNC_STANDARD 在 11 个投影重试期间仍被调度执行（未被饿死的
        # 直接证据：tick 1 与首个投影尝试同批 SUCCEEDED）
        self.assertGreaterEqual(
            len(self._task_rows("SYNC_STANDARD", "SUCCEEDED")), 1
        )
        self.assertGreaterEqual(
            len(self._task_rows("SYNC_OPERATIONS", "SUCCEEDED")), 1
        )

        # 无 retry storm：第二轮 backoff（60s）内不再生成新任务
        runtime._ensure_projection_tasks(now=_tick(55))
        self.assertEqual(self._task_rows("FEISHU_PROJECTION", "PENDING"), [])
        # backoff 曲线不变：30 / 60 / 120 / …
        self.assertEqual(projection.retry_backoff_seconds(0), 30.0)
        self.assertEqual(projection.retry_backoff_seconds(1), 60.0)
        self.assertEqual(projection.retry_backoff_seconds(2), 120.0)

        # 到达第二轮 backoff 后：每设备仍至多一个未完成任务
        runtime._ensure_projection_tasks(now=_tick(130))
        self.assertEqual(len(self._task_rows("FEISHU_PROJECTION", "PENDING")), 11)


if __name__ == "__main__":
    unittest.main()
