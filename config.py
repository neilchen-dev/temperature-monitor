from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


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


# 设备与飞书多维表格记录的映射保持原版本不变。
DEVICES = {
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


def ensure_runtime_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
