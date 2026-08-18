from __future__ import annotations

import hmac

from flask import Blueprint, jsonify, request

import config
from services.history import (
    HistoryConfigurationError,
    sample_history,
    validate_history_config,
)


history_bp = Blueprint("history", __name__)


@history_bp.post("/history/sample")
def sample():
    if not config.HISTORY_API_KEY:
        return jsonify({
            "status": "error",
            "error": "历史采样未配置：缺少 HISTORY_API_KEY",
            "cleanup_enabled": bool(config.HISTORY_CLEANUP_ENABLED),
        }), 503

    provided_key = request.headers.get("X-History-Key", "")
    if not provided_key or not hmac.compare_digest(
        provided_key,
        config.HISTORY_API_KEY,
    ):
        return jsonify({
            "status": "error",
            "error": "历史采样鉴权失败",
            "cleanup_enabled": bool(config.HISTORY_CLEANUP_ENABLED),
        }), 401

    try:
        validate_history_config()
    except HistoryConfigurationError as exc:
        return jsonify({
            "status": "error",
            "error": str(exc),
            "cleanup_enabled": bool(config.HISTORY_CLEANUP_ENABLED),
        }), 503

    payload, status_code = sample_history()
    return jsonify(payload), status_code
