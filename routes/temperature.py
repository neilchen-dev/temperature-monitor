from __future__ import annotations

import hmac
import logging
from typing import Any

import requests
from flask import Blueprint, jsonify, request

import config
from services import db, devices, projection
from services.feishu import resolve_record_id, update_feishu_fields
from services.storage import save_history
from services.validator import is_offline_status, normalize_humidity, normalize_temperature


temperature_bp = Blueprint("temperature", __name__)
logger = logging.getLogger("temperature_monitor")


def _temperature_auth_error() -> Any:
    """Optional shared-key auth; stays disabled when TEMPERATURE_API_KEY is empty."""
    if not config.TEMPERATURE_API_KEY:
        return None

    provided = (
        request.headers.get("X-Temperature-Key", "")
        or request.headers.get("X-History-Key", "")
    )
    if not provided or not hmac.compare_digest(provided, config.TEMPERATURE_API_KEY):
        return jsonify({"status": "error", "error": "温度上报鉴权失败"}), 401
    return None


@temperature_bp.post("/temperature")
def temperature():
    """Two-phase ingestion for HA temperature reports.

    Phase A — durable local persistence: a sample that passed basic
    validation is persisted to device_samples/device_events *before* any
    Feishu call, so an external Feishu outage can never lose the raw
    sample (production invariant; see docs/ for the TH-05 incident).

    Phase B — Feishu realtime projection + Runtime/Shadow dispatch: the
    projection is attempted inline (legacy realtime behaviour). On
    success the sample is dispatched to the Shadow pipeline, preserving
    the ordering "Feishu updated before SHADOW_COMPARE observes it". On
    failure the sample stays local, the failure is recorded durably
    (sample_projection_state + temperature_reports evidence), a bounded
    retry task is scheduled, dispatch is deferred (no guaranteed-wrong
    compares), and the request returns 200 accepted/deferred.
    """
    auth_error = _temperature_auth_error()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "error": "请求体必须是 JSON 对象"}), 400

    device = str(data.get("device", "")).strip().upper()
    if not device:
        return jsonify({"status": "error", "error": "缺少 device"}), 400
    # 兼容旧 HA 配置：没有 status 时仍按在线数据处理。
    status_value = data.get("status", "在线")
    # /temperature 存活打点：与统一模型健康联动，识别“上报仍在、
    # 统一样本断流”的 degraded 状态（见 devices.get_device_model_health）。
    devices.note_temperature_request(device)

    # ---- 基本校验（与旧版完全一致的 400 语义，发生在任何持久化之前）----
    bitable_device = config.DEVICE_NAME_MAP.get(device, device)
    configured_record_id = config.DEVICES.get(bitable_device, {}).get("record_id")
    offline = is_offline_status(status_value)
    if offline:
        # 离线时只修改在线状态，保留飞书中的最后温湿度。
        temperature_c: Any = ""
        humidity: Any = ""
        final_status = "离线"
    else:
        try:
            temperature_c = normalize_temperature(data.get("temperature"))
            humidity = normalize_humidity(data.get("humidity"))
        except ValueError as exc:
            logger.warning(
                "数据无效 | device=%s | error=%s | data=%s", device, exc, data
            )
            return jsonify({"status": "error", "error": str(exc), "device": device}), 400
        final_status = "在线"

    # ---- 阶段 A：本地 durable 持久化（绝不依赖飞书是否可用）----
    # 统一设备模型：与 Modbus 采集共用同一份 device_samples 存储；使用映射
    # 后的设备编号（与飞书/历史采样同一身份）。内容级去重让 HTTP/HA 重试
    # 不产生第二份业务样本。
    outcome = devices.persist_sample(
        device=bitable_device,
        source=devices.SOURCE_HOME_ASSISTANT,
        temperature=temperature_c if isinstance(temperature_c, (int, float)) else None,
        humidity=humidity if isinstance(humidity, (int, float)) else None,
        status=final_status,
        dedupe_window_ms=config.TEMPERATURE_DEDUPE_WINDOW_MS,
    )
    sample = outcome.sample
    durable = outcome.durable
    if sample is not None:
        projection.note_sample_persisted(bitable_device, int(outcome.sample_time_ms))
        logger.info(
            "sample_received | device=%s | status=%s | temperature=%s"
            " | humidity=%s | sample_time_ms=%s | local_persisted=%s"
            " | duplicate=%s",
            bitable_device,
            final_status,
            temperature_c,
            humidity,
            outcome.sample_time_ms,
            outcome.persisted,
            outcome.duplicate,
        )

    # 重复上报且上一份已经投影+派发完成：直接重放成功语义，不再触碰飞书
    # （HA/客户端重试不会产生重复副作用）。请求级审计日志照常记录。
    if (
        outcome.duplicate
        and sample is not None
        and projection.is_sample_dispatched(bitable_device, int(outcome.sample_time_ms))
    ):
        logger.info(
            "duplicate_request_accepted | device=%s | sample_time_ms=%s",
            bitable_device,
            outcome.sample_time_ms,
        )
        save_history(
            bitable_device,
            temperature_c,
            humidity,
            final_status,
            0,
            "duplicate request: already projected and dispatched",
        )
        return jsonify({
            "status": "success",
            "device": device,
            "online_status": final_status,
            "temperature_c": temperature_c,
            "humidity": humidity,
        }), 200

    # ---- 阶段 B：飞书 realtime projection ----
    # 投影刚失败过（pending/failed 且在抑制窗口内）时不再同步阻塞
    # Waitress 线程重试飞书；由 scheduler 的 FEISHU_PROJECTION 任务收敛。
    attempted = False
    result: dict[str, Any] = {}
    projection_error: str | None = None
    suppressed = durable and projection.should_suppress_inline_attempt(bitable_device)

    if not suppressed:
        attempted = True
        try:
            record_id = resolve_record_id(bitable_device, configured_record_id)
            result = update_feishu_fields(
                record_id,
                projection.build_projection_fields(
                    temperature_c, humidity, offline=offline
                ),
            )
        except (
            requests.exceptions.RequestException,
            RuntimeError,
            ValueError,
        ) as exc:
            projection_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "飞书实时投影失败 | device=%s | error=%s", device, projection_error
            )

    feishu_code = int(result.get("code", -1)) if attempted else -1
    feishu_message = str(result.get("msg", "")) if attempted else ""
    success = attempted and projection_error is None and feishu_code == 0

    if success:
        # 投影成功：与旧版一致的顺序 —— 先历史镜像，再派发 Runtime/Shadow
        # （保证 SHADOW_COMPARE 观察到的飞书已是最新投影）。
        # mark_projection_success 先落投影水位再清状态：崩溃在水位之后、
        # 派发之前时，恢复扫描（recover_pending_dispatches）能发现
        # projected > dispatched 并补派发。
        projection.mark_projection_success(
            bitable_device,
            int(outcome.sample_time_ms) if outcome.sample_time_ms is not None else None,
        )
        save_history(bitable_device, temperature_c, humidity, final_status, 0, feishu_message)
        if sample is not None:
            projection.dispatch_projected_sample(
                bitable_device, sample, outcome.sample_time_ms
            )
        logger.info(
            "%s | 飞书更新成功 | status=%s | temperature=%s | humidity=%s",
            device,
            final_status,
            temperature_c,
            humidity,
        )
        return jsonify({
            "status": "success",
            "device": device,
            "online_status": final_status,
            "temperature_c": temperature_c,
            "humidity": humidity,
        }), 200

    # ---- 投影失败 / 被抑制：本地样本保留，语义显式区分 ----
    if durable and sample is not None:
        # 有效样本已本地持久化：外部投影失败不等于 sample 采集失败。
        # HA rest_command 不做自动重试（fire-and-forget）；返回 200 让
        # 重试型客户端（curl 脚本/网关）也拿到确定性结果，副作用去重由
        # 内容级 dedupe + 投影状态机保证。
        if attempted:
            if projection_error is None:
                error_summary = (
                    f"feishu code={feishu_code} msg={feishu_message!r}"
                )
            else:
                error_summary = projection_error
        else:
            error_summary = "projection suppressed: recent failure backoff window"
        projection.mark_projection_failure(bitable_device, error_summary)
        save_history(
            bitable_device,
            temperature_c,
            humidity,
            final_status,
            feishu_code if attempted else -1,
            f"projection_deferred: {error_summary}",
        )
        logger.warning(
            "sample_accepted_projection_deferred | device=%s"
            " | sample_time_ms=%s | attempted=%s | error=%s",
            bitable_device,
            outcome.sample_time_ms,
            attempted,
            error_summary,
        )
        return jsonify({
            "status": "accepted",
            "device": device,
            "online_status": final_status,
            "temperature_c": temperature_c,
            "humidity": humidity,
            "local_persisted": True,
            "feishu_projection": "deferred",
        }), 200

    # 本地镜像不可用（SQLITE_ENABLED=false 或落库失败）：无法保证“sample
    # 不丢”不变量，保持旧版语义 —— 非外部依赖故障时如实返回 502，
    # 由 HA/监控侧感知；历史 CSV 仍记录失败证据。
    error_summary = (
        projection_error
        if projection_error is not None
        else ("projection suppressed" if suppressed else "local persistence unavailable")
    )
    save_history(
        bitable_device,
        temperature_c,
        humidity,
        final_status,
        feishu_code if attempted else -1,
        f"projection_failed: {error_summary}",
    )
    logger.error(
        "飞书更新失败且本地持久化不可用 | device=%s | attempted=%s | error=%s",
        device,
        attempted,
        error_summary,
    )
    if attempted and projection_error is None:
        return jsonify({"status": "error", "device": device, "feishu": result}), 502
    return jsonify({"status": "error", "error": error_summary, "device": device}), 502


@temperature_bp.get("/health")
def health():
    return jsonify({"status": "ok", "sqlite": db.get_stats()}), 200
