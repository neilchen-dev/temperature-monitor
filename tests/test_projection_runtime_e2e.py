"""/temperature → 投影重试 → Shadow 全链路集成测试（真实 runtime）。

复现生产事故链路并验证恢复语义：
1. 飞书故障期间 HA 上报 → 200 accepted（sample 已本地持久化，无 Shadow 派发）。
2. runtime scheduler 扫描 pending → durable FEISHU_PROJECTION 任务 → 重试成功。
3. 投影成功后才派发 Runtime/Shadow → 恰好一个 SHADOW_COMPARE → automation_runs。
4. 全链路恰好一次副作用（一份 sample / 一次飞书写 / 一次 compare）。
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
from services import db, devices


TZ = ZoneInfo("Asia/Shanghai")


def _std_record(record_id: str, area: str, device: str, temp_max: float) -> FeishuRawRecord:
    return FeishuRawRecord(
        record_id=record_id,
        fields={
            "标准编号": f"ENV-{device}",
            "版本": "Rev.A",
            "适用区域": area,
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
                _std_record("std-10", "PE仓库", "TH-10", 26),
            ],
            config.FEISHU_OPERATION_TABLE_ID: [],
            config.FEISHU_EVENT_TABLE_ID: [],
            "device-table": [_dev_record("dev-10", "TH-10")],
        }

    def read_records(self, table_id: str):
        return tuple(self.records.get(table_id, ()))


def _connection_error(*_args, **_kwargs):
    raise requests.exceptions.ConnectionError("feishu unreachable")


class ProjectionRuntimeE2ETests(unittest.TestCase):
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
                "TEMPERATURE_DEDUPE_WINDOW_MS",
                "FEISHU_PROJECTION_MAX_RETRIES",
                "FEISHU_PROJECTION_BACKOFF_SECONDS",
                "FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS",
                "TEMPERATURE_API_KEY",
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = ("TH-10",)
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "e2e.db"
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        config.DEVICE_NAME_MAP = {}
        config.DEVICES = {}
        config.TEMPERATURE_API_KEY = ""
        config.TEMPERATURE_DEDUPE_WINDOW_MS = 5000
        config.FEISHU_PROJECTION_MAX_RETRIES = 3
        config.FEISHU_PROJECTION_BACKOFF_SECONDS = 30.0
        config.FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS = 0.0
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

        # start() without the scheduler thread (manual run_once below).
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
        """扫描/调度时钟：路由与投影状态机用真实墙钟记录 last_attempt_at，
        退避到期判定必须基于真实时间的未来偏移。"""
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

    def test_outage_to_recovery_full_chain(self) -> None:
        runtime = self.components.runtime

        # --- 1. 飞书故障：HA 上报被接受，sample 已持久化，未派发 ---
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
        self.assertEqual(response.get_json()["feishu_projection"], "deferred")
        samples = db.fetch_device_samples("TH-10", limit=10)
        self.assertEqual(len(samples), 1)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(
            db.fetch_projection_state("TH-10")["projection_status"], "pending"
        )

        # --- 2. 退避到期：scheduler 扫描创建 durable 重试任务 ---
        runtime._ensure_projection_tasks(now=self._clock(61))
        pending = self._task_rows("FEISHU_PROJECTION", "PENDING")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["entity_id"], "TH-10")

        # --- 3. 飞书恢复：重试成功 → 先投影后派发 ---
        with (
            patch("services.projection.resolve_record_id", return_value="dev-10"),
            patch(
                "services.projection.update_feishu_fields",
                return_value={"code": 0, "msg": "ok"},
            ) as retry_update,
        ):
            report = runtime.scheduler.run_once(now=self._clock(62))
        self.assertGreaterEqual(report.succeeded, 1)
        retry_update.assert_called_once()
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0].device_id, "TH-10")
        # SHADOW_COMPARE 任务恰好一个（同 sample 只比对一次）
        compare_tasks = self._task_rows("SHADOW_COMPARE")
        self.assertEqual(len(compare_tasks), 1)

        # --- 4. 执行比对：进入 automation_runs ---
        self.now = self.now + timedelta(seconds=1)
        runtime.scheduler.run_once(now=self._clock(63))
        runs = self.connection.execute(
            "SELECT device_id, matched, difference_type FROM automation_runs"
            " WHERE action_type = 'SHADOW_COMPARE'"
        ).fetchall()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["device_id"], "TH-10")
        self.assertEqual(runs[0]["matched"], 1)

        # --- 5. 幂等收尾：再次扫描/执行不产生新副作用 ---
        runtime._ensure_projection_tasks(now=self._clock(130))
        runtime.scheduler.run_once(now=self._clock(131))
        self.assertEqual(len(self._task_rows("SHADOW_COMPARE")), 1)
        self.assertEqual(len(db.fetch_device_samples("TH-10", limit=10)), 1)
        self.assertEqual(
            db.fetch_projection_state("TH-10")["projection_status"], "ok"
        )

    def test_outage_sample_without_projection_stays_undispatched(self) -> None:
        """飞书持续故障：pending 有界重试，Shadow 不被必然错误的 compare 污染。"""
        runtime = self.components.runtime
        config.FEISHU_PROJECTION_MAX_RETRIES = 2

        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self.client.post(
                "/temperature",
                json={"device": "TH-10", "temperature": 24.0, "humidity": 50.0},
            )

        with (
            patch("services.projection.resolve_record_id", side_effect=_connection_error),
            patch("services.projection.update_feishu_fields", side_effect=_connection_error),
        ):
            for tick in range(2):
                runtime._ensure_projection_tasks(now=self._clock(61 + tick * 61))
                runtime.scheduler.run_once(now=self._clock(62 + tick * 61))

        # 重试耗尽 → failed 终态；期间从未派发、从未产生 SHADOW_COMPARE
        state = db.fetch_projection_state("TH-10")
        self.assertEqual(state["projection_status"], "failed")
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self._task_rows("SHADOW_COMPARE"), [])
        # 任务全部终态（无无限 PENDING）
        self.assertEqual(self._task_rows("FEISHU_PROJECTION", "PENDING"), [])
        failed_rows = self._task_rows("FEISHU_PROJECTION", "FAILED")
        self.assertEqual(len(failed_rows), 2)


if __name__ == "__main__":
    unittest.main()
