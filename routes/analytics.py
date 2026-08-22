from __future__ import annotations

import hmac
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, jsonify, request

import config
from services import db


analytics_bp = Blueprint("analytics", __name__)


def _auth_error():
    """Reuse the X-History-Key scheme so read endpoints never leak data."""
    if not config.HISTORY_API_KEY:
        return jsonify({
            "status": "error",
            "error": "本地查询未配置：缺少 HISTORY_API_KEY",
        }), 503

    provided_key = request.headers.get("X-History-Key", "")
    if not provided_key or not hmac.compare_digest(
        provided_key,
        config.HISTORY_API_KEY,
    ):
        return jsonify({"status": "error", "error": "本地查询鉴权失败"}), 401
    return None


def _mirror_disabled_error():
    if db.is_enabled():
        return None
    return jsonify({
        "status": "error",
        "error": "SQLite 本地镜像未启用或初始化失败",
    }), 503


def _parse_time_to_ms(raw: str | None) -> int | None:
    """Accept epoch seconds/milliseconds or an ISO 8601 timestamp."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
        if value > 10_000_000_000:
            return int(value)
        return int(value * 1000)
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"无法解析时间参数: {raw}")
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(config.HISTORY_TIMEZONE))
        except ZoneInfoNotFoundError:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return int(parsed.timestamp() * 1000)


def _parse_time_arg(name: str) -> tuple[int | None, str | None]:
    try:
        return _parse_time_to_ms(request.args.get(name)), None
    except ValueError as exc:
        return None, str(exc)


@analytics_bp.get("/history/query")
def query_snapshots():
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    start_ms, parse_error = _parse_time_arg("start")
    if parse_error:
        return jsonify({"status": "error", "error": parse_error}), 400
    end_ms, parse_error = _parse_time_arg("end")
    if parse_error:
        return jsonify({"status": "error", "error": parse_error}), 400

    device = request.args.get("device", "").strip().upper() or None
    # ``type=int`` yields None for non-numeric input; fall back to the default
    # and clamp instead of letting a TypeError turn into a 500.
    limit = request.args.get("limit", default=500, type=int) or 500
    limit = max(1, min(limit, 10000))

    items = db.fetch_history_snapshots(
        device=device,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=limit,
    )
    return jsonify({
        "status": "success",
        "count": len(items),
        "device": device,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "items": items,
    }), 200


@analytics_bp.get("/history/stats/daily")
def daily_stats():
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    start_ms, parse_error = _parse_time_arg("start")
    if parse_error:
        return jsonify({"status": "error", "error": parse_error}), 400
    end_ms, parse_error = _parse_time_arg("end")
    if parse_error:
        return jsonify({"status": "error", "error": parse_error}), 400

    device = request.args.get("device", "").strip().upper() or None
    days = request.args.get("days", default=7, type=int) or 7
    days = max(1, min(days, 90))

    # Default window: the last N local days ending now, so an unparameterized
    # call never scans the whole mirror.
    if start_ms is None and end_ms is None:
        end_ms = int(datetime.now().timestamp() * 1000)
        start_ms = end_ms - days * 86_400_000

    try:
        timezone = ZoneInfo(config.HISTORY_TIMEZONE)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")

    items = db.fetch_daily_stats(
        device=device,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    return jsonify({
        "status": "success",
        "count": len(items),
        "device": device,
        "timezone": str(timezone),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "items": items,
    }), 200


@analytics_bp.get("/history/stats/devices")
def device_stats():
    error = _auth_error() or _mirror_disabled_error()
    if error:
        return error

    items = db.fetch_device_stats()
    interval_seconds = config.HISTORY_INTERVAL_MINUTES * 60
    for item in items:
        # Estimate, not exact duration: missed buckets, service downtime, or
        # clock drift all skew this value. Field name makes that explicit.
        item["estimated_offline_duration_sec"] = (
            (item.get("offline_sample_count") or 0) * interval_seconds
        )
    return jsonify({
        "status": "success",
        "count": len(items),
        "interval_minutes": interval_seconds // 60,
        "items": items,
    }), 200
