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
    attempts: int | None = None,
    timeout: float | None = None,
) -> requests.Response:
    """One HTTP call with bounded retry.

    ``attempts``/``timeout`` override the config defaults for callers that
    must stay strictly bounded (scheduler FEISHU_PROJECTION handlers:
    attempts=1 so a single task can never run a long internal retry loop;
    the scheduler itself owns the retry cadence via exponential backoff).
    """
    request_headers = dict(headers or {})
    request_headers.setdefault("Connection", "close")
    last_exception: BaseException | None = None
    retry_times = attempts if attempts is not None else config.REQUEST_RETRY_TIMES
    request_timeout = timeout if timeout is not None else config.REQUEST_TIMEOUT_SECONDS

    for attempt in range(1, retry_times + 1):
        try:
            with create_http_session() as session:
                response = session.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    json=json_data,
                    timeout=request_timeout,
                )

            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
            if retryable and attempt < retry_times:
                logger.warning(
                    "飞书 HTTP 暂时异常，准备重试 | HTTP=%s | attempt=%s/%s",
                    response.status_code,
                    attempt,
                    retry_times,
                )
                time.sleep(config.REQUEST_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            return response

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exception = exc
            if attempt < retry_times:
                logger.warning(
                    "飞书网络异常，准备重试 | attempt=%s/%s | error=%s",
                    attempt,
                    retry_times,
                    exc,
                )
                time.sleep(config.REQUEST_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue

    raise requests.exceptions.ConnectionError(
        f"请求连续失败 {retry_times} 次: {last_exception}"
    )
