"""Dispatch ownership 并发专项：验证 AB-BA 锁反转已消除（2026-09-02 生产死锁）。

生产事故回放（修复前）：
- Waitress HTTP 线程：/temperature 成功 → dispatch_projected_sample →
  持有 _dispatch_lock → listener → handle_sample 等待 _execution_lock
- Shadow scheduler 线程：持 _execution_lock → run_once 的
  FEISHU_PROJECTION handler / finally 的 recover_pending_dispatches →
  dispatch_projected_sample 等待 _dispatch_lock
- 经典 AB-BA：scheduler 停转（SYNC_OPERATIONS 永久 PENDING），
  HTTP 线程继续投影但 Shadow 不再比对。

修复后的不变量（本文件用真实线程验证）：
1. HTTP 投影路径不调用 dispatch，不触碰任何 runtime 锁。
2. scheduler 线程是 HA projection → Runtime dispatch 的唯一 owner
   （recover_pending_dispatches，每 tick）。
3. 投影成功后派发延迟 ≤ 一个 poll interval，业务副作用恰好一次。
4. 任意线程组合都不会以相反顺序获取两把锁（_dispatch_lock 已删除，
   _execution_lock 是 runtime 中唯一的锁）。
"""

from __future__ import annotations

import inspect
import sqlite3
import tempfile
import threading
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
DEVICES = ("TH-01", "TH-03", "TH-05")


def _std_record(device: str) -> FeishuRawRecord:
    return FeishuRawRecord(
        record_id=f"std-{device}",
        fields={
            "标准编号": f"ENV-{device}",
            "版本": "Rev.A",
            "适用区域": "测试区",
            "适用设备": device,
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
            "条款": "1.0",
        },
        created_at=datetime(2026, 1, 1, tzinfo=TZ),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
    )


def _dev_record(device: str) -> FeishuRawRecord:
    return FeishuRawRecord(
        record_id=f"dev-{device}",
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
    def __init__(self) -> None:
        self.records: dict[str, list[FeishuRawRecord]] = {
            config.FEISHU_STANDARD_TABLE_ID: [_std_record(d) for d in DEVICES],
            config.FEISHU_OPERATION_TABLE_ID: [],
            config.FEISHU_EVENT_TABLE_ID: [],
            "device-table": [_dev_record(d) for d in DEVICES],
        }

    def read_records(self, table_id: str):
        return tuple(self.records.get(table_id, ()))


def _connection_error(*_args, **_kwargs):
    raise requests.exceptions.ConnectionError("feishu unreachable")


class DispatchOwnershipTests(unittest.TestCase):
    """真实双线程：HTTP projection 与 scheduler tick 同时推进，不死锁。"""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
                "TEMPERATURE_API_KEY",
                "FEISHU_PROJECTION_MAX_RETRIES",
                "FEISHU_PROJECTION_BACKOFF_SECONDS",
                "FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS",
            )
        }
        config.AUTOMATION_MODE = "shadow"
        config.SHADOW_DEVICE_IDS = DEVICES
        config.APP_ID = "app"
        config.APP_SECRET = "secret"
        config.APP_TOKEN = "token"
        config.FEISHU_DEVICE_TABLE_ID = "device-table"
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "ownership.db"
        config.HISTORY_TIMEZONE = "Asia/Shanghai"
        config.AUTOMATION_RUN_RETENTION_DAYS = 30
        config.DEVICE_NAME_MAP = {}
        config.DEVICES = {}
        config.TEMPERATURE_API_KEY = ""
        config.TEMPERATURE_DEDUPE_WINDOW_MS = 5000
        config.FEISHU_PROJECTION_MAX_RETRIES = 5
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
            now_provider=lambda: datetime.now().astimezone(),
        )
        self.addCleanup(self._cleanup_runtime)
        self.components.runtime.handle_standard_sync(object())

        # 高频真实 scheduler 线程（生产 _run_scheduler 路径，非 mock）。
        self.components.runtime.scheduler.poll_interval = 0.02
        self.components.runtime.start()
        self.assertTrue(self.components.runtime.status()["available"])

        self.dispatched: list = []
        self._dispatched_lock = threading.Lock()
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
        with self._dispatched_lock:
            self.dispatched.append(sample)

    def _dispatched_count(self, device: str | None = None) -> int:
        with self._dispatched_lock:
            if device is None:
                return len(self.dispatched)
            return sum(1 for s in self.dispatched if s.device_id == device)

    def _future(self, seconds: float = 600.0) -> datetime:
        return datetime.now().astimezone() + timedelta(seconds=seconds)

    def _wait_until(self, predicate, timeout: float = 5.0) -> bool:
        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            if predicate():
                return True
            threading.Event().wait(0.02)
        return predicate()

    def _undispatched(self) -> list:
        return db.fetch_undispatched_projection_states()

    def _task_count(self, task_type: str, status: str | None = None) -> int:
        query = (
            "SELECT COUNT(*) AS n FROM automation_tasks WHERE task_type = ?"
        )
        params: list = [task_type]
        if status:
            query += " AND status = ?"
            params.append(status)
        return self.connection.execute(query, params).fetchone()["n"]

    # ------------------------------------------------------------------
    # 1-3. 并发压力：真实线程，join 超时即死锁暴露
    # ------------------------------------------------------------------

    def _run_http_threads(self, post_fn, *, threads: int = 3) -> list:
        """Run N real HTTP threads; returns collected errors."""
        errors: list = []
        barrier = threading.Barrier(threads + 1)

        def _worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                post_fn(index)
            except Exception as exc:  # noqa: BLE001 - collect, assert later
                errors.append(exc)

        workers = [
            threading.Thread(
                target=_worker, args=(i,), name=f"http-{i}", daemon=True
            )
            for i in range(threads)
        ]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=10)  # 同时放行，最大化锁窗口交叠
        for worker in workers:
            worker.join(timeout=30)
        # 任何线程 join 超时 = 死锁（修复前在此红）
        self.assertFalse(
            any(worker.is_alive() for worker in workers),
            "HTTP 线程卡死：dispatch 锁反转回归",
        )
        return errors

    def test_http_projection_concurrent_with_scheduler_recovery(self) -> None:
        """场景1：HTTP 投影成功与 scheduler recovery/retry 同时发生。

        预置 pending + FEISHU_PROJECTION 任务，使 scheduler 每 tick 都
        执行 retry handler（旧代码中其 dispatch 要拿 _dispatch_lock），
        同时 HTTP 线程高频 POST（旧代码成功路径 dispatch 先拿
        _dispatch_lock 再等 _execution_lock）——两线程都必须有限时结束。
        """
        # 预置：TH-05 投影失败 → pending + durable retry 任务（已到期）
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            first = self.client.post(
                "/temperature",
                json={"device": "TH-05", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(first.status_code, 200)
        self.components.runtime._ensure_projection_tasks(now=self._future(61))
        self.assertEqual(self._task_count("FEISHU_PROJECTION", "PENDING"), 1)

        # 飞书恢复（route + retry 两条路径都成功）
        def _post_round(index: int) -> None:
            for i in range(10):
                response = self.client.post(
                    "/temperature",
                    json={
                        "device": DEVICES[index % len(DEVICES)],
                        "temperature": 24.0 + index * 0.1 + i * 0.01,
                        "humidity": 50.0,
                    },
                )
                self.assertEqual(response.status_code, 200)

        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ),
            patch("routes.temperature.save_history"),
            patch("services.projection.resolve_record_id", return_value="rec_01"),
            patch(
                "services.projection.update_feishu_fields",
                return_value={"code": 0, "msg": "ok"},
            ),
        ):
            errors = self._run_http_threads(_post_round, threads=3)

        self.assertEqual(errors, [])
        # scheduler 仍在推进且派发收敛：undispatched 最终归零。
        self.assertTrue(
            self._wait_until(lambda: len(self._undispatched()) == 0, timeout=5),
            f"undispatched 未收敛: {self._undispatched()}",
        )
        # SYNC_OPERATIONS 未被饿死/停转（生产事故的直接症状）
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SYNC_OPERATIONS", "SUCCEEDED") >= 1,
                timeout=5,
            ),
            "SYNC_OPERATIONS 停转：scheduler 死锁回归",
        )
        # 投影成功的设备都已派发（TH-05 的 pending 也被 retry 收敛）
        for device in DEVICES:
            self.assertGreaterEqual(self._dispatched_count(device), 1)

    def test_http_multi_device_projection_with_sync_operations(self) -> None:
        """场景2：HTTP 连续多设备投影 + scheduler SYNC_OPERATIONS，无死锁。"""
        # 等首个 SYNC 周期任务被创建并执行
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SYNC_OPERATIONS", "SUCCEEDED") >= 1,
                timeout=5,
            )
        )

        def _post_round(index: int) -> None:
            for i in range(12):
                response = self.client.post(
                    "/temperature",
                    json={
                        "device": DEVICES[(index + i) % len(DEVICES)],
                        "temperature": 23.0 + i * 0.1,
                        "humidity": 50.0 + index,
                    },
                )
                self.assertEqual(response.status_code, 200)

        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ),
            patch("routes.temperature.save_history"),
        ):
            errors = self._run_http_threads(_post_round, threads=3)

        self.assertEqual(errors, [])
        self.assertTrue(
            self._wait_until(lambda: len(self._undispatched()) == 0, timeout=5)
        )
        # 多轮 SYNC 持续执行（scheduler 没被 HTTP 拖死）
        self.assertGreaterEqual(
            self._task_count("SYNC_OPERATIONS", "SUCCEEDED"), 1
        )

    def test_http_multi_device_projection_with_shadow_compare(self) -> None:
        """场景3：HTTP 连续多设备投影 + scheduler SHADOW_COMPARE，无死锁。"""
        # 先派发一轮，制造待执行的 SHADOW_COMPARE 任务
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ),
            patch("routes.temperature.save_history"),
        ):
            for device in DEVICES:
                self.client.post(
                    "/temperature",
                    json={"device": device, "temperature": 24.0, "humidity": 50.0},
                )
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SHADOW_COMPARE") >= len(DEVICES),
                timeout=5,
            ),
            "首轮派发未生成 SHADOW_COMPARE",
        )

        def _post_round(index: int) -> None:
            for i in range(10):
                response = self.client.post(
                    "/temperature",
                    json={
                        "device": DEVICES[(index + i) % len(DEVICES)],
                        "temperature": 24.5 + i * 0.1 + index * 0.01,
                        "humidity": 51.0 + index,
                    },
                )
                self.assertEqual(response.status_code, 200)

        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ),
            patch("routes.temperature.save_history"),
        ):
            errors = self._run_http_threads(_post_round, threads=3)

        self.assertEqual(errors, [])
        # 比对持续产生（scheduler 活着）
        self.assertTrue(
            self._wait_until(
                lambda: self.connection.execute(
                    "SELECT COUNT(*) AS n FROM automation_runs"
                    " WHERE action_type = 'SHADOW_COMPARE'"
                ).fetchone()["n"]
                >= len(DEVICES),
                timeout=5,
            ),
            "并发期间 SHADOW_COMPARE 未执行",
        )
        self.assertTrue(
            self._wait_until(lambda: len(self._undispatched()) == 0, timeout=5)
        )

    # ------------------------------------------------------------------
    # 4-8. ownership 语义（单 owner / 延迟 / 幂等 / 重启 / retry）
    # ------------------------------------------------------------------

    def test_projected_ahead_of_dispatched_recovered_next_tick(self) -> None:
        """场景4：projected > dispatched ⇒ 下一个 tick 自动派发。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ),
            patch("routes.temperature.save_history"),
        ):
            response = self.client.post(
                "/temperature",
                json={"device": "TH-01", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(response.status_code, 200)
        # HTTP 成功 ≠ 派发：水位差距可见
        state = db.fetch_projection_state("TH-01")
        self.assertIsNotNone(state["last_projected_sample_time_ms"])
        self.assertIsNone(state["last_dispatched_sample_time_ms"])
        self.assertEqual(self._dispatched_count("TH-01"), 0)
        # 下一个 tick（真实 scheduler 线程，poll=0.02s）自动补派发
        self.assertTrue(
            self._wait_until(
                lambda: self._dispatched_count("TH-01") == 1, timeout=2
            ),
            "一个 poll interval 内未补派发",
        )
        self.assertEqual(len(self._undispatched()), 0)

    def test_http_success_dispatches_within_one_poll_interval(self) -> None:
        """场景5：投影成功后 ≤1 poll interval 生成 SHADOW_COMPARE 任务。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ),
            patch("routes.temperature.save_history"),
        ):
            response = self.client.post(
                "/temperature",
                json={"device": "TH-03", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SHADOW_COMPARE") >= 1, timeout=2
            ),
            "一个 poll interval 内未生成 SHADOW_COMPARE",
        )

    def test_duplicate_projection_single_business_compare(self) -> None:
        """场景6：同一 projected sample 最终最多一个业务 SHADOW_COMPARE。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01")
            as resolve,
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ) as update,
            patch("routes.temperature.save_history"),
        ):
            first = self.client.post(
                "/temperature",
                json={"device": "TH-05", "temperature": 24.0, "humidity": 50.0},
            )
            duplicate = self.client.post(
                "/temperature",
                json={"device": "TH-05", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        # 重复请求被投影水位短路：飞书只写一次
        resolve.assert_called_once()
        update.assert_called_once()
        # 等待派发收敛 + 比对执行完
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SHADOW_COMPARE") >= 1, timeout=2
            )
        )
        # 恰好一个业务 compare（dedupe key: device + sample_time）
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SHADOW_COMPARE", "SUCCEEDED") >= 1,
                timeout=2,
            )
        )
        self.assertEqual(self._task_count("SHADOW_COMPARE"), 1)
        self.assertEqual(self._dispatched_count("TH-05"), 1)

    def test_restart_after_projection_still_dispatches_via_recovery(self) -> None:
        """场景7：投影成功后进程重启，派发仍收敛且幂等。

        真实 scheduler 线程（poll=0.02s）可能在断言前已补派发——这本身
        正是修复后的预期行为。本用例验证的是跨"重启"的最终语义：
        undispatched 归零、恰好派发一次、重复 recovery 不重复派发。
        """
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", return_value={"code": 0}
            ),
            patch("routes.temperature.save_history"),
        ):
            response = self.client.post(
                "/temperature",
                json={"device": "TH-01", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(response.status_code, 200)
        # 模拟重启：停掉 runtime（scheduler 线程退出），水位仍在 mirror db
        self.components.runtime.stop()
        # 重启前后的 recovery 都保证收敛：恰好派发一次、不重复
        self.assertTrue(
            self._wait_until(
                lambda: (
                    len(self._undispatched()) == 0
                    and self._dispatched_count("TH-01") == 1
                ),
                timeout=2,
            ),
            "重启后派发未收敛",
        )
        state = db.fetch_projection_state("TH-01")
        self.assertEqual(
            int(state["last_projected_sample_time_ms"]),
            int(state["last_dispatched_sample_time_ms"]),
        )
        # 重启后（scheduler 已停）再跑 recovery：幂等，不产生第二次派发
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-01"), 1)

    def test_feishu_projection_retry_success_does_not_dispatch_inline(self) -> None:
        """场景8：FEISHU_PROJECTION retry 成功不直接派发（单一 owner）。

        真实 scheduler 线程在场时会在 0.02s 内补派发，"未派发窗口"无法
        观察——因此本用例先停掉 scheduler 线程再做确定性验证：retry
        handler 若直接派发，dispatched 会立即 +1（修复前行为）。
        """
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            deferred = self.client.post(
                "/temperature",
                json={"device": "TH-03", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(deferred.status_code, 200)
        self.components.runtime._ensure_projection_tasks(now=self._future(61))
        self.assertEqual(self._task_count("FEISHU_PROJECTION", "PENDING"), 1)

        # 停掉 scheduler 线程：retry 期间无并发派发者
        self.components.runtime.stop()

        # retry 成功：只推进投影水位，不直接派发（确定性断言）
        with (
            patch("services.projection.resolve_record_id", return_value="rec_01"),
            patch(
                "services.projection.update_feishu_fields",
                return_value={"code": 0, "msg": "ok"},
            ),
        ):
            result = projection.retry_device_projection("TH-03")
        self.assertEqual(result["result"], "projected")
        self.assertEqual(result["dispatch"], "scheduler_recovery")
        self.assertNotIn("shadow_dispatched", result)
        state = db.fetch_projection_state("TH-03")
        self.assertIsNotNone(state["last_projected_sample_time_ms"])
        self.assertEqual(self._dispatched_count("TH-03"), 0)
        # recovery 补派发恰好一次；重复 recovery 幂等
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-03"), 1)
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-03"), 1)

    # ------------------------------------------------------------------
    # 9-10. 结构性守卫：防止 dispatch 直接路径被加回 HTTP / 锁被复活
    # ------------------------------------------------------------------

    def test_http_route_source_never_dispatches(self) -> None:
        """HTTP 路由源码不得包含任何 dispatch 调用（单一 owner 结构守卫）。"""
        from routes import temperature as temperature_module

        source = inspect.getsource(temperature_module)
        self.assertNotIn(
            "dispatch_projected_sample",
            source,
            "HTTP 路径重新引入直接 dispatch：AB-BA 死锁风险回归",
        )

    def test_projection_module_has_no_dispatch_lock(self) -> None:
        """projection 模块不得重新引入 dispatch 进程锁（锁序守卫）。

        用属性检查而不是源码字符串匹配：模块 docstring 记录了死锁
        历史（含旧锁名），字符串匹配会误报。
        """
        self.assertFalse(
            hasattr(projection, "_dispatch_lock"),
            "projection 重新引入 _dispatch_lock：AB-BA 死锁风险回归",
        )


if __name__ == "__main__":
    unittest.main()
