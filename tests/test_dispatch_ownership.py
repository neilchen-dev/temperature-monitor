"""Dispatch ownership 并发专项：验证 AB-BA 锁反转已消除（2026-09-02 生产死锁）。

生产事故回放（修复前）：
- Waitress HTTP 线程：/temperature 成功 → dispatch_projected_sample →
  持有 _dispatch_lock → listener → handle_sample 等待 _execution_lock
- Shadow scheduler 线程：持 _execution_lock → run_once 的
  FEISHU_PROJECTION handler / finally 的 recover_pending_dispatches →
  dispatch_projected_sample 等待 _dispatch_lock
- 经典 AB-BA：scheduler 停转（SYNC_OPERATIONS 永久 PENDING），
  HTTP 线程继续投影但 Shadow 不再比对。

测试结构（两类，刻意分开）：

LiveSchedulerConcurrencyTests —— 真实双线程压力：
  后台 scheduler 线程（0.05s 轮询，生产 _run_scheduler 路径）+
  多个真实 HTTP 线程同时 POST。只做**收敛断言**（_wait_until），
  不做任何窗口断言（"此刻尚未派发/仍是 PENDING"之类与后台线程
  竞争的断言在慢速 CI 上必然抖动）。所有触达飞书的路径在产生
  pending 状态之前就已 mock —— 后台线程绝无真实网络调用。

SchedulerOwnedDispatchTests —— 单 owner 语义（确定性）：
  e2e 模式：start() 时把 _run_scheduler 打桩为纯等待（监听器注册、
  _accepting_samples=True，但**没有后台线程**），recovery / run_once
  全部手动驱动。窗口断言（projected>dispatched 可观察、handler 不
  直接派发）在此确定性执行。
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


class _OwnershipTestBase(unittest.TestCase):
    """公共装配：隔离配置、mirror db（临时文件）、runtime、监听器。"""

    # 子类覆盖：Live 类用真实线程；Semantics 类打桩掉调度线程。
    live_scheduler = False

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
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.components = build_runtime(
            connection=self.connection,
            record_source=self.source,
            now_provider=lambda: datetime.now().astimezone(),
        )
        self.addCleanup(self._cleanup_runtime)
        self.components.runtime.handle_standard_sync(object())

        if self.live_scheduler:
            # 真实调度线程（生产 _run_scheduler 路径，非 mock）。
            self.components.runtime.scheduler.poll_interval = 0.05
            self.components.runtime.start()
        else:
            # e2e 模式：注册监听器与 _accepting_samples，但无后台线程。
            with patch.object(
                self.components.runtime,
                "_run_scheduler",
                side_effect=lambda stop_event: stop_event.wait(),
            ):
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

    def _wait_until(self, predicate, timeout: float = 10.0) -> bool:
        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            if predicate():
                return True
            threading.Event().wait(0.02)
        return predicate()

    def _undispatched(self) -> list:
        return db.fetch_undispatched_projection_states()

    def _task_count(self, task_type: str, status: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM automation_tasks WHERE task_type = ?"
        params: list = [task_type]
        if status:
            query += " AND status = ?"
            params.append(status)
        return self.connection.execute(query, params).fetchone()["n"]

    def _run_http_threads(self, post_fn, *, threads: int = 3) -> list:
        """Run N real HTTP threads; returns collected errors.

        join 超时 = 死锁暴露（修复前在旧代码上 3 线程全部卡死）。
        worker 设为 daemon：死锁回归时进程仍可退出、报告失败。
        """
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
        self.assertFalse(
            any(worker.is_alive() for worker in workers),
            "HTTP 线程卡死：dispatch 锁反转回归",
        )
        return errors


class LiveSchedulerConcurrencyTests(_OwnershipTestBase):
    """真实双线程并发：HTTP projection × scheduler tick（recovery/retry/compare）。"""

    live_scheduler = True

    def test_projection_success_concurrent_with_scheduler_recovery_and_retry(self) -> None:
        """场景1：HTTP 投影成功与 scheduler recovery/retry 同时发生。

        制造 pending 状态后，后台线程会：建 FEISHU_PROJECTION 任务 →
        claim → retry → mark projected → 同 tick recovery 补派发。
        全程与 3 个 HTTP 线程的 30 次 POST 交叠——两线程都必须在
        有限时间内结束（修复前在此死锁：3 线程 join 全部超时）。

        注意：services.projection 的 mock 在产生 pending 之前就已生效，
        后台 retry 绝无真实网络调用。
        """
        with (
            patch("services.projection.resolve_record_id", return_value="rec_01"),
            patch(
                "services.projection.update_feishu_fields",
                return_value={"code": 0, "msg": "ok"},
            ),
        ):
            # 内联投影失败 → deferred（pending 状态驱动后台 retry 路径）
            with (
                patch(
                    "routes.temperature.resolve_record_id",
                    side_effect=_connection_error,
                ),
                patch("routes.temperature.update_feishu_fields"),
                patch("routes.temperature.save_history"),
            ):
                deferred = self.client.post(
                    "/temperature",
                    json={"device": "TH-05", "temperature": 24.0, "humidity": 50.0},
                )
            self.assertEqual(deferred.status_code, 200)
            self.assertEqual(
                deferred.get_json()["feishu_projection"], "deferred"
            )

            # HTTP 线程（飞书恢复正常）：成功路径 × 后台 recovery/retry 并发
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
                    "routes.temperature.update_feishu_fields",
                    return_value={"code": 0},
                ),
                patch("routes.temperature.save_history"),
            ):
                errors = self._run_http_threads(_post_round, threads=3)

        self.assertEqual(errors, [])
        # scheduler 仍在推进且派发收敛：undispatched 最终归零。
        self.assertTrue(
            self._wait_until(lambda: len(self._undispatched()) == 0),
            f"undispatched 未收敛: {self._undispatched()}",
        )
        # SYNC_OPERATIONS 未被饿死/停转（生产事故的直接症状）
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SYNC_OPERATIONS", "SUCCEEDED") >= 1
            ),
            "SYNC_OPERATIONS 停转：scheduler 死锁回归",
        )
        # 所有设备的投影都已派发（TH-05 的 pending 也被 retry 收敛）
        for device in DEVICES:
            self.assertGreaterEqual(self._dispatched_count(device), 1)

    def test_http_multi_device_projection_with_sync_operations(self) -> None:
        """场景2：HTTP 连续多设备投影 + scheduler SYNC_OPERATIONS，无死锁。"""
        self.assertTrue(
            self._wait_until(
                lambda: self._task_count("SYNC_OPERATIONS", "SUCCEEDED") >= 1
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
        self.assertTrue(self._wait_until(lambda: len(self._undispatched()) == 0))
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
                lambda: self._task_count("SHADOW_COMPARE") >= len(DEVICES)
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
                >= len(DEVICES)
            ),
            "并发期间 SHADOW_COMPARE 未执行",
        )
        self.assertTrue(self._wait_until(lambda: len(self._undispatched()) == 0))

    def test_http_success_dispatches_within_one_poll_interval(self) -> None:
        """场景5：投影成功后 ≤1 poll interval 生成 SHADOW_COMPARE 任务。

        收敛断言（生成即可，不检查瞬时窗口）：poll=0.05s，等待上限 10s
        为 CI 抖动留足裕度。
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
                json={"device": "TH-03", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            self._wait_until(lambda: self._task_count("SHADOW_COMPARE") >= 1),
            "一个 poll interval 内未生成 SHADOW_COMPARE",
        )


class SchedulerOwnedDispatchTests(_OwnershipTestBase):
    """单 owner 语义（确定性）：无后台线程，recovery / run_once 手动驱动。"""

    live_scheduler = False

    def test_projected_ahead_of_dispatched_recovered_next_tick(self) -> None:
        """场景4：projected > dispatched ⇒ 下一个 tick（recovery）自动派发。"""
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

        # 确定性窗口断言：无任何派发者存在，HTTP 成功 ≠ 派发。
        state = db.fetch_projection_state("TH-01")
        self.assertIsNotNone(state["last_projected_sample_time_ms"])
        self.assertIsNone(state["last_dispatched_sample_time_ms"])
        self.assertEqual(len(self._undispatched()), 1)
        self.assertEqual(self._dispatched_count("TH-01"), 0)

        # 模拟下一个 scheduler tick 的 recovery：补派发一次
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-01"), 1)
        self.assertEqual(len(self._undispatched()), 0)
        state = db.fetch_projection_state("TH-01")
        self.assertEqual(
            int(state["last_projected_sample_time_ms"]),
            int(state["last_dispatched_sample_time_ms"]),
        )

        # 重复 recovery 幂等：不产生第二次派发
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-01"), 1)

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
        self.assertEqual(duplicate.get_json()["status"], "success")
        # 重复请求被投影水位短路：飞书只写一次
        resolve.assert_called_once()
        update.assert_called_once()
        # HTTP 从不派发（确定性）
        self.assertEqual(self._dispatched_count("TH-05"), 0)

        # recovery 派发一次 → handle_sample 创建 SHADOW_COMPARE 任务（恰好 1）
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-05"), 1)
        self.assertEqual(self._task_count("SHADOW_COMPARE"), 1)

        # 手动执行 compare（scheduler 语义）
        report = self.components.runtime.scheduler.run_once(now=self._future())
        self.assertGreaterEqual(report.succeeded, 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) AS n FROM automation_runs"
                " WHERE action_type = 'SHADOW_COMPARE'"
            ).fetchone()["n"],
            1,
        )
        # 重复 recovery 不再派发 → 不会出现第二个业务 compare
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._task_count("SHADOW_COMPARE"), 1)

    def test_projection_then_restart_recovery_dispatches_once(self) -> None:
        """场景7：mark projected 后"重启"，仍通过 recovery 补 dispatch。

        崩溃点 = mark projected 之后、dispatch 之前（水位差 durable 落在
        mirror db）。"重启后的第一个 tick" 即 recovery 扫描。
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
        # 崩溃点状态：projected 已标记、未派发（确定性——无派发者）
        self.assertEqual(self._dispatched_count("TH-01"), 0)
        state = db.fetch_projection_state("TH-01")
        self.assertGreater(
            int(state["last_projected_sample_time_ms"]),
            int(state["last_dispatched_sample_time_ms"] or 0),
        )

        # "重启"后的第一个 scheduler tick：recovery 补派发，恰好一次
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-01"), 1)
        state = db.fetch_projection_state("TH-01")
        self.assertEqual(
            int(state["last_projected_sample_time_ms"]),
            int(state["last_dispatched_sample_time_ms"]),
        )
        # 重复 recovery 幂等
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-01"), 1)

    def test_feishu_projection_retry_success_does_not_dispatch_inline(self) -> None:
        """场景8：FEISHU_PROJECTION retry 成功不直接派发（单一 owner）。"""
        with (
            patch(
                "routes.temperature.resolve_record_id", side_effect=_connection_error
            ),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            deferred = self.client.post(
                "/temperature",
                json={"device": "TH-03", "temperature": 24.0, "humidity": 50.0},
            )
        self.assertEqual(deferred.status_code, 200)

        # 手动维护钩子（无后台线程，确定性）：创建 retry 任务并保持 PENDING
        self.components.runtime._ensure_projection_tasks(now=self._future(61))
        self.assertEqual(self._task_count("FEISHU_PROJECTION", "PENDING"), 1)

        # retry 成功：只推进投影水位，不直接派发
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
        # handler 没有派发（确定性——无任何派发者存在）
        self.assertEqual(self._dispatched_count("TH-03"), 0)

        # recovery 补派发恰好一次；重复 recovery 幂等
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-03"), 1)
        projection.recover_pending_dispatches(now=self._future())
        self.assertEqual(self._dispatched_count("TH-03"), 1)

    def test_http_route_source_never_dispatches(self) -> None:
        """场景9（结构守卫）：HTTP 路由源码不得包含 dispatch 调用。"""
        from routes import temperature as temperature_module

        source = inspect.getsource(temperature_module)
        self.assertNotIn(
            "dispatch_projected_sample",
            source,
            "HTTP 路径重新引入直接 dispatch：AB-BA 死锁风险回归",
        )

    def test_projection_module_has_no_dispatch_lock(self) -> None:
        """场景10（结构守卫）：projection 模块不得重新引入 dispatch 进程锁。

        用属性检查而不是源码字符串匹配：模块 docstring 记录了死锁
        历史（含旧锁名），字符串匹配会误报。
        """
        self.assertFalse(
            hasattr(projection, "_dispatch_lock"),
            "projection 重新引入 _dispatch_lock：AB-BA 死锁风险回归",
        )


if __name__ == "__main__":
    unittest.main()
