from __future__ import annotations

import logging
import threading
from typing import Any

import config
from services.http_client import request_with_retry
from services.token import clear_token, get_token


logger = logging.getLogger("temperature_monitor")
_feishu_write_lock = threading.Lock()


def _validate_bitable_config() -> None:
    if not config.APP_TOKEN or not config.TABLE_ID:
        raise RuntimeError("缺少飞书环境变量 APP_TOKEN 或 TABLE_ID")


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
