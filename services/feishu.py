from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import urlencode

import config
from services.http_client import request_with_retry
from services.token import clear_token, get_token


logger = logging.getLogger("temperature_monitor")
_feishu_write_lock = threading.Lock()
_record_id_cache: dict[str, str] = {}
_record_id_lock = threading.Lock()


def _validate_bitable_config() -> None:
    if not config.APP_TOKEN or not config.TABLE_ID:
        raise RuntimeError("缺少飞书环境变量 APP_TOKEN 或 TABLE_ID")


def _normalize_field_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_normalize_field_value(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text", value.get("name", ""))).strip()
    return str(value or "").strip()


def resolve_record_id(device: str, configured_record_id: str | None = None) -> str:
    """Return a mapped record ID or discover it from the Bitable device field."""
    if configured_record_id:
        return configured_record_id

    normalized_device = device.strip().upper()
    with _record_id_lock:
        cached_record_id = _record_id_cache.get(normalized_device)
    if cached_record_id:
        return cached_record_id

    _validate_bitable_config()
    base_url = (
        "https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{config.APP_TOKEN}/tables/{config.TABLE_ID}/records"
    )

    for auth_attempt in range(2):
        token = get_token(force_refresh=(auth_attempt == 1))
        page_token: str | None = None

        while True:
            query = {"page_size": "500"}
            if page_token:
                query["page_token"] = page_token
            response = request_with_retry(
                "GET",
                f"{base_url}?{urlencode(query)}",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                result = response.json()
            except ValueError as exc:
                raise RuntimeError("飞书查询记录接口返回了非 JSON 内容") from exc

            if result.get("code") == 99991663 and auth_attempt == 0:
                logger.warning("Token 无效，清空缓存后重试记录识别")
                clear_token()
                break
            if response.status_code != 200 or result.get("code") != 0:
                raise RuntimeError(
                    "飞书查询记录失败: "
                    f"HTTP={response.status_code}, code={result.get('code')}, "
                    f"msg={result.get('msg')}"
                )

            data = result.get("data", {})
            for record in data.get("items", []):
                fields = record.get("fields", {})
                field_value = _normalize_field_value(fields.get(config.DEVICE_ID_FIELD))
                if field_value.upper() == normalized_device:
                    record_id = str(record.get("record_id", "")).strip()
                    if not record_id:
                        raise RuntimeError("飞书返回的匹配记录缺少 record_id")
                    with _record_id_lock:
                        _record_id_cache[normalized_device] = record_id
                    logger.info(
                        "自动识别飞书 record_id 成功 | device=%s | field=%s",
                        normalized_device,
                        config.DEVICE_ID_FIELD,
                    )
                    return record_id

            if not data.get("has_more"):
                raise RuntimeError(
                    f"未在飞书表中找到设备 {normalized_device}；"
                    f"请检查字段 {config.DEVICE_ID_FIELD} 或 DEVICE_RECORD_MAP"
                )
            page_token = str(data.get("page_token", "")).strip()
            if not page_token:
                raise RuntimeError("飞书记录分页响应缺少 page_token")

    raise RuntimeError("飞书记录自动识别失败")


def update_feishu_fields(record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    _validate_bitable_config()
    url = (
        "https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{config.APP_TOKEN}/tables/{config.TABLE_ID}/records/{record_id}"
    )

    with _feishu_write_lock:
        for auth_attempt in range(2):
            token = get_token(force_refresh=(auth_attempt == 1))
            response = request_with_retry(
                "PUT",
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json_data={"fields": fields},
            )

            try:
                result = response.json()
            except ValueError:
                return {"code": -2, "msg": "飞书更新接口返回了非 JSON 内容"}

            if response.status_code == 200 and result.get("code") == 0:
                return result

            # 保持旧版行为：飞书返回 Token 无效后，清缓存并自动刷新一次。
            if result.get("code") == 99991663 and auth_attempt == 0:
                logger.warning("Token 无效，清空缓存后重试")
                clear_token()
                continue

            return result

    return {"code": -3, "msg": "飞书更新失败"}
