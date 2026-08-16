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
        "source_temperature_unit": "SOURCE_TEMPERATURE_UNIT",
        "waitress_threads": "WAITRESS_THREADS",
    }
    for option_name, environment_name in option_names.items():
        value = options.get(option_name)
        if value not in (None, ""):
            os.environ.setdefault(environment_name, str(value))

    device_record_map = options.get("device_record_map")
    if device_record_map not in (None, ""):
        os.environ.setdefault("DEVICE_RECORD_MAP", str(device_record_map))


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

# 保持旧版默认行为：HA 当前发送华氏温度，由服务转换为摄氏温度。
SOURCE_TEMPERATURE_UNIT = os.getenv("SOURCE_TEMPERATURE_UNIT", "F").upper()
USE_SYSTEM_PROXY = _get_bool("USE_SYSTEM_PROXY", False)

REQUEST_TIMEOUT_SECONDS = _get_float("REQUEST_TIMEOUT_SECONDS", 10.0)
REQUEST_RETRY_TIMES = _get_int("REQUEST_RETRY_TIMES", 3)
REQUEST_RETRY_BACKOFF_SECONDS = _get_float("REQUEST_RETRY_BACKOFF_SECONDS", 0.8)
TOKEN_REFRESH_MARGIN_SECONDS = _get_int("TOKEN_REFRESH_MARGIN_SECONDS", 300)

LOG_MAX_BYTES = _get_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
LOG_BACKUP_COUNT = _get_int("LOG_BACKUP_COUNT", 5)


# 可通过 DEVICE_RECORD_MAP 注入设备映射，避免为每个部署者修改并提交 config.py。
# 格式：{"DEV-01":"recxxxx","DEV-02":"recyyyy"}
DEFAULT_DEVICES = {
    "TH-01": {"record_id": "YOUR EECORD ID"},
    "TH-02": {"record_id": "YOUR EECORD ID"},
    "TH-03": {"record_id": "YOUR EECORD ID"},
    "TH-04": {"record_id": "YOUR EECORD ID"},
    "TH-05": {"record_id": "YOUR EECORD ID"},
    "TH-06": {"record_id": "YOUR EECORD ID"},
    "TH-07": {"record_id": "YOUR EECORD ID"},
    "TH-08": {"record_id": "YOUR EECORD ID"},
    "TH-09": {"record_id": "YOUR EECORD ID"},
    "TH-10": {"record_id": "YOUR EECORD ID"},
    "TH-11": {"record_id": "YOUR EECORD ID"},
}


def _get_devices() -> dict[str, dict[str, str]]:
    raw_value = os.getenv("DEVICE_RECORD_MAP", "").strip()
    if not raw_value:
        return DEFAULT_DEVICES

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


DEVICES = _get_devices()


def ensure_runtime_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
