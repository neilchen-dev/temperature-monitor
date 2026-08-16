from __future__ import annotations

import logging
import time
from typing import Any

import requests
from flask import Blueprint, jsonify, request

import config
from services.feishu import resolve_record_id, update_feishu_fields
from services.storage import save_history
from services.validator import is_offline_status, normalize_humidity, normalize_temperature


temperature_bp = Blueprint("temperature", __name__)
logger = logging.getLogger("temperature_monitor")


@temperature_bp.post("/temperature")
def temperature():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "error": "请求体必须是 JSON 对象"}), 400

    device = str(data.get("device", "")).strip().upper()
    if not device:
        return jsonify({"status": "error", "error": "缺少 device"}), 400
    # 兼容旧 HA 配置：没有 status 时仍按在线数据处理。
    status_value = data.get("status", "在线")

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
        device,
        temperature_c,
        humidity,
        final_status,
        feishu_code,
        feishu_message,
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
    return jsonify({"status": "ok"}), 200
