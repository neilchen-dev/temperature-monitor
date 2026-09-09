from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from typing import Any, Mapping
from urllib.parse import urlencode

import config
from services.http_client import request_with_retry
from services.token import clear_token, get_token


logger = logging.getLogger("temperature_monitor")
# 串行化粒度是"资源"（某张表或某条记录），而不是全局：不同设备写不同
# record、不同历史表之间可以并行，慢请求的重试退避只阻塞同一资源。
_resource_locks: dict[str, threading.RLock] = {}
_resource_locks_guard = threading.Lock()
_record_id_cache: dict[str, tuple[str, float]] = {}
_record_not_found_until: dict[str, float] = {}
_record_id_lock = threading.Lock()
_RECORD_ID_CACHE_TTL_SECONDS = 3600
_RECORD_NOT_FOUND_TTL_SECONDS = 300
_TRANSIENT_BITABLE_CODES = {1254290, 1254291, 1254607, 1255040}


def _resource_lock(key: str) -> threading.RLock:
    with _resource_locks_guard:
        lock = _resource_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _resource_locks[key] = lock
        return lock


def _validate_bitable_config(*, require_default_table: bool = False) -> None:
    if not config.APP_TOKEN:
        raise RuntimeError("缺少飞书环境变量 APP_TOKEN 或 FEISHU_BASE_APP_TOKEN")
    if require_default_table and not config.TABLE_ID:
        raise RuntimeError("缺少飞书环境变量 TABLE_ID 或 FEISHU_DEVICE_TABLE_ID")


def normalize_client_token(client_token: Any | None) -> str:
    """Return one stable, Feishu-compatible idempotency token.

    Caller-owned business keys map deterministically to UUIDv4-formatted values
    derived from SHA-256.  A canonical UUIDv4 remains unchanged when it crosses
    another adapter layer; UUIDs of other versions are treated as business keys
    and remapped.  A missing key receives a fresh random UUIDv4 because it does
    not represent a retryable business identity.
    """
    token = "" if client_token is None else str(client_token)
    if not token:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(token)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.version == 4 and str(parsed) == token:
        return token
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def normalize_field_value(value: Any) -> str:
    """Convert Feishu rich/select values into stable text for history fields."""
    if isinstance(value, list):
        return " ".join(
            item for item in (normalize_field_value(item) for item in value) if item
        )
    if isinstance(value, dict):
        # Feishu formula fields return their displayed value in a nested
        # ``value`` rich-text array, rather than at the top-level ``text``.
        # Normalize it recursively so history stores the displayed result as
        # immutable plain text.
        if "value" in value:
            return normalize_field_value(value["value"])
        return str(value.get("text", value.get("name", ""))).strip()
    return str(value or "").strip()


def _bitable_table_url(table_id: str, suffix: str = "") -> str:
    normalized_table_id = str(table_id).strip()
    if not normalized_table_id:
        raise RuntimeError("飞书 table_id 不能为空")
    return (
        "https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{config.APP_TOKEN}/tables/{normalized_table_id}{suffix}"
    )


def _request_bitable_json(
    method: str,
    url: str,
    *,
    operation: str,
    json_data: dict[str, Any] | None = None,
    lock_key: str,
    max_attempts: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Call Bitable serialized per resource; retry token, rate-limit, conflicts.

    ``max_attempts``/``timeout`` bound the whole operation (token fetch
    included) to a single short attempt — used by scheduler
    FEISHU_PROJECTION handlers so one task can never run a long internal
    retry loop on the single-threaded scheduler. Default (None) keeps the
    legacy full-retry behaviour for the inline /temperature path.
    """
    attempts_limit = (
        max_attempts if max_attempts is not None
        else max(1, config.REQUEST_RETRY_TIMES)
    )
    attempt = 1
    token_refreshed = False

    with _resource_lock(lock_key):
        while attempt <= attempts_limit:
            token = get_token(attempts=max_attempts, timeout=timeout)
            headers = {"Authorization": f"Bearer {token}"}
            if json_data is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"

            response = request_with_retry(
                method,
                url,
                headers=headers,
                json_data=json_data,
                attempts=max_attempts,
                timeout=timeout,
            )
            try:
                result = response.json()
            except ValueError:
                return {
                    "code": -2,
                    "msg": f"飞书{operation}接口返回了非 JSON 内容",
                    "http_status": response.status_code,
                }

            code = int(result.get("code", -1))
            if (
                code == 99991663
                and not token_refreshed
                and (max_attempts is None or attempt < attempts_limit)
            ):
                # 有界模式（max_attempts=1）下不做 token 刷新重试：刷新
                # 会额外增加一次网络调用，破坏单次 handler 的耗时可预算
                # 性；token 失效直接按失败返回，交给 backoff 调度下一次。
                logger.warning("Token 无效，清空缓存后重试 | operation=%s", operation)
                clear_token()
                token_refreshed = True
                continue

            transient = (
                response.status_code == 429
                or response.status_code >= 500
                or code in _TRANSIENT_BITABLE_CODES
            )
            if transient and attempt < attempts_limit:
                delay = config.REQUEST_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "飞书业务暂时异常，准备重试 | operation=%s | HTTP=%s | "
                    "code=%s | attempt=%s/%s",
                    operation,
                    response.status_code,
                    code,
                    attempt,
                    attempts_limit,
                )
                time.sleep(delay)
                attempt += 1
                continue

            if response.status_code != 200 and code == 0:
                result = dict(result)
                result["code"] = -response.status_code
                result["msg"] = f"HTTP {response.status_code}"
            return result

    return {"code": -3, "msg": f"飞书{operation}失败"}


class FeishuAPIError(RuntimeError):
    """A Feishu response that did not confirm a successful operation."""

    def __init__(self, result: Mapping[str, Any], operation: str) -> None:
        self.operation = operation
        self.code = int(result.get("code", -1))
        self.http_status = result.get("http_status")
        self.result = dict(result)
        super().__init__(
            f"飞书{operation}失败: code={result.get('code')}, msg={result.get('msg')}"
        )

    @property
    def outcome_unknown(self) -> bool:
        """Whether the server may have committed the request."""
        return (
            (
                self.http_status is not None
                and (
                    int(self.http_status) == 429
                    or int(self.http_status) >= 500
                )
            )
            or self.code in _TRANSIENT_BITABLE_CODES
            or self.code < 0
        )


class FeishuIMError(RuntimeError):
    """A Feishu IM send that did not produce an auditable message id."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool,
        outcome_unknown: bool = False,
        http_status: int | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.http_status = http_status
        self.result = dict(result or {})
        super().__init__(message)


class FeishuIMClient:
    """Minimal tenant-token client for the official Feishu IM API.

    The scheduler owns retry cadence.  A notification task therefore invokes
    this client with ``max_attempts=1`` so a single task attempt does one
    bounded network request and can be retried with the same UUID later.
    """

    _ALLOWED_RECEIVE_ID_TYPES = {"open_id", "user_id", "union_id", "email", "chat_id"}

    def __init__(
        self,
        *,
        base_url: str = "https://open.feishu.cn",
    ) -> None:
        self.base_url = base_url.rstrip("/")

    def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        normalized_id = str(receive_id).strip()
        normalized_type = str(receive_id_type).strip().lower()
        if not normalized_id:
            raise FeishuIMError(
                "Feishu IM recipient is empty",
                error_code="invalid_recipient",
                retryable=False,
                outcome_unknown=False,
            )
        if normalized_type not in self._ALLOWED_RECEIVE_ID_TYPES:
            raise FeishuIMError(
                f"unsupported Feishu receive_id_type: {normalized_type}",
                error_code="invalid_receive_id_type",
                retryable=False,
                outcome_unknown=False,
            )
        if not str(text).strip():
            raise FeishuIMError(
                "Feishu IM message text is empty",
                error_code="invalid_message",
                retryable=False,
                outcome_unknown=False,
            )
        try:
            token = get_token(attempts=max_attempts, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - classify token transport/config errors
            message = str(exc)
            lowered = message.lower()
            retryable = any(
                marker in lowered
                for marker in ("timeout", "timed out", "connection", "tempor")
            )
            raise FeishuIMError(
                f"Feishu tenant token unavailable: {message}",
                error_code="token_unavailable",
                retryable=retryable,
                outcome_unknown=False,
            ) from exc

        url = (
            f"{self.base_url}/open-apis/im/v1/messages?"
            f"{urlencode({'receive_id_type': normalized_type})}"
        )
        body = {
            "receive_id": normalized_id,
            "msg_type": "text",
            "content": json.dumps({"text": str(text)}, ensure_ascii=False),
            "uuid": normalize_client_token(idempotency_key),
        }
        try:
            response = request_with_retry(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json_data=body,
                # Do not hide scheduler backoff behind this API helper.
                attempts=max_attempts,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport outcome is ambiguous
            raise FeishuIMError(
                f"Feishu IM transport failed: {exc}",
                error_code="network_error",
                retryable=True,
                outcome_unknown=True,
            ) from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise FeishuIMError(
                "Feishu IM returned non-JSON content",
                error_code="invalid_response",
                retryable=False,
                outcome_unknown=True,
                http_status=response.status_code,
            ) from exc
        if not isinstance(result, Mapping):
            raise FeishuIMError(
                "Feishu IM response is not an object",
                error_code="invalid_response",
                retryable=False,
                outcome_unknown=True,
                http_status=response.status_code,
            )

        code = int(result.get("code", -1))
        if response.status_code != 200 or code != 0:
            status = int(response.status_code)
            retryable = status == 429 or status >= 500 or code in _TRANSIENT_BITABLE_CODES
            if code == 99991663:
                clear_token()
                retryable = True
            if status in {400, 401, 403, 404} and code not in _TRANSIENT_BITABLE_CODES:
                retryable = False
            error_code = (
                "rate_limited" if status == 429 or code in _TRANSIENT_BITABLE_CODES
                else "permission_denied" if status == 403
                else "unauthorized" if status == 401
                else "invalid_request" if 400 <= status < 500
                else "feishu_http_error"
            )
            raise FeishuIMError(
                f"Feishu IM send failed: HTTP={status}, code={code}, "
                f"msg={result.get('msg')}",
                error_code=error_code,
                retryable=retryable,
                outcome_unknown=retryable or status >= 500,
                http_status=status,
                result=result,
            )

        data = result.get("data")
        message_id = data.get("message_id") if isinstance(data, Mapping) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise FeishuIMError(
                "Feishu IM success response is missing message_id",
                error_code="message_id_missing",
                retryable=False,
                outcome_unknown=True,
                http_status=response.status_code,
                result=result,
            )
        return result

def _require_success(result: dict[str, Any], operation: str) -> dict[str, Any]:
    if int(result.get("code", -1)) != 0:
        raise FeishuAPIError(result, operation)
    return result


def list_bitable_records(
    table_id: str,
    *,
    field_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Read all records from an explicitly configured Base table.

    This is the read primitive for the integration layer.  It uses the same
    token, retry, and pagination handling as the write primitives below.
    """
    _validate_bitable_config()
    base_url = _bitable_table_url(table_id, "/records/search")
    page_token: str | None = None
    records: list[dict[str, Any]] = []
    body: dict[str, Any] = {}
    if field_names:
        body["field_names"] = list(field_names)

    while True:
        query = {"page_size": "500"}
        if page_token:
            query["page_token"] = page_token
        result = _require_success(
            _request_bitable_json(
                "POST",
                f"{base_url}?{urlencode(query)}",
                operation="读取 Base 记录",
                json_data=body,
                lock_key=f"table:{table_id}:read",
            ),
            "读取 Base 记录",
        )
        data = result.get("data", {})
        records.extend(data.get("items", []))
        if not data.get("has_more"):
            return records
        page_token = str(data.get("page_token", "")).strip()
        if not page_token:
            raise RuntimeError("飞书 Base 记录分页响应缺少 page_token")


def resolve_record_id(
    device: str,
    configured_record_id: str | None = None,
    *,
    attempt_timeout: float | None = None,
) -> str:
    """Return a mapped record ID or discover it from the Bitable device field.

    ``attempt_timeout`` bounds each network call to a single short attempt
    (scheduler FEISHU_PROJECTION handlers only); the inline /temperature
    path keeps the full retry behaviour.
    """
    if configured_record_id:
        return configured_record_id

    normalized_device = device.strip().upper()
    now = time.time()
    with _record_id_lock:
        cached = _record_id_cache.get(normalized_device)
        if cached and now < cached[1]:
            return cached[0]
        not_found_until = _record_not_found_until.get(normalized_device)
        if not_found_until and now < not_found_until:
            raise RuntimeError(
                f"未在飞书表中找到设备 {normalized_device}（负缓存生效中，最多 "
                f"{_RECORD_NOT_FOUND_TTL_SECONDS} 秒后重查）；"
                f"请检查字段 {config.DEVICE_ID_FIELD} 或 DEVICE_RECORD_MAP"
            )

    _validate_bitable_config(require_default_table=True)
    base_url = _bitable_table_url(config.TABLE_ID, "/records")
    page_token: str | None = None
    matching_record_ids: list[str] = []

    while True:
        query = {"page_size": "500"}
        if page_token:
            query["page_token"] = page_token
        result = _require_success(
            _request_bitable_json(
                "GET",
                f"{base_url}?{urlencode(query)}",
                operation="查询记录",
                lock_key=f"table:{config.TABLE_ID}",
                max_attempts=1 if attempt_timeout is not None else None,
                timeout=attempt_timeout,
            ),
            "查询记录",
        )

        data = result.get("data", {})
        for record in data.get("items", []):
            fields = record.get("fields", {})
            field_value = normalize_field_value(fields.get(config.DEVICE_ID_FIELD))
            if field_value.upper() == normalized_device:
                record_id = str(record.get("record_id", "")).strip()
                if not record_id:
                    raise RuntimeError("飞书返回的匹配记录缺少 record_id")
                matching_record_ids.append(record_id)

        if not data.get("has_more"):
            if len(matching_record_ids) == 1:
                record_id = matching_record_ids[0]
                with _record_id_lock:
                    _record_id_cache[normalized_device] = (
                        record_id,
                        time.time() + _RECORD_ID_CACHE_TTL_SECONDS,
                    )
                    _record_not_found_until.pop(normalized_device, None)
                logger.info(
                    "自动识别飞书 record_id 成功 | device=%s | field=%s",
                    normalized_device,
                    config.DEVICE_ID_FIELD,
                )
                return record_id
            if len(matching_record_ids) > 1:
                record_ids = ", ".join(matching_record_ids)
                raise RuntimeError(
                    f"飞书表中设备编号 {normalized_device} 重复；"
                    f"请保留唯一记录后重试。重复 record_id: {record_ids}"
                )
            # 未找到：短暂负缓存，避免未知设备名反复触发全表扫描。
            with _record_id_lock:
                _record_not_found_until[normalized_device] = (
                    time.time() + _RECORD_NOT_FOUND_TTL_SECONDS
                )
            raise RuntimeError(
                f"未在飞书表中找到设备 {normalized_device}；"
                f"请检查字段 {config.DEVICE_ID_FIELD} 或 DEVICE_RECORD_MAP"
            )
        page_token = str(data.get("page_token", "")).strip()
        if not page_token:
            raise RuntimeError("飞书记录分页响应缺少 page_token")


def update_feishu_fields(
    record_id: str,
    fields: dict[str, Any],
    *,
    attempt_timeout: float | None = None,
) -> dict[str, Any]:
    return update_bitable_record(
        config.TABLE_ID,
        record_id,
        fields,
        attempt_timeout=attempt_timeout,
    )


def create_bitable_record(
    table_id: str,
    fields: dict[str, Any],
    *,
    client_token: str | None = None,
) -> dict[str, Any]:
    """Create one Base record with an optional caller-owned idempotency token."""
    _validate_bitable_config()
    normalized_table_id = str(table_id).strip()
    if not normalized_table_id:
        raise ValueError("table_id 不能为空")
    if not isinstance(fields, dict):
        raise TypeError("fields 必须是 JSON object")
    normalized_token = normalize_client_token(client_token)
    query = urlencode({"client_token": normalized_token})
    return _require_success(
        _request_bitable_json(
            "POST",
            f"{_bitable_table_url(normalized_table_id, '/records')}?{query}",
            operation="新增 Base 记录",
            json_data={"fields": dict(fields)},
            lock_key=f"table:{normalized_table_id}",
            max_attempts=1,
        ),
        "新增 Base 记录",
    )


def update_bitable_record(
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
    *,
    attempt_timeout: float | None = None,
) -> dict[str, Any]:
    """Update one Base record in an explicitly configured table.

    ``attempt_timeout`` bounds the update to a single short network attempt
    (scheduler FEISHU_PROJECTION handlers only).
    """
    _validate_bitable_config()
    normalized_table_id = str(table_id).strip()
    normalized_record_id = str(record_id).strip()
    if not normalized_table_id:
        raise ValueError("table_id 不能为空")
    if not normalized_record_id:
        raise ValueError("record_id 不能为空")
    if not isinstance(fields, dict):
        raise TypeError("fields 必须是 JSON object")
    return _require_success(
        _request_bitable_json(
            "PUT",
            _bitable_table_url(
                normalized_table_id,
                f"/records/{normalized_record_id}",
            ),
            operation="更新 Base 记录",
            json_data={"fields": dict(fields)},
            lock_key=f"record:{normalized_record_id}",
            max_attempts=1 if attempt_timeout is not None else None,
            timeout=attempt_timeout,
        ),
        "更新 Base 记录",
    )


def list_realtime_snapshots(field_names: list[str]) -> list[dict[str, Any]]:
    """Read all current records with the minimum fields needed for a snapshot."""
    _validate_bitable_config(require_default_table=True)
    base_url = _bitable_table_url(config.TABLE_ID, "/records/search")
    page_token: str | None = None
    records: list[dict[str, Any]] = []

    while True:
        query = {"page_size": "500"}
        if page_token:
            query["page_token"] = page_token
        result = _require_success(
            _request_bitable_json(
                "POST",
                f"{base_url}?{urlencode(query)}",
                operation="读取实时快照",
                json_data={"field_names": field_names},
                lock_key=f"table:{config.TABLE_ID}:search",
            ),
            "读取实时快照",
        )
        data = result.get("data", {})
        records.extend(data.get("items", []))
        if not data.get("has_more"):
            return records
        page_token = str(data.get("page_token", "")).strip()
        if not page_token:
            raise RuntimeError("飞书实时快照分页响应缺少 page_token")


def get_latest_history_timestamp(table_id: str) -> Any | None:
    """Return the newest sampling time from a history table, or None if empty."""
    _validate_bitable_config()
    url = f"{_bitable_table_url(table_id, '/records/search')}?page_size=1"
    result = _require_success(
        _request_bitable_json(
            "POST",
            url,
            operation="查询最新历史记录",
            json_data={
                "field_names": ["采集时间"],
                "sort": [{"field_name": "采集时间", "desc": True}],
            },
            lock_key=f"table:{table_id}",
        ),
        "查询最新历史记录",
    )
    items = result.get("data", {}).get("items", [])
    if not items:
        return None
    return items[0].get("fields", {}).get("采集时间")


def create_history_record(table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Create one history record with an idempotency token reused by retries."""
    _validate_bitable_config()
    query = urlencode({"client_token": normalize_client_token(uuid.uuid4())})
    return _request_bitable_json(
        "POST",
        f"{_bitable_table_url(table_id, '/records')}?{query}",
        operation="新增历史记录",
        json_data={"fields": fields},
        lock_key=f"table:{table_id}",
    )


def find_expired_history_record_ids(
    table_id: str,
    cutoff_timestamp_ms: int,
) -> list[str]:
    """Cloud-filter history records strictly before a cutoff timestamp's date."""
    _validate_bitable_config()
    if isinstance(cutoff_timestamp_ms, bool) or cutoff_timestamp_ms <= 0:
        raise ValueError("历史清理截止时间必须是正整数毫秒时间戳")
    base_url = _bitable_table_url(table_id, "/records/search")
    page_token: str | None = None
    record_ids: list[str] = []
    body = {
        "field_names": ["采集时间"],
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": "采集时间",
                    "operator": "isLess",
                    "value": ["ExactDate", str(cutoff_timestamp_ms)],
                }
            ],
        },
    }

    while True:
        query = {"page_size": "500"}
        if page_token:
            query["page_token"] = page_token
        result = _require_success(
            _request_bitable_json(
                "POST",
                f"{base_url}?{urlencode(query)}",
                operation="筛选过期历史记录",
                json_data=body,
                lock_key=f"table:{table_id}",
            ),
            "筛选过期历史记录",
        )
        data = result.get("data", {})
        for item in data.get("items", []):
            record_id = str(item.get("record_id", "")).strip()
            if record_id:
                record_ids.append(record_id)
        if not data.get("has_more"):
            return record_ids
        page_token = str(data.get("page_token", "")).strip()
        if not page_token:
            raise RuntimeError("飞书过期记录分页响应缺少 page_token")


def delete_history_records(table_id: str, record_ids: list[str]) -> dict[str, Any]:
    """Delete one already-bounded batch; callers must enforce the feature gate."""
    if not record_ids:
        return {"code": 0, "msg": "no records"}
    if len(record_ids) > 500:
        raise ValueError("单次历史记录删除不得超过 500 条")
    _validate_bitable_config()
    return _request_bitable_json(
        "POST",
        _bitable_table_url(table_id, "/records/batch_delete"),
        operation="删除过期历史记录",
        json_data={"records": record_ids},
        lock_key=f"table:{table_id}",
    )
