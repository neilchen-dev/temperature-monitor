from __future__ import annotations

import logging
import threading
import time
from typing import Any

import config
from services.http_client import request_with_retry


logger = logging.getLogger("temperature_monitor")
_token_cache: dict[str, Any] = {"token": None, "expire": 0.0}
_token_lock = threading.Lock()


def clear_token() -> None:
    with _token_lock:
        _token_cache["token"] = None
        _token_cache["expire"] = 0.0


def is_token_cached() -> bool:
    return bool(_token_cache["token"])


def get_token(
    force_refresh: bool = False,
    *,
    attempts: int | None = None,
    timeout: float | None = None,
) -> str:
    # attempts/timeout 透传给 request_with_retry：scheduler 的
    # FEISHU_PROJECTION 有界尝试（attempts=1）必须覆盖 token 获取，
    # 否则 token 冷缓存时一次 get_token 就可能占用 ~REQUEST_RETRY_TIMES
    # × timeout 的调度线程时间。
    now = time.time()

    with _token_lock:
        if (
            not force_refresh
            and _token_cache["token"]
            and now < _token_cache["expire"]
        ):
            return str(_token_cache["token"])

        if not config.APP_ID or not config.APP_SECRET:
            raise RuntimeError("缺少飞书环境变量 APP_ID 或 APP_SECRET")

        response = request_with_retry(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json_data={"app_id": config.APP_ID, "app_secret": config.APP_SECRET},
            attempts=attempts,
            timeout=timeout,
        )

        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("Token 接口返回了非 JSON 内容") from exc

        if response.status_code != 200 or result.get("code") != 0:
            raise RuntimeError(
                f"Token 获取失败: HTTP={response.status_code}, "
                f"code={result.get('code')}, msg={result.get('msg')}"
            )

        token = result["tenant_access_token"]
        expire_seconds = int(result.get("expire", 7200))
        _token_cache["token"] = token
        _token_cache["expire"] = now + max(
            60, expire_seconds - config.TOKEN_REFRESH_MARGIN_SECONDS
        )
        logger.info("tenant_access_token 获取成功")
        return str(token)
