"""Unified device/event/status API.

Auth policy mirrors ``routes/analytics`` exactly: ``/api/devices``,
``/api/devices/<id>``, ``/api/events`` and ``/api/thresholds`` require
``HISTORY_API_KEY`` to be configured and the request to carry
``X-History-Key`` (or ``Authorization: Bearer``); an unconfigured key
returns 503 so an existing deployment never silently gains unauthenticated
read access after upgrade. ``/api/system/status`` is unauthenticated like
``/health`` and intentionally exposes only health aggregates — never
endpoints, IPs, or key details.

``/api/thresholds`` is the only writing surface here: it stores per-device
control bands in the local SQLite mirror (never pushed to Feishu) for the
``/console`` page to render limit highlighting.
"""

from __future__ import annotations

from datetime import datetime
import hmac
import requests

from flask import Blueprint, jsonify, request

from runtime.bootstrap import runtime_status, shadow_summary_snapshot

import config
from integrations.feishu_records import FeishuBitableRecordSource
from integrations.feishu_writers import (
    FeishuBitableRecordWriter,
    FeishuEnvironmentEventWriter,
    FeishuInspectionRecordWriter,
    FeishuOperationRecordWriter,
    FeishuWriteError,
)
from services import db, devices
from services.collector import get_collector_status


api_bp = Blueprint("api", __name__)

# Threshold bounds match the sensor physical ranges (validator.py /
# modbus_client.py): thresholds are stored in °C / %RH regardless of
# SOURCE_TEMPERATURE_UNIT, which only governs interpreting incoming reports.
TEMPERATURE_BOUND_MIN, TEMPERATURE_BOUND_MAX = -50.0, 100.0
HUMIDITY_BOUND_MIN, HUMIDITY_BOUND_MAX = 0.0, 100.0

_THRESHOLD_FIELDS = ("temp_min", "temp_max", "humidity_min", "humidity_max")


def _auth_error():
    if not config.HISTORY_API_KEY:
        return jsonify({
            "status": "error",
            "error": "本地查询未配置：缺少 HISTORY_API_KEY",
        }), 503

    provided = (
        request.headers.get("X-History-Key", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    if not provided or not hmac.compare_digest(provided, config.HISTORY_API_KEY):
        return jsonify({"status": "error", "error": "API 鉴权失败"}), 401
    return None


def _mirror_disabled_error():
    """A dead/disabled SQLite mirror must surface as 503, not empty lists."""
    if db.is_enabled():
        return None
    return jsonify({
        "status": "error",
        "error": "SQLite 本地镜像未启用或初始化失败",
    }), 503


def _feishu_write_disabled_error():
    if config.FEISHU_WRITE_ENABLED and str(config.AUTOMATION_MODE).lower() == "active":
        return None
    return jsonify({
        "status": "error",
        "error": "飞书写入未启用；请同时设置 FEISHU_WRITE_ENABLED=true 和 AUTOMATION_MODE=active",
    }), 503


def _json_payload() -> tuple[dict | None, tuple]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, (jsonify({"status": "error", "error": "请求体必须是 JSON 对象"}), 400)
    return payload, ()


def _api_datetime(payload: dict, field_name: str) -> datetime:
    value = payload.get(field_name)
    if value in (None, ""):
        raise ValueError(f"缺少 {field_name}")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是 ISO 时间字符串")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} 不是有效的 ISO 时间") from exc


def _writer_error_response(exc: Exception):
    if isinstance(exc, FeishuWriteError):
        return jsonify({"status": "error", "error": str(exc)}), 409
    if isinstance(exc, ValueError):
        return jsonify({"status": "error", "error": str(exc)}), 400
    if isinstance(exc, (RuntimeError, requests.exceptions.RequestException)):
        return jsonify({"status": "error", "error": str(exc)}), 502
    raise exc


def _serialize_device_state(row: dict) -> dict:
    return {
        "device_id": row["device"],
        "source": row["source"],
        "status": row["status"],
        "temperature": row["temperature"],
        "humidity": row["humidity"],
        "last_sample_time": row["sample_time_iso"],
        "last_sample_time_ms": row["sample_time_ms"],
        "sample_count": row.get("sample_count"),
    }


@api_bp.get("/api/devices")
def list_devices():
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    items = [
        _serialize_device_state(row) for row in db.fetch_latest_device_states()
    ]
    return jsonify({"status": "success", "count": len(items), "items": items}), 200


@api_bp.get("/api/devices/<device_id>")
def device_detail(device_id: str):
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    normalized = device_id.strip().upper()
    source = request.args.get("source", "").strip().lower() or None
    rows = [
        row for row in db.fetch_latest_device_states()
        if row["device"] == normalized
    ]
    if not rows:
        return jsonify({
            "status": "error",
            "error": f"未知设备: {normalized}",
            "device_id": normalized,
        }), 404

    if source is not None:
        rows = [row for row in rows if row["source"] == source]
        if not rows:
            return jsonify({
                "status": "error",
                "error": f"设备 {normalized} 没有来自 {source} 的样本",
                "device_id": normalized,
                "source": source,
            }), 404
        latest = rows[0]
    elif len(rows) > 1:
        # 同一设备多个数据源时不允许静默任取一条：必须显式选择身份
        return jsonify({
            "status": "error",
            "error": f"设备 {normalized} 存在多个数据源，请用 ?source= 指定",
            "device_id": normalized,
            "sources": sorted(row["source"] for row in rows),
        }), 400
    else:
        latest = rows[0]

    limit = request.args.get("limit", default=50, type=int) or 50
    samples = db.fetch_device_samples(normalized, source=source, limit=limit)
    return jsonify({
        "status": "success",
        "device": _serialize_device_state(latest),
        "samples": samples,
        "sample_count": len(samples),
    }), 200


@api_bp.get("/api/events")
def list_events():
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    device_id = request.args.get("device", "").strip().upper() or None
    limit = request.args.get("limit", default=100, type=int) or 100
    items = db.fetch_device_events(device_id=device_id, limit=limit)
    return jsonify({
        "status": "success",
        "count": len(items),
        "device": device_id,
        "items": items,
    }), 200


@api_bp.post("/api/operations")
def create_operation_registration():
    """Create one validated operation registration in the Feishu source table."""
    error = _auth_error() or _feishu_write_disabled_error()
    if error:
        return error
    payload, payload_error = _json_payload()
    if payload_error:
        return payload_error
    assert payload is not None
    try:
        writer = FeishuOperationRecordWriter(
            writer=FeishuBitableRecordWriter(),
            operation_table_id=config.FEISHU_OPERATION_TABLE_ID,
            interval_table_id=config.FEISHU_OPERATION_INTERVAL_TABLE_ID,
            device_table_id=config.FEISHU_DEVICE_TABLE_ID,
        )
        result = writer.create_registration(
            device_id=str(payload.get("device_id", "")),
            area=str(payload.get("area", "")),
            action=str(payload.get("action", "")),
            operation_type=(
                str(payload["operation_type"]).strip()
                if payload.get("operation_type") not in (None, "")
                else None
            ),
            work_order=(
                str(payload["work_order"]).strip()
                if payload.get("work_order") not in (None, "")
                else None
            ),
            status_recorded_at=(
                _api_datetime(payload, "status_recorded_at")
                if payload.get("status_recorded_at") not in (None, "")
                else None
            ),
            idempotency_key=(
                str(payload["idempotency_key"]).strip()
                if payload.get("idempotency_key") not in (None, "")
                else None
            ),
        )
        return jsonify({"status": "success", "feishu": result}), 201
    except Exception as exc:  # noqa: BLE001 - translate adapter errors to API responses
        return _writer_error_response(exc)


@api_bp.post("/api/environment-events")
def create_environment_event():
    """Create one ENV event after enforcing the single-active-event rule."""
    error = _auth_error() or _feishu_write_disabled_error()
    if error:
        return error
    payload, payload_error = _json_payload()
    if payload_error:
        return payload_error
    assert payload is not None
    try:
        writer = FeishuEnvironmentEventWriter(
            writer=FeishuBitableRecordWriter(),
            source=FeishuBitableRecordSource(),
            event_table_id=config.FEISHU_EVENT_TABLE_ID,
            device_table_id=config.FEISHU_DEVICE_TABLE_ID,
            device_id_field=config.DEVICE_ID_FIELD,
        )
        result = writer.create_event(
            device_id=str(payload.get("device_id", "")),
            area=str(payload.get("area", "")),
            start_time=_api_datetime(payload, "start_time"),
            temperature=payload.get("temperature"),
            humidity=payload.get("humidity"),
            temperature_status=payload.get("temperature_status"),
            humidity_status=payload.get("humidity_status"),
            owner=payload.get("owner"),
            control_requirement=payload.get("control_requirement"),
            idempotency_key=payload.get("idempotency_key"),
        )
        return jsonify({"status": "success", "feishu": result}), 201
    except Exception as exc:  # noqa: BLE001 - translate adapter errors to API responses
        return _writer_error_response(exc)


@api_bp.patch("/api/environment-events/<record_id>")
def close_environment_event(record_id: str):
    """Close an ENV event only after the required manual closure fields exist."""
    error = _auth_error() or _feishu_write_disabled_error()
    if error:
        return error
    payload, payload_error = _json_payload()
    if payload_error:
        return payload_error
    assert payload is not None
    try:
        writer = FeishuEnvironmentEventWriter(
            writer=FeishuBitableRecordWriter(),
            source=FeishuBitableRecordSource(),
            event_table_id=config.FEISHU_EVENT_TABLE_ID,
            device_table_id=config.FEISHU_DEVICE_TABLE_ID,
            device_id_field=config.DEVICE_ID_FIELD,
        )
        result = writer.close_event(
            record_id=record_id,
            closed_at=_api_datetime(payload, "closed_at"),
            cause=str(payload.get("cause", "")),
            measure=str(payload.get("measure", "")),
            product_impact=str(payload.get("product_impact", "")),
            recovered_at=(
                _api_datetime(payload, "recovered_at")
                if payload.get("recovered_at") not in (None, "")
                else None
            ),
        )
        return jsonify({"status": "success", "feishu": result}), 200
    except Exception as exc:  # noqa: BLE001 - translate adapter errors to API responses
        return _writer_error_response(exc)


@api_bp.post("/api/inspections")
def create_inspection_record():
    """Create one warehouse inspection record with safe link/number handling."""
    error = _auth_error() or _feishu_write_disabled_error()
    if error:
        return error
    payload, payload_error = _json_payload()
    if payload_error:
        return payload_error
    assert payload is not None
    try:
        writer = FeishuInspectionRecordWriter(
            writer=FeishuBitableRecordWriter(),
            inspection_table_id=config.FEISHU_INSPECTION_TABLE_ID,
            device_table_id=config.FEISHU_DEVICE_TABLE_ID,
        )
        result = writer.create_snapshot(
            area=str(payload.get("area", "")),
            inspected_at=_api_datetime(payload, "inspected_at"),
            inspector=payload.get("inspector"),
            temperature=payload.get("temperature"),
            humidity=payload.get("humidity"),
            online_status=payload.get("online_status"),
            environment_status=payload.get("environment_status"),
            temperature_status=payload.get("temperature_status"),
            humidity_status=payload.get("humidity_status"),
            alarm_status=payload.get("alarm_status"),
            monitoring_system_status=payload.get("monitoring_system_status"),
            site_storage_status=payload.get("site_storage_status"),
            abnormal_alarm_number=payload.get("abnormal_alarm_number"),
            abnormal_handling=payload.get("abnormal_handling"),
            system_abnormal_description=payload.get("system_abnormal_description"),
            parent_record_id=payload.get("parent_record_id"),
            state_recorded_at=(
                _api_datetime(payload, "state_recorded_at")
                if payload.get("state_recorded_at") not in (None, "")
                else None
            ),
            idempotency_key=payload.get("idempotency_key"),
        )
        return jsonify({"status": "success", "feishu": result}), 201
    except Exception as exc:  # noqa: BLE001 - translate adapter errors to API responses
        return _writer_error_response(exc)


def _parse_threshold_bound(
    payload: dict, field: str, low: float, high: float,
) -> tuple[float | None, str | None]:
    value = payload.get(field)
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{field} 必须是数字或 null: {value!r}"
    number = float(value)
    if not low <= number <= high:
        return None, f"{field} 超出有效范围 [{low}, {high}]: {number}"
    return number, None


@api_bp.get("/api/thresholds")
def list_thresholds():
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    items = db.fetch_device_thresholds()
    return jsonify({"status": "success", "count": len(items), "items": items}), 200


@api_bp.put("/api/thresholds/<device_id>")
def replace_threshold(device_id: str):
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    normalized = device_id.strip().upper()
    if not normalized:
        return jsonify({
            "status": "error",
            "error": "设备编号不能为空",
        }), 400

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error": "请求体必须是 JSON 对象",
        }), 400
    missing = [field for field in _THRESHOLD_FIELDS if field not in payload]
    if missing:
        return jsonify({
            "status": "error",
            "error": f"缺少字段: {', '.join(missing)}（清除边界请显式传 null）",
        }), 400

    bounds: dict[str, float | None] = {}
    for field, low, high in (
        ("temp_min", TEMPERATURE_BOUND_MIN, TEMPERATURE_BOUND_MAX),
        ("temp_max", TEMPERATURE_BOUND_MIN, TEMPERATURE_BOUND_MAX),
        ("humidity_min", HUMIDITY_BOUND_MIN, HUMIDITY_BOUND_MAX),
        ("humidity_max", HUMIDITY_BOUND_MIN, HUMIDITY_BOUND_MAX),
    ):
        value, parse_error = _parse_threshold_bound(payload, field, low, high)
        if parse_error:
            return jsonify({"status": "error", "error": parse_error}), 400
        bounds[field] = value

    for low_field, high_field, label in (
        ("temp_min", "temp_max", "温度"),
        ("humidity_min", "humidity_max", "湿度"),
    ):
        low, high = bounds[low_field], bounds[high_field]
        if low is not None and high is not None and low >= high:
            return jsonify({
                "status": "error",
                "error": f"{label}下限必须小于上限: {low} >= {high}",
            }), 400

    saved = db.save_device_threshold(
        normalized,
        bounds["temp_min"],
        bounds["temp_max"],
        bounds["humidity_min"],
        bounds["humidity_max"],
    )
    if not saved:
        return jsonify({
            "status": "error",
            "error": "阈值写入失败，本地存储不可用",
        }), 503
    items = db.fetch_device_thresholds(normalized)
    return jsonify({
        "status": "success",
        "device": normalized,
        "threshold": items[0] if items else None,
    }), 200


@api_bp.get("/api/system/status")
def system_status():
    summary = db.fetch_device_summary()
    last_sample_ms = summary.get("last_sample_time_ms")
    return jsonify({
        "status": "ok",
        "service": "temperature-monitor",
        "sqlite": db.get_stats(),
        "collectors": get_collector_status(),
        "runtime": runtime_status(),
        # 统一设备模型健康：record_sample 错误计数/最后错误/最后成功时间，
        # 以及“/temperature 仍有上报但统一样本断流”的 degraded 判定。
        "device_model": devices.get_device_model_health(),
        # device_count = 不同设备编号数；identity_count = (设备, 数据源)
        # 身份对数量，与 /api/devices 的 count 一致。
        "device_count": summary.get("device_count", 0),
        "identity_count": summary.get("identity_count", 0),
        "last_sample_time_ms": last_sample_ms,
    }), 200


@api_bp.get("/api/shadow/summary")
def shadow_summary_route():
    """Recent Shadow comparison aggregates for production verification.

    Requires HISTORY_API_KEY like other read endpoints; exposes counts only
    (never record payloads or credentials).
    """
    auth_error = _auth_error()
    if auth_error:
        return auth_error
    hours = request.args.get("hours", default=24, type=int)
    if hours is None:
        hours = 24
    hours = max(1, min(hours, 24 * 30))
    summary = shadow_summary_snapshot(hours=hours)
    return jsonify({"status": "success", "summary": summary}), 200
