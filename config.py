from __future__ import annotations

import os
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
HASSIO_OPTIONS_PATH = Path(os.getenv("HASSIO_OPTIONS_PATH", "/data/options.json"))


def _load_hassio_options() -> None:
    """Load Home Assistant Add-on options into the existing environment-based config."""
    if not HASSIO_OPTIONS_PATH.is_file():
        return

    try:
        options = json.loads(HASSIO_OPTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取 Home Assistant Add-on 配置") from exc

    if not isinstance(options, dict):
        raise ValueError("Home Assistant Add-on 配置必须是 JSON 对象")

    option_names = {
        "app_id": "APP_ID",
        "app_secret": "APP_SECRET",
        "app_token": "APP_TOKEN",
        "table_id": "TABLE_ID",
        "history_api_key": "HISTORY_API_KEY",
        "history_interval_minutes": "HISTORY_INTERVAL_MINUTES",
        "history_timezone": "HISTORY_TIMEZONE",
        "history_cleanup_enabled": "HISTORY_CLEANUP_ENABLED",
        "history_retention_days": "HISTORY_RETENTION_DAYS",
        "history_cleanup_hour": "HISTORY_CLEANUP_HOUR",
        "source_temperature_unit": "SOURCE_TEMPERATURE_UNIT",
        "waitress_threads": "WAITRESS_THREADS",
        "device_id_field": "DEVICE_ID_FIELD",
    }
    for option_name, environment_name in option_names.items():
        value = options.get(option_name)
        if value not in (None, ""):
            os.environ.setdefault(environment_name, str(value))

    device_record_map = options.get("device_record_map")
    if device_record_map not in (None, ""):
        os.environ.setdefault("DEVICE_RECORD_MAP", str(device_record_map))

    device_name_map = options.get("device_name_map")
    if device_name_map not in (None, ""):
        os.environ.setdefault("DEVICE_NAME_MAP", str(device_name_map))

    history_table_map = options.get("history_table_map")
    if history_table_map not in (None, ""):
        os.environ.setdefault("HISTORY_TABLE_MAP", str(history_table_map))


_load_hassio_options()


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _get_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# 飞书凭据只从运行环境读取，默认空值方便本地启动和健康检查。
APP_ID = os.getenv("APP_ID", "")
APP_SECRET = os.getenv("APP_SECRET", "")
APP_TOKEN = os.getenv("APP_TOKEN", "")
TABLE_ID = os.getenv("TABLE_ID", "")

# 运行配置。容器中的默认路径自然对应 /app/data 与 /app/logs。
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _get_int("PORT", 5000)
WAITRESS_THREADS = _get_int("WAITRESS_THREADS", 4)
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))

# Home Assistant commonly reports Celsius; values are stored in Celsius.
SOURCE_TEMPERATURE_UNIT = os.getenv("SOURCE_TEMPERATURE_UNIT", "C").upper()
USE_SYSTEM_PROXY = _get_bool("USE_SYSTEM_PROXY", False)

REQUEST_TIMEOUT_SECONDS = _get_float("REQUEST_TIMEOUT_SECONDS", 10.0)
REQUEST_RETRY_TIMES = _get_int("REQUEST_RETRY_TIMES", 3)
REQUEST_RETRY_BACKOFF_SECONDS = _get_float("REQUEST_RETRY_BACKOFF_SECONDS", 0.8)
TOKEN_REFRESH_MARGIN_SECONDS = _get_int("TOKEN_REFRESH_MARGIN_SECONDS", 300)

LOG_MAX_BYTES = _get_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
LOG_BACKUP_COUNT = _get_int("LOG_BACKUP_COUNT", 5)

# 历史快照由 Home Assistant 每十分钟触发一次。删除默认硬关闭，只有重启后
# 读取到 HISTORY_CLEANUP_ENABLED=true 才可能执行，HTTP 请求不能覆盖此配置。
HISTORY_API_KEY = os.getenv("HISTORY_API_KEY", "").strip()
HISTORY_INTERVAL_MINUTES = _get_int("HISTORY_INTERVAL_MINUTES", 10)
HISTORY_TIMEZONE = os.getenv("HISTORY_TIMEZONE", "Asia/Shanghai").strip()
HISTORY_CLEANUP_ENABLED = _get_bool("HISTORY_CLEANUP_ENABLED", False)
HISTORY_RETENTION_DAYS = _get_int("HISTORY_RETENTION_DAYS", 90)
HISTORY_CLEANUP_HOUR = _get_int("HISTORY_CLEANUP_HOUR", 2)


# DEVICE_RECORD_MAP 是可选手动覆盖项；未配置的设备会按设备编号字段自动查找。
# 格式：{"DEV-01":"recxxxx","DEV-02":"recyyyy"}
DEVICE_ID_FIELD = os.getenv("DEVICE_ID_FIELD", "设备编号").strip() or "设备编号"


def _get_devices() -> dict[str, dict[str, str]]:
    raw_value = os.getenv("DEVICE_RECORD_MAP", "").strip()
    if not raw_value:
        return {}

    try:
        mapping = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("DEVICE_RECORD_MAP 必须是有效的 JSON 对象") from exc

    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("DEVICE_RECORD_MAP 必须是非空 JSON 对象")

    devices: dict[str, dict[str, str]] = {}
    for device, record_id in mapping.items():
        normalized_device = str(device).strip().upper()
        normalized_record_id = str(record_id).strip()
        if not normalized_device or not normalized_record_id:
            raise ValueError("DEVICE_RECORD_MAP 中的设备名和 record_id 不能为空")
        devices[normalized_device] = {"record_id": normalized_record_id}
    return devices


def _get_device_name_map() -> dict[str, str]:
    raw_value = os.getenv("DEVICE_NAME_MAP", "").strip()
    if not raw_value:
        return {}

    try:
        mapping = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("DEVICE_NAME_MAP 必须是有效的 JSON 对象") from exc

    if not isinstance(mapping, dict):
        raise ValueError("DEVICE_NAME_MAP 必须是 JSON 对象")

    normalized_mapping: dict[str, str] = {}
    for source_name, bitable_name in mapping.items():
        source = str(source_name).strip().upper()
        target = str(bitable_name).strip().upper()
        if not source or not target:
            raise ValueError("DEVICE_NAME_MAP 中的设备名不能为空")
        normalized_mapping[source] = target
    return normalized_mapping


def _get_history_table_map() -> dict[str, str]:
    raw_value = os.getenv("HISTORY_TABLE_MAP", "").strip()
    if not raw_value:
        return {}

    try:
        mapping = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("HISTORY_TABLE_MAP 必须是有效的 JSON 对象") from exc

    if not isinstance(mapping, dict):
        raise ValueError("HISTORY_TABLE_MAP 必须是 JSON 对象")

    normalized_mapping: dict[str, str] = {}
    for device, table_id in mapping.items():
        normalized_device = str(device).strip().upper()
        normalized_table_id = str(table_id).strip()
        if not normalized_device or not normalized_table_id:
            raise ValueError("HISTORY_TABLE_MAP 中的设备名和 table_id 不能为空")
        normalized_mapping[normalized_device] = normalized_table_id
    return normalized_mapping


DEVICES = _get_devices()
DEVICE_NAME_MAP = _get_device_name_map()
HISTORY_TABLE_MAP = _get_history_table_map()


def ensure_runtime_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
