"""Shared request dependencies for the route blueprints.

All key checks are timing-safe (``hmac.compare_digest``) and fail closed:
an unconfigured key returns 503 instead of silently granting access.
Query-string keys are deliberately not accepted — they leak into browser
history, bookmarks, and gateway access logs.
"""

from __future__ import annotations

import hmac
from typing import Any

from flask import jsonify, request

import config
from services import db


def history_auth_error() -> Any:
    """Auth gate for ``HISTORY_API_KEY``-protected endpoints.

    Accepts ``X-History-Key`` or ``Authorization: Bearer``; returns the Flask
    error response (503 when unconfigured, 401 on a wrong key) or None.
    """
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


def temperature_auth_error() -> Any:
    """Optional shared-key gate for ``/temperature``.

    Stays disabled (returns None) when ``TEMPERATURE_API_KEY`` is empty to
    keep legacy unauthenticated HA configurations working.
    """
    if not config.TEMPERATURE_API_KEY:
        return None

    provided = (
        request.headers.get("X-Temperature-Key", "")
        or request.headers.get("X-History-Key", "")
    )
    if not provided or not hmac.compare_digest(provided, config.TEMPERATURE_API_KEY):
        return jsonify({"status": "error", "error": "温度上报鉴权失败"}), 401
    return None


def mirror_disabled_error() -> Any:
    """A dead/disabled SQLite mirror must surface as 503, not empty lists."""
    if db.is_enabled():
        return None
    return jsonify({
        "status": "error",
        "error": "SQLite 本地镜像未启用或初始化失败",
    }), 503


def feishu_write_disabled_error() -> Any:
    """Both global switches must be on before any Feishu write endpoint."""
    if config.FEISHU_WRITE_ENABLED and str(config.AUTOMATION_MODE).lower() == "active":
        return None
    return jsonify({
        "status": "error",
        "error": "飞书写入未启用；请同时设置 FEISHU_WRITE_ENABLED=true 和 AUTOMATION_MODE=active",
    }), 503


def json_payload() -> tuple[dict | None, tuple]:
    """Parse the request body as a JSON object or return a 400 response."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, (jsonify({"status": "error", "error": "请求体必须是 JSON 对象"}), 400)
    return payload, ()
