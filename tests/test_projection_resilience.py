"""/temperature 可靠性专项测试：飞书故障 ≠ 原始 sample 丢失。

覆盖生产不变量：
- 有效 sample 一旦通过基本格式校验，本地持久化不依赖飞书（阶段A先行）。
- 飞书失败：sample 保留、projection failure 有 durable 证据、bounded retry、
  不立即触发必然错误的 Shadow compare。
- 幂等：HTTP/HA 重试、scheduler retry、服务重启都不重复副作用。
- offline 心跳与 ONLINE sample 统一处理但不混淆语义。
- HTTP 语义：本地已持久化时飞书失败返回 200 accepted/deferred。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import requests

import config
from app import create_app
from domain.models import MonitorSample
from repositories import connect
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from scheduler.worker import TaskScheduler
from services import db, devices, projection


def _connection_error(*_args, **_kwargs):
    raise requests.exceptions.ConnectionError("feishu unreachable")


def _read_timeout(*_args, **_kwargs):
    raise requests.exceptions.ReadTimeout("feishu read timeout")


def _ok(*_args, **_kwargs):
    return {"code": 0, "msg": "success"}


class ProjectionResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "DEVICE_NAME_MAP": config.DEVICE_NAME_MAP,
            "DEVICES": config.DEVICES,
            "TEMPERATURE_API_KEY": config.TEMPERATURE_API_KEY,
            "TEMPERATURE_DEDUPE_WINDOW_MS": config.TEMPERATURE_DEDUPE_WINDOW_MS,
            "FEISHU_PROJECTION_MAX_RETRIES": config.FEISHU_PROJECTION_MAX_RETRIES,
            "FEISHU_PROJECTION_BACKOFF_SECONDS": config.FEISHU_PROJECTION_BACKOFF_SECONDS,
            "FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS": (
                config.FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS
            ),
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "monitor.db"
        config.DEVICE_NAME_MAP = {}
        config.DEVICES = {}
        config.TEMPERATURE_API_KEY = ""
        config.TEMPERATURE_DEDUPE_WINDOW_MS = 5000
        config.FEISHU_PROJECTION_MAX_RETRIES = 2
        config.FEISHU_PROJECTION_BACKOFF_SECONDS = 30.0
        # 默认关闭内联抑制，保证各用例可显式尝试内联投影；
        # 抑制语义由专门用例验证。
        config.FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS = 0.0
        devices._reset_device_model_stats()

        self.dispatched: list[MonitorSample] = []
        devices.register_sample_listener(self._capture_sample)
        self.client = create_app().test_client()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        devices.unregister_sample_listener(self._capture_sample)
        devices._reset_device_model_stats()
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def _capture_sample(self, sample: MonitorSample) -> None:
        self.dispatched.append(sample)

    def _post(
        self,
        device: str = "TH-05",
        temperature: float | None = 24.6,
        humidity: float | None = 52.0,
        status: str | None = None,
    ):
        payload: dict = {"device": device}
        if temperature is not None:
            payload["temperature"] = temperature
        if humidity is not None:
            payload["humidity"] = humidity
        if status is not None:
            payload["status"] = status
        return self.client.post("/temperature", json=payload)

    def _samples(self, device: str = "TH-05") -> list[dict]:
        return db.fetch_device_samples(device, limit=100)

    def _state(self, device: str = "TH-05") -> dict | None:
        return db.fetch_projection_state(device)

    def _future_now(self, seconds: float = 600.0) -> datetime:
        return datetime.now().astimezone() + timedelta(seconds=seconds)

    # ------------------------------------------------------------------
    # A. 本地持久化
    # ------------------------------------------------------------------

    def test_feishu_success_persists_sample(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._samples()), 1)
        self.assertEqual(self._state()["projection_status"], "ok")

    def test_resolve_failure_still_persists(self) -> None:
        """resolve_record_id 网络失败：sample 必须已本地持久化。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "accepted")
        self.assertTrue(body["local_persisted"])
        self.assertEqual(body["feishu_projection"], "deferred")
        self.assertEqual(len(self._samples()), 1)
        self.assertEqual(self._state()["projection_status"], "pending")

    def test_update_timeout_still_persists(self) -> None:
        """update_feishu_fields Read timeout：sample 必须已本地持久化。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", side_effect=_read_timeout),
            patch("routes.temperature.save_history"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["feishu_projection"], "deferred")
        self.assertEqual(len(self._samples()), 1)
        self.assertEqual(self._state()["projection_status"], "pending")

    def test_repeated_failures_single_sample(self) -> None:
        """连续失败 + 重复请求（dedupe）：业务 sample 只有一份。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", side_effect=_connection_error),
            patch("routes.temperature.save_history"),
        ):
            first = self._post()
            second = self._post(temperature=24.6, humidity=52.0)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        rows = self._samples()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["temperature"], 24.6)

    def test_feishu_nonzero_code_also_defers(self) -> None:
        """飞书返回业务错误码（非异常）：同样保留本地 sample。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields",
                return_value={"code": 1254301, "msg": "field error"},
            ),
            patch("routes.temperature.save_history") as save_history,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["feishu_projection"], "deferred")
        self.assertEqual(len(self._samples()), 1)
        # 失败证据进入请求级审计日志
        self.assertIn(1254301, save_history.call_args.args)

    # ------------------------------------------------------------------
    # B. Shadow
    # ------------------------------------------------------------------

    def test_feishu_success_dispatches_shadow_once(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
        ):
            self._post()

        # HTTP 线程不派发（单一 dispatch owner = scheduler recovery）。
        self.assertEqual(self.dispatched, [])
        projection.recover_pending_dispatches(now=self._future_now())
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0].device_id, "TH-05")
        self.assertEqual(self.dispatched[0].temperature, 24.6)

    def test_feishu_failure_does_not_dispatch(self) -> None:
        """飞书失败：不得因 observed 未更新而触发必然错误的 Shadow compare。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()

        self.assertEqual(self.dispatched, [])

    def test_retry_success_dispatches_exactly_once(self) -> None:
        """投影重试成功后 Shadow 只触发一次；重复 retry 任务是无害 no-op。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()

        with (
            patch("services.projection.resolve_record_id", return_value="rec_01"),
            patch("services.projection.update_feishu_fields", return_value={"code": 0}),
        ):
            first = projection.retry_device_projection("TH-05")
            # 模拟重启后遗留/重复的 retry 任务再次执行
            second = projection.retry_device_projection("TH-05")

        self.assertEqual(first["result"], "projected")
        self.assertEqual(second["result"], "skipped")
        # retry handler 只 mark projected；派发由 recovery 统一完成。
        self.assertEqual(self.dispatched, [])
        projection.recover_pending_dispatches(now=self._future_now())
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self._state()["projection_status"], "ok")

    def test_dispatch_happens_after_feishu_write(self) -> None:
        """顺序不变量：先飞书投影（HTTP），后 Runtime/Shadow 派发（recovery）。"""
        events: list[str] = []

        def _update(*_args, **_kwargs):
            events.append("feishu")
            return {"code": 0}

        def _listener(_sample: MonitorSample) -> None:
            events.append("dispatch")

        devices.register_sample_listener(_listener)
        self.addCleanup(devices.unregister_sample_listener, _listener)
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", side_effect=_update),
            patch("routes.temperature.save_history"),
        ):
            self._post()

        # HTTP 路径只做投影；派发发生在投影成功之后（scheduler recovery）。
        self.assertEqual(events, ["feishu"])
        projection.recover_pending_dispatches(now=self._future_now())
        self.assertEqual(events, ["feishu", "dispatch"])

    # ------------------------------------------------------------------
    # C. 幂等
    # ------------------------------------------------------------------

    def test_duplicate_request_single_side_effect(self) -> None:
        """相同 sample 重复两次：一份 sample、一次投影、一次派发。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01") as resolve,
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}) as update,
            patch("routes.temperature.save_history"),
        ):
            first = self._post()
            second = self._post(temperature=24.6, humidity=52.0)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "success")
        self.assertEqual(len(self._samples()), 1)
        # 重复请求被投影水位短路：飞书只调一次。
        resolve.assert_called_once()
        update.assert_called_once()
        # 派发由 scheduler recovery 统一完成：恰好一次。
        projection.recover_pending_dispatches(now=self._future_now())
        self.assertEqual(len(self.dispatched), 1)

    def test_projection_retry_no_duplicate_feishu_write(self) -> None:
        """相同 projection retry：飞书副作用不重复。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()

        with (
            patch("services.projection.resolve_record_id", return_value="rec_01"),
            patch("services.projection.update_feishu_fields", side_effect=_ok) as retry_update,
        ):
            projection.retry_device_projection("TH-05")
            projection.retry_device_projection("TH-05")

        retry_update.assert_called_once()

    def test_restart_recovery_keeps_idempotent(self) -> None:
        """服务重启后存在待 retry task：仍保持幂等（一份 sample、一次投影、
        一次派发、一个任务消费）。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()
        self.assertEqual(self._state()["projection_status"], "pending")

        # —— 模拟重启：全新连接 + 全新任务仓储，状态/样本都在磁盘上 ——
        connection = connect(config.SQLITE_DB_PATH)
        self.addCleanup(connection.close)
        repository = SQLiteAutomationTaskRepository(connection)
        scheduler = TaskScheduler(
            repository=repository,
            handlers={
                "FEISHU_PROJECTION": lambda task: projection.retry_device_projection(
                    task.entity_id
                )
            },
            worker_id="restart-test",
        )

        with (
            patch("services.projection.resolve_record_id", return_value="rec_01"),
            patch("services.projection.update_feishu_fields", side_effect=_ok) as retry_update,
        ):
            # 重启后第一个 tick：扫描 pending → 建任务 → 执行成功
            projection.ensure_projection_tasks(repository, now=self._future_now())
            report = scheduler.run_once(now=self._future_now(610))
            self.assertEqual(report.succeeded, 1)
            # 重启后第二个 tick：遗留/重复扫描不再产生副作用
            projection.ensure_projection_tasks(repository, now=self._future_now(620))
            second = scheduler.run_once(now=self._future_now(630))
            self.assertEqual(second.claimed, 0)

        retry_update.assert_called_once()
        # retry handler 只 mark projected；派发由 recovery 统一补齐一次。
        self.assertEqual(self.dispatched, [])
        projection.recover_pending_dispatches(now=self._future_now(640))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(len(self._samples()), 1)
        self.assertEqual(self._state()["projection_status"], "ok")

    # ------------------------------------------------------------------
    # D. offline
    # ------------------------------------------------------------------

    def test_offline_failure_preserves_local_evidence(self) -> None:
        """offline 心跳在飞书失败时：本地离线证据（样本+事件）不丢。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
        ):
            self._post(temperature=25.0, humidity=50.0)

        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            response = self._post(status="offline", temperature=None, humidity=None)

        self.assertEqual(response.status_code, 200)
        rows = self._samples()
        self.assertEqual(len(rows), 2)
        latest = rows[0]
        self.assertEqual(latest["status"], "offline")
        self.assertIsNone(latest["temperature"])
        events = db.fetch_device_events(device_id="TH-05", limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "status_change")
        self.assertEqual(events[0]["old_state"], "ONLINE")
        self.assertEqual(events[0]["new_state"], "OFFLINE")

    def test_offline_report_does_not_fabricate_temperature(self) -> None:
        """离线上报即使携带温度字段，也不得伪造温湿度 sample。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", side_effect=_ok) as update,
            patch("routes.temperature.save_history"),
        ):
            self._post(temperature=25.0, humidity=50.0)
            self._post(status="offline", temperature=99.9, humidity=88.8)

        latest = self._samples()[0]
        self.assertEqual(latest["status"], "offline")
        self.assertIsNone(latest["temperature"])
        self.assertIsNone(latest["humidity"])
        # 飞书投影字段：离线只改在线状态（保留最后温湿度），与旧版一致
        self.assertEqual(update.call_args.args[1], {"在线状态": "离线"})

    # ------------------------------------------------------------------
    # E. HTTP 语义
    # ------------------------------------------------------------------

    def test_deferred_response_semantics(self) -> None:
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "accepted")
        self.assertTrue(body["local_persisted"])
        self.assertEqual(body["feishu_projection"], "deferred")
        self.assertEqual(body["device"], "TH-05")

    def test_client_retry_no_duplicate_side_effects(self) -> None:
        """抑制窗口内的客户端重试：不重复飞书调用、不重复 sample/派发。"""
        config.FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS = 30.0
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch(
                "routes.temperature.update_feishu_fields", side_effect=_connection_error
            ) as update,
            patch("routes.temperature.save_history"),
        ):
            first = self._post(temperature=24.6)
            # 30s 内的重试（同内容 → dedupe；不同内容 → 新样本但被抑制）
            retry_same = self._post(temperature=24.6)
            retry_new = self._post(temperature=25.0)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry_same.status_code, 200)
        self.assertEqual(retry_new.status_code, 200)
        self.assertEqual(retry_new.get_json()["feishu_projection"], "deferred")
        # 只发生了一次内联飞书尝试
        update.assert_called_once()
        self.assertEqual(len(self._samples()), 2)
        self.assertEqual(self.dispatched, [])

    # ------------------------------------------------------------------
    # 故障注入：内联先失败后成功 / 重试耗尽 / 任务扫描
    # ------------------------------------------------------------------

    def test_inline_fail_then_succeed_on_duplicate_retry(self) -> None:
        """第一次失败，重复请求作为内联重试成功：恢复为正常链路。"""
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", side_effect=_read_timeout),
            patch("routes.temperature.save_history"),
        ):
            first = self._post(temperature=24.6)
        self.assertEqual(first.get_json()["feishu_projection"], "deferred")

        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
        ):
            second = self._post(temperature=24.6)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "success")
        self.assertEqual(len(self._samples()), 1)
        self.assertEqual(self._state()["projection_status"], "ok")
        # HTTP 成功不直接派发；recovery 补派发恰好一次。
        self.assertEqual(self.dispatched, [])
        projection.recover_pending_dispatches(now=self._future_now())
        self.assertEqual(len(self.dispatched), 1)

    def test_retry_exhaustion_marks_failed_and_stops(self) -> None:
        """连续失败：重试有上限，终态 failed 可被发现，不再生成新任务。"""
        config.FEISHU_PROJECTION_MAX_RETRIES = 2
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()
        self.assertEqual(self._state()["projection_status"], "pending")

        connection = connect(config.SQLITE_DB_PATH)
        self.addCleanup(connection.close)
        repository = SQLiteAutomationTaskRepository(connection)
        scheduler = TaskScheduler(
            repository=repository,
            handlers={
                "FEISHU_PROJECTION": lambda task: projection.retry_device_projection(
                    task.entity_id
                )
            },
            worker_id="exhaustion-test",
        )

        with (
            patch("services.projection.resolve_record_id", side_effect=_connection_error),
            patch("services.projection.update_feishu_fields", side_effect=_connection_error),
        ):
            # 第一次重试失败（rc=1，仍 pending）
            projection.ensure_projection_tasks(repository, now=self._future_now())
            first = scheduler.run_once(now=self._future_now(610))
            self.assertEqual(first.failed, 1)
            self.assertEqual(self._state()["projection_status"], "pending")
            # 第二次重试失败（rc=2 达到上限 → failed 终态）
            projection.ensure_projection_tasks(repository, now=self._future_now(650))
            second = scheduler.run_once(now=self._future_now(660))
            self.assertEqual(second.failed, 1)
            self.assertEqual(self._state()["projection_status"], "failed")
            # 终态后 scanner 不再生成新任务（无无限 PENDING/retry storm）
            projection.ensure_projection_tasks(repository, now=self._future_now(700))
            third = scheduler.run_once(now=self._future_now(710))
            self.assertEqual(third.claimed, 0)

        # failed 状态可被健康检查发现
        summary = projection.projection_health_summary()
        self.assertEqual(summary["by_status"]["failed"], 1)
        self.assertEqual(summary["failed_devices"][0]["device"], "TH-05")

        # 恢复：飞书恢复后的下一次内联投影成功 → 状态复位；
        # 派发由 recovery 补齐 → Shadow 恢复
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
        ):
            recovery = self._post(temperature=26.0)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(self._state()["projection_status"], "ok")
        self.assertEqual(self.dispatched, [])
        projection.recover_pending_dispatches(now=self._future_now())
        self.assertEqual(len(self.dispatched), 1)

    def test_scanner_creates_single_task_per_device(self) -> None:
        """扫描器对同一设备最多维护一个未完成任务（dedupe）。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()

        connection = connect(config.SQLITE_DB_PATH)
        self.addCleanup(connection.close)
        repository = SQLiteAutomationTaskRepository(connection)
        projection.ensure_projection_tasks(repository, now=self._future_now())
        projection.ensure_projection_tasks(repository, now=self._future_now(5))

        rows = connection.execute(
            "SELECT COUNT(*) AS n FROM automation_tasks"
            " WHERE task_type = 'FEISHU_PROJECTION'"
            " AND status IN ('PENDING', 'RUNNING')"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_retry_projects_latest_sample_state(self) -> None:
        """重试投影的是最新本地样本（Feishu 是 current-state 投影）。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post(temperature=24.6, humidity=52.0)
            self._post(temperature=25.5, humidity=60.0)

        with (
            patch("services.projection.resolve_record_id", return_value="rec_01"),
            patch("services.projection.update_feishu_fields", side_effect=_ok) as retry_update,
        ):
            result = projection.retry_device_projection("TH-05")

        self.assertEqual(result["result"], "projected")
        fields = retry_update.call_args.args[1]
        self.assertEqual(fields["当前温度"], 25.5)
        self.assertEqual(fields["当前湿度"], 60.0)
        # retry 只投影不派发；recovery 只派发最新样本一次
        self.assertEqual(self.dispatched, [])
        projection.recover_pending_dispatches(now=self._future_now())
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0].temperature, 25.5)

    def test_backoff_grows_and_caps(self) -> None:
        config.FEISHU_PROJECTION_BACKOFF_SECONDS = 30.0
        self.assertEqual(projection.retry_backoff_seconds(0), 30.0)
        self.assertEqual(projection.retry_backoff_seconds(1), 60.0)
        self.assertEqual(projection.retry_backoff_seconds(2), 120.0)
        self.assertEqual(projection.retry_backoff_seconds(10), 600.0)

    def test_retry_not_due_before_backoff_elapses(self) -> None:
        """退避未到期的设备不会被扫描到（无 retry storm）。"""
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()

        soon = datetime.now().astimezone() + timedelta(seconds=5)
        self.assertEqual(projection.list_due_projection_retries(now=soon), [])
        later = datetime.now().astimezone() + timedelta(seconds=61)
        due = projection.list_due_projection_retries(now=later)
        self.assertEqual([device for device, _ in due], ["TH-05"])

    # ------------------------------------------------------------------
    # Scheduler-blocking：单次 handler 只做一次有界网络尝试
    # ------------------------------------------------------------------

    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def json(self) -> dict:
            return self._payload

    def _bounded_retry_setup(self) -> dict:
        """让 retry 走真实 feishu/token 内部实现（mock 网络边界）。

        返回共享的调用计数器；token 与 record 缓存被清空并在用例结束恢复。
        """
        import services.feishu as feishu_module
        import services.token as token_module

        counters = {
            "token_calls": [],
            "feishu_calls": [],
        }
        feishu_module._record_id_cache.clear()
        feishu_module._record_not_found_until.clear()
        token_module.clear_token()
        self._original.setdefault("TABLE_ID", config.TABLE_ID)
        config.TABLE_ID = "tbl-bounded-test"
        self._original.setdefault("APP_TOKEN", config.APP_TOKEN)
        config.APP_TOKEN = "bound-test-token"
        self._original.setdefault("APP_ID", config.APP_ID)
        config.APP_ID = "bound-test-app"
        self._original.setdefault("APP_SECRET", config.APP_SECRET)
        config.APP_SECRET = "bound-test-secret"

        def _token_call(method, url, **kwargs):
            counters["token_calls"].append(kwargs)
            return self._FakeResponse(
                {"code": 0, "tenant_access_token": "tok", "expire": 7200}
            )

        def _feishu_call(method, url, **kwargs):
            counters["feishu_calls"].append(kwargs)
            raise requests.exceptions.ConnectionError("feishu down")

        token_patcher = patch(
            "services.token.request_with_retry", side_effect=_token_call
        )
        feishu_patcher = patch(
            "services.feishu.request_with_retry", side_effect=_feishu_call
        )
        token_patcher.start()
        feishu_patcher.start()
        self.addCleanup(token_patcher.stop)
        self.addCleanup(feishu_patcher.stop)
        self.addCleanup(feishu_module._record_id_cache.clear)
        self.addCleanup(feishu_module._record_not_found_until.clear)
        self.addCleanup(token_module.clear_token)
        return counters

    def test_retry_attempt_is_single_bounded_network_call(self) -> None:
        """handler 内部不做多轮长 retry：每次网络调用 attempts=1、超时封顶。

        冷 token + 冷 record 缓存的最坏路径：token 1 次 + resolve 1 次，
        全部有界；失败立即返回（不 sleep 退避——那是 scheduler 的事）。
        """
        counters = self._bounded_retry_setup()
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()
        self.assertEqual(self._state()["projection_status"], "pending")

        with patch.object(config, "FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS", 5.0):
            with self.assertRaises(RuntimeError):
                projection.retry_device_projection("TH-05")

        # token 恰好 1 次、bitable 恰好 1 次（resolve 即失败，无内部重试）
        self.assertEqual(len(counters["token_calls"]), 1)
        self.assertEqual(len(counters["feishu_calls"]), 1)
        for kwargs in counters["token_calls"] + counters["feishu_calls"]:
            self.assertEqual(kwargs.get("attempts"), 1)
            self.assertEqual(kwargs.get("timeout"), 5.0)
        self.assertEqual(self._state()["projection_status"], "pending")
        self.assertEqual(int(self._state()["retry_count"]), 1)

    def test_retry_attempt_bounds_every_call_when_update_fails(self) -> None:
        """resolve 成功、update 失败：仍只有 2 次有界 bitable 调用。"""
        counters = self._bounded_retry_setup()
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()
        self.assertEqual(self._state()["projection_status"], "pending")

        def _feishu_call(method, url, **kwargs):
            counters["feishu_calls"].append(kwargs)
            if "/records" in url and method == "GET":
                return self._FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "items": [
                                {"record_id": "rec-05", "fields": {"设备编号": "TH-05"}}
                            ],
                            "has_more": False,
                        },
                    }
                )
            raise requests.exceptions.ConnectionError("feishu down on update")

        with patch(
            "services.feishu.request_with_retry", side_effect=_feishu_call
        ):
            with patch.object(
                config, "FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS", 5.0
            ):
                with self.assertRaises(RuntimeError):
                    projection.retry_device_projection("TH-05")

        # token 1 次（后续走缓存）+ resolve 1 页 + update 1 次，全部有界
        self.assertEqual(len(counters["token_calls"]), 1)
        self.assertEqual(len(counters["feishu_calls"]), 2)
        for kwargs in counters["feishu_calls"]:
            self.assertEqual(kwargs.get("attempts"), 1)
            self.assertEqual(kwargs.get("timeout"), 5.0)

    def test_token_fetch_bounded_during_projection_retry(self) -> None:
        """token 获取同样有界：飞书全站异常时 handler 不会先在 token 上
        烧掉 REQUEST_RETRY_TIMES × REQUEST_TIMEOUT_SECONDS。"""
        counters = self._bounded_retry_setup()
        with (
            patch("routes.temperature.resolve_record_id", side_effect=_connection_error),
            patch("routes.temperature.update_feishu_fields"),
            patch("routes.temperature.save_history"),
        ):
            self._post()
        self.assertEqual(self._state()["projection_status"], "pending")

        def _token_call(method, url, **kwargs):
            counters["token_calls"].append(kwargs)
            raise requests.exceptions.ConnectionError("token endpoint down")

        with patch("services.token.request_with_retry", side_effect=_token_call):
            with patch.object(
                config, "FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS", 5.0
            ):
                with self.assertRaises(RuntimeError):
                    projection.retry_device_projection("TH-05")

        self.assertEqual(len(counters["token_calls"]), 1)
        self.assertEqual(counters["token_calls"][0].get("attempts"), 1)
        # bitable 层从未被触碰
        self.assertEqual(counters["feishu_calls"], [])

    def test_content_dedupe_is_weak_idempotency_documented(self) -> None:
        """文档化边界：5 秒窗口会把同内容重复上报合并为一个业务样本。

        HA payload 无源端时间戳时的最小安全方案——两个内容完全相同且
        间隔 <5s 的请求视为同一次采样的重试；真实传感器状态变化必然
        改变内容，因此误合并只可能发生在窗口内读数完全相同的极端情况。
        """
        with (
            patch("routes.temperature.resolve_record_id", return_value="rec_01"),
            patch("routes.temperature.update_feishu_fields", return_value={"code": 0}),
            patch("routes.temperature.save_history"),
        ):
            self._post(temperature=24.6, humidity=52.0)
            # 窗口外（模拟 6s 后）同内容上报 = 第二次真实采样，保留两行
            with patch("services.devices.time.time", return_value=time.time() + 6):
                self._post(temperature=24.6, humidity=52.0)

        self.assertEqual(len(self._samples()), 2)


if __name__ == "__main__":
    unittest.main()
