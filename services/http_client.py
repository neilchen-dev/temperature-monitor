from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config


logger = logging.getLogger("temperature_monitor")


def create_http_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = config.USE_SYSTEM_PROXY
    if not config.USE_SYSTEM_PROXY:
        session.proxies.clear()
    return session


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_data: dict[str, Any] | None = None,
) -> requests.Response:
    request_headers = dict(headers or {})
    request_headers.setdefault("Connection", "close")
    last_exception: BaseException | None = None

    for attempt in range(1, config.REQUEST_RETRY_TIMES + 1):
        try:
            with create_http_session() as session:
                response = session.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    json=json_data,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )

            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
            if retryable and attempt < config.REQUEST_RETRY_TIMES:
                logger.warning(
                    "飞书 HTTP 暂时异常，准备重试 | HTTP=%s | attempt=%s/%s",
                    response.status_code,
                    attempt,
                    config.REQUEST_RETRY_TIMES,
                )
                time.sleep(config.REQUEST_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            return response

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exception = exc
            if attempt < config.REQUEST_RETRY_TIMES:
                logger.warning(
                    "飞书网络异常，准备重试 | attempt=%s/%s | error=%s",
                    attempt,
                    config.REQUEST_RETRY_TIMES,
                    exc,
                )
                time.sleep(config.REQUEST_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue

    raise requests.exceptions.ConnectionError(
        f"请求连续失败 {config.REQUEST_RETRY_TIMES} 次: {last_exception}"
    )
