"""Unified device/event/status API.

Auth policy mirrors ``routes/analytics`` exactly: ``/api/devices``,
``/api/devices/<id>`` and ``/api/events`` require ``HISTORY_API_KEY`` to be
configured and the request to carry ``X-History-Key`` (or
``Authorization: Bearer``); an unconfigured key returns 503 so an existing
deployment never silently gains unauthenticated read access after upgrade.
``/api/system/status`` is unauthenticated like ``/health`` and intentionally
exposes only health aggregates — never endpoints, IPs, or key details.
"""

from __future__ import annotations

import hmac

from flask import Blueprint, jsonify, request

import config
from services import db
from services.collector import get_collector_status


api_bp = Blueprint("api", __name__)


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


@api_bp.get("/api/system/status")
def system_status():
    summary = db.fetch_device_summary()
    last_sample_ms = summary.get("last_sample_time_ms")
    return jsonify({
        "status": "ok",
        "service": "temperature-monitor",
        "sqlite": db.get_stats(),
        "collectors": get_collector_status(),
        # device_count = 不同设备编号数；identity_count = (设备, 数据源)
        # 身份对数量，与 /api/devices 的 count 一致。
        "device_count": summary.get("device_count", 0),
        "identity_count": summary.get("identity_count", 0),
        "last_sample_time_ms": last_sample_ms,
    }), 200
