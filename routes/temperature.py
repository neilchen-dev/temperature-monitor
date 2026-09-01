from __future__ import annotations

import hmac
import logging
import time
from typing import Any

import requests
from flask import Blueprint, jsonify, request

import config
from services import db, devices
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

    try:
        bitable_device = config.DEVICE_NAME_MAP.get(device, device)
        configured_device = config.DEVICES.get(bitable_device, {})
        record_id = resolve_record_id(bitable_device, configured_device.get("record_id"))
        if is_offline_status(status_value):
            # 离线时只修改在线状态，保留飞书中的最后温湿度和更新时间。
            result = update_feishu_fields(record_id, {"在线状态": "离线"})
            temperature_c: Any = ""
            humidity: Any = ""
            final_status = "离线"
        else:
            temperature_c = normalize_temperature(data.get("temperature"))
            humidity = normalize_humidity(data.get("humidity"))
            result = update_feishu_fields(
                record_id,
                {
                    "当前温度": temperature_c,
                    "当前湿度": humidity,
                    "在线状态": "在线",
                    "更新时间": int(time.time() * 1000),
                },
            )
            final_status = "在线"

    except ValueError as exc:
        logger.warning("数据无效 | device=%s | error=%s | data=%s", device, exc, data)
        return jsonify({"status": "error", "error": str(exc), "device": device}), 400
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        logger.exception("飞书请求异常 | device=%s", device)
        return jsonify({"status": "error", "error": str(exc), "device": device}), 502

    feishu_code = int(result.get("code", -1))
    feishu_message = str(result.get("msg", ""))
    success = feishu_code == 0

    save_history(
        # 历史镜像同样使用映射后的设备编号，和 device_samples/飞书保持同一
        # 设备身份；否则 DEVICE_NAME_MAP 非恒等映射时 /api 与历史表各说各话。
        bitable_device,
        temperature_c,
        humidity,
        final_status,
        feishu_code,
        feishu_message,
    )

    # 统一设备模型：与 Modbus 采集共用同一份 device_samples 存储；
    # 使用映射后的设备编号（与飞书/历史采样同一身份），避免同一物理设备
    # 因 HA 上报名称不同被拆成两个身份。record_sample 内部吞掉所有异常，
    # 绝不影响原有上报链路。
    devices.record_sample(
        device=bitable_device,
        source=devices.SOURCE_HOME_ASSISTANT,
        temperature=temperature_c if isinstance(temperature_c, (int, float)) else None,
        humidity=humidity if isinstance(humidity, (int, float)) else None,
        status=final_status,
    )

    if not success:
        logger.error(
            "飞书更新失败 | device=%s | code=%s | msg=%s",
            device,
            feishu_code,
            feishu_message,
        )
        return jsonify({"status": "error", "device": device, "feishu": result}), 502

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


@temperature_bp.get("/health")
def health():
    return jsonify({"status": "ok", "sqlite": db.get_stats()}), 200
