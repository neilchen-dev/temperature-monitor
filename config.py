from __future__ import annotations

import json
import os
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
        "temperature_api_key": "TEMPERATURE_API_KEY",
        "history_devices": "HISTORY_DEVICES",
        "history_interval_minutes": "HISTORY_INTERVAL_MINUTES",
        "history_timezone": "HISTORY_TIMEZONE",
        "history_cleanup_enabled": "HISTORY_CLEANUP_ENABLED",
        "history_retention_days": "HISTORY_RETENTION_DAYS",
        "history_cleanup_hour": "HISTORY_CLEANUP_HOUR",
        "source_temperature_unit": "SOURCE_TEMPERATURE_UNIT",
        "sqlite_enabled": "SQLITE_ENABLED",
        "waitress_threads": "WAITRESS_THREADS",
        "device_id_field": "DEVICE_ID_FIELD",
        "modbus_enabled": "MODBUS_ENABLED",
        "modbus_transport": "MODBUS_TRANSPORT",
        "modbus_host": "MODBUS_HOST",
        "modbus_port": "MODBUS_PORT",
        "modbus_serial_port": "MODBUS_SERIAL_PORT",
        "modbus_baudrate": "MODBUS_BAUDRATE",
        "modbus_parity": "MODBUS_PARITY",
        "modbus_stopbits": "MODBUS_STOPBITS",
        "modbus_bytesize": "MODBUS_BYTESIZE",
        "modbus_unit_id": "MODBUS_UNIT_ID",
        "modbus_device_id": "MODBUS_DEVICE_ID",
        "modbus_poll_interval_seconds": "MODBUS_POLL_INTERVAL_SECONDS",
        "modbus_timeout_seconds": "MODBUS_TIMEOUT_SECONDS",
        "event_temperature_high_c": "EVENT_TEMPERATURE_HIGH_C",
        "automation_mode": "AUTOMATION_MODE",
        "shadow_device_ids": "SHADOW_DEVICE_IDS",
        "shadow_device_contexts": "SHADOW_DEVICE_CONTEXTS",
        "feishu_standard_table_id": "FEISHU_STANDARD_TABLE_ID",
        "feishu_operation_table_id": "FEISHU_OPERATION_TABLE_ID",
        "feishu_event_table_id": "FEISHU_EVENT_TABLE_ID",
        "feishu_operation_validation_field": "FEISHU_OPERATION_VALIDATION_FIELD",
        "feishu_operation_validation_value": "FEISHU_OPERATION_VALIDATION_VALUE",
        "feishu_operation_allowed_devices": "FEISHU_OPERATION_ALLOWED_DEVICES",
        "feishu_operation_interval_table_id": "FEISHU_OPERATION_INTERVAL_TABLE_ID",
        "feishu_inspection_table_id": "FEISHU_INSPECTION_TABLE_ID",
        "feishu_write_enabled": "FEISHU_WRITE_ENABLED",
        "active_cutover_ack": "ACTIVE_CUTOVER_ACK",
        "shadow_worker_id": "SHADOW_WORKER_ID",
        "shadow_scheduler_poll_seconds": "SHADOW_SCHEDULER_POLL_SECONDS",
        "shadow_operation_sync_seconds": "SHADOW_OPERATION_SYNC_SECONDS",
        "shadow_standard_sync_seconds": "SHADOW_STANDARD_SYNC_SECONDS",
        "shadow_feishu_delay_seconds": "SHADOW_FEISHU_DELAY_SECONDS",
        "runtime_shutdown_timeout_seconds": "RUNTIME_SHUTDOWN_TIMEOUT_SECONDS",
        "automation_run_retention_days": "AUTOMATION_RUN_RETENTION_DAYS",
        "device_model_stale_seconds": "DEVICE_MODEL_STALE_SECONDS",
        "temperature_dedupe_window_ms": "TEMPERATURE_DEDUPE_WINDOW_MS",
        "feishu_projection_max_retries": "FEISHU_PROJECTION_MAX_RETRIES",
        "feishu_projection_backoff_seconds": "FEISHU_PROJECTION_BACKOFF_SECONDS",
        "feishu_projection_inline_suppress_seconds": "FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS",
        "feishu_projection_attempt_timeout_seconds": "FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS",
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

    modbus_register_map = options.get("modbus_register_map")
    if modbus_register_map not in (None, ""):
        os.environ.setdefault("MODBUS_REGISTER_MAP", str(modbus_register_map))


_load_hassio_options()


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值: {raw!r}") from None


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，当前值: {raw!r}") from None


# 飞书凭据只从运行环境读取，默认空值方便本地启动和健康检查。
# FEISHU_* 是 Shadow Runtime 的显式命名；保留旧名称以兼容现有采集链路。
APP_ID = os.getenv("APP_ID") or os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("APP_SECRET") or os.getenv("FEISHU_APP_SECRET", "")
APP_TOKEN = os.getenv("APP_TOKEN") or os.getenv("FEISHU_BASE_APP_TOKEN", "")
TABLE_ID = os.getenv("TABLE_ID") or os.getenv("FEISHU_DEVICE_TABLE_ID", "")
FEISHU_DEVICE_TABLE_ID = TABLE_ID

# 运行配置。容器中的默认路径自然对应 /app/data 与 /app/logs。
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _get_int("PORT", 5000)
WAITRESS_THREADS = _get_int("WAITRESS_THREADS", 4)
# Home Assistant Add-on 中 /app/data 位于容器内、升级即丢；Supervisor 的
# /data 才是持久目录。检测到 Add-on 环境时默认切换，仍可用 DATA_DIR 覆盖。
IS_HOME_ASSISTANT_ADDON = HASSIO_OPTIONS_PATH.is_file()
DATA_DIR = Path(
    os.getenv("DATA_DIR")
    or ("/data" if IS_HOME_ASSISTANT_ADDON else str(BASE_DIR / "data"))
)
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

# SQLite 本地镜像：仅作为飞书数据的本地副本（温度上报 + 历史快照），
# 不改变飞书写入、去重与清理逻辑；写入失败只记日志并继续。
SQLITE_ENABLED = _get_bool("SQLITE_ENABLED", True)
SQLITE_DB_PATH = Path(
    os.getenv("SQLITE_DB_PATH", str(DATA_DIR / "temperature_monitor.db"))
)

# 温度上报接口的可选共享密钥；设置后 /temperature 必须携带
# X-Temperature-Key 头（或 X-History-Key），留空则保持无鉴权以兼容旧配置。
TEMPERATURE_API_KEY = os.getenv("TEMPERATURE_API_KEY", "").strip()

# 请求体大小上限，防止异常大的 payload 占用内存。
MAX_CONTENT_LENGTH = _get_int("MAX_CONTENT_LENGTH", 16 * 1024)

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


def _get_history_devices() -> tuple[str, ...]:
    """参与历史采样的设备列表；默认 TH-01～TH-11，可用逗号分隔列表覆盖。"""
    raw_value = os.getenv("HISTORY_DEVICES", "").strip()
    if not raw_value:
        return tuple(f"TH-{index:02d}" for index in range(1, 12))

    devices = [part.strip().upper() for part in raw_value.split(",")]
    devices = [device for device in devices if device]
    if not devices:
        raise ValueError("HISTORY_DEVICES 至少需要一个设备名")
    if len(set(devices)) != len(devices):
        raise ValueError("HISTORY_DEVICES 中的设备名不能重复")
    return tuple(devices)


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
HISTORY_DEVICES = _get_history_devices()
HISTORY_TABLE_MAP = _get_history_table_map()


def _get_optional_float(name: str) -> float | None:
    """可选数字配置：未设置或留空表示关闭对应功能。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，当前值: {raw!r}") from None


# ---- 工业数据采集（P0：Modbus TCP）----
# 默认关闭；未开启时系统行为与引入本模块之前完全一致。
# 采集线程只在单进程入口（app.run_server）启动一次；若未来迁移到多 worker
# 部署（如 gunicorn），只允许一个 worker 开启 MODBUS_ENABLED，否则会重复轮询。
MODBUS_ENABLED = _get_bool("MODBUS_ENABLED", False)
# 传输层：tcp（默认，向后兼容）| rtu（USB-RS485 / 串口变送器）。
MODBUS_TRANSPORT = os.getenv("MODBUS_TRANSPORT", "tcp").strip().lower() or "tcp"
MODBUS_HOST = os.getenv("MODBUS_HOST", "127.0.0.1").strip() or "127.0.0.1"
MODBUS_PORT = _get_int("MODBUS_PORT", 5020)
# RTU 专用：Windows 填 COM3，Linux 填 /dev/ttyUSB0；transport=rtu 时必填。
MODBUS_SERIAL_PORT = os.getenv("MODBUS_SERIAL_PORT", "").strip()
MODBUS_BAUDRATE = _get_int("MODBUS_BAUDRATE", 9600)
MODBUS_PARITY = os.getenv("MODBUS_PARITY", "N").strip().upper() or "N"
MODBUS_STOPBITS = _get_int("MODBUS_STOPBITS", 1)
MODBUS_BYTESIZE = _get_int("MODBUS_BYTESIZE", 8)
MODBUS_UNIT_ID = _get_int("MODBUS_UNIT_ID", 1)
MODBUS_DEVICE_ID = os.getenv("MODBUS_DEVICE_ID", "PLC-01").strip().upper() or "PLC-01"
MODBUS_POLL_INTERVAL_SECONDS = max(1.0, _get_float("MODBUS_POLL_INTERVAL_SECONDS", 5.0))
MODBUS_TIMEOUT_SECONDS = max(1.0, _get_float("MODBUS_TIMEOUT_SECONDS", 5.0))

# 可选 JSON 覆盖默认寄存器映射；留空使用内置布局（温度=保持寄存器0，int16×0.1；
# 湿度=寄存器1，uint16×0.1；设备状态=寄存器2，等于 online_value 视为在线）。
# 惰性解析：非法 JSON 只记录错误并禁用 Modbus 采集，不影响服务启动。
MODBUS_REGISTER_MAP = os.getenv("MODBUS_REGISTER_MAP", "").strip()

# 设备事件：温度超过该摄氏度阈值时产生 NORMAL -> TEMPERATURE_HIGH 事件，
# 回落后产生恢复事件。留空表示关闭温度阈值事件（在线/离线事件不受影响）。
EVENT_TEMPERATURE_HIGH_C = _get_optional_float("EVENT_TEMPERATURE_HIGH_C")


# ---- Shadow Runtime（默认不启用新领域链路）----
# Shadow 只能显式开启；尤其不能因为配置遗漏而进入 Active。
AUTOMATION_MODE = os.getenv("AUTOMATION_MODE", "disabled").strip().lower()
SHADOW_DEVICE_IDS = tuple(
    device.strip().upper()
    for device in os.getenv("SHADOW_DEVICE_IDS", "").split(",")
    if device.strip()
)
SHADOW_SCHEDULER_POLL_SECONDS = max(
    0.1, _get_float("SHADOW_SCHEDULER_POLL_SECONDS", 1.0)
)
SHADOW_OPERATION_SYNC_SECONDS = max(
    5.0, _get_float("SHADOW_OPERATION_SYNC_SECONDS", 30.0)
)
SHADOW_STANDARD_SYNC_SECONDS = max(
    30.0, _get_float("SHADOW_STANDARD_SYNC_SECONDS", 300.0)
)
SHADOW_FEISHU_DELAY_SECONDS = max(
    0.0, _get_float("SHADOW_FEISHU_DELAY_SECONDS", 60.0)
)
RUNTIME_SHUTDOWN_TIMEOUT_SECONDS = max(
    1.0, _get_float("RUNTIME_SHUTDOWN_TIMEOUT_SECONDS", 15.0)
)
# automation_runs / automation_tasks 只保留最近 N 天；0 表示禁用清理。
# SHADOW_COMPARE 每个采样一条 run，长跑会无限增长，必须给上限。
AUTOMATION_RUN_RETENTION_DAYS = max(
    0, _get_int("AUTOMATION_RUN_RETENTION_DAYS", 30)
)
# 统一设备模型断流判定阈值（秒）：/temperature 仍有上报但统一样本超过该
# 时间没有成功落库时，/api/system/status 标记 degraded。
DEVICE_MODEL_STALE_SECONDS = max(
    60, _get_int("DEVICE_MODEL_STALE_SECONDS", 300)
)
# ---- /temperature 可靠性（飞书投影解耦）----
# 同一 (device, source) 在该时间窗内提交内容完全相同（温度/湿度/在线状态）
# 的重复上报视为同一业务样本（HTTP retry / HA 重复触发），不重复落库、
# 不重复触发 Shadow。HA payload 无源端时间戳，只能用「内容 + 窗口」做
# 请求级去重；0 表示关闭。真实 HA 上报由状态变化触发，两个内容完全相同
# 且间隔数秒的请求几乎必然是重试而非两次真实采样。
TEMPERATURE_DEDUPE_WINDOW_MS = max(
    0, _get_int("TEMPERATURE_DEDUPE_WINDOW_MS", 5000)
)
# 飞书投影重试（scheduler 任务 FEISHU_PROJECTION）：
# - 最多重试次数（超过后状态置 failed，可被巡检发现；下一次 /temperature
#   在线投影成功即自动恢复）
# - 基础退避秒数，指数增长：30s, 60s, 120s, ...，上限 600s
# - 内联抑制窗口（秒）：投影刚失败后，短时间内到达的新上报不再同步等待
#   飞书（避免 Waitress 线程被连续失败拖满），直接返回 deferred
FEISHU_PROJECTION_MAX_RETRIES = max(
    1, _get_int("FEISHU_PROJECTION_MAX_RETRIES", 5)
)
FEISHU_PROJECTION_BACKOFF_SECONDS = max(
    1.0, _get_float("FEISHU_PROJECTION_BACKOFF_SECONDS", 30.0)
)
FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS = max(
    0.0, _get_float("FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS", 30.0)
)
# 单个 FEISHU_PROJECTION 任务内每次网络尝试的硬上限（秒）。
# Scheduler 是单线程串行执行：一个 projection handler 禁止内部再做
# 多轮长 retry loop（否则 11 台设备故障时 SHADOW_COMPARE / SYNC 会被
# 饿死）。有界模式下每次飞书调用恰好 1 次请求、该超时封顶；handler
# 最坏 = token + resolve + update 共 3 次有界调用（token 缓存命中时
# ≤ 2 次），失败立即返回，由 automation_tasks + exponential backoff
# 调度下一次尝试。普通 /temperature 内联路径不受影响（仍用完整重试）。
FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS = max(
    1.0, _get_float("FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS", 5.0)
)
# Active 切换三开关确认：AUTOMATION_MODE=active + FEISHU_WRITE_ENABLED=true
# + ACTIVE_CUTOVER_ACK=I_HAVE_DISABLED_LEGACY_FEISHU_WORKFLOWS 必须同时满足
# 才允许 Active 写回；确认字符串不匹配时 Runtime 降级为 disabled。
ACTIVE_CUTOVER_ACK_EXPECTED = "I_HAVE_DISABLED_LEGACY_FEISHU_WORKFLOWS"
ACTIVE_CUTOVER_ACK = os.getenv("ACTIVE_CUTOVER_ACK", "").strip()
SHADOW_WORKER_ID = os.getenv("SHADOW_WORKER_ID", "").strip()

FEISHU_STANDARD_TABLE_ID = os.getenv(
    "FEISHU_STANDARD_TABLE_ID", "tbl4S6Q0VOYjK92t"
).strip()
FEISHU_OPERATION_TABLE_ID = os.getenv(
    "FEISHU_OPERATION_TABLE_ID", "tbl3xFxhxnNlv4pm"
).strip()
FEISHU_EVENT_TABLE_ID = os.getenv(
    "FEISHU_EVENT_TABLE_ID", "tblc6uCFLGPZLcR6"
).strip()
FEISHU_OPERATION_DEVICE_FIELD = os.getenv(
    "FEISHU_OPERATION_DEVICE_FIELD", "监测点"
).strip()
FEISHU_OPERATION_AREA_FIELD = os.getenv(
    "FEISHU_OPERATION_AREA_FIELD", "区域"
).strip()
FEISHU_OPERATION_ACTION_FIELD = os.getenv(
    "FEISHU_OPERATION_ACTION_FIELD", "状态变更"
).strip()
FEISHU_OPERATION_TYPE_FIELD = os.getenv(
    "FEISHU_OPERATION_TYPE_FIELD", "当前工艺"
).strip()
FEISHU_OPERATION_WORK_ORDER_FIELD = os.getenv(
    "FEISHU_OPERATION_WORK_ORDER_FIELD", "工单号"
).strip()
FEISHU_OPERATION_VALIDATION_FIELD = os.getenv(
    "FEISHU_OPERATION_VALIDATION_FIELD", "登记组合校验"
).strip()
FEISHU_OPERATION_VALIDATION_VALUE = os.getenv(
    "FEISHU_OPERATION_VALIDATION_VALUE", "有效"
).strip()
FEISHU_OPERATION_ALLOWED_DEVICES = tuple(
    device.strip().upper()
    for device in os.getenv(
        "FEISHU_OPERATION_ALLOWED_DEVICES", "TH-03,TH-04,TH-05,TH-07"
    ).split(",")
    if device.strip()
)
FEISHU_OPERATION_INTERVAL_TABLE_ID = os.getenv(
    "FEISHU_OPERATION_INTERVAL_TABLE_ID", "tblZ9JHVhDCSqfhp"
).strip()
FEISHU_INSPECTION_TABLE_ID = os.getenv(
    "FEISHU_INSPECTION_TABLE_ID", "tblwkQNdeEan8LKf"
).strip()
FEISHU_WRITE_ENABLED = _get_bool("FEISHU_WRITE_ENABLED", False)
FEISHU_OBSERVATION_ALARM_FIELD = os.getenv(
    "FEISHU_OBSERVATION_ALARM_FIELD", "警报状态"
).strip()
FEISHU_OBSERVATION_OPERATION_FIELD = os.getenv(
    "FEISHU_OBSERVATION_OPERATION_FIELD", "当前作业状态"
).strip()
FEISHU_OBSERVATION_OPERATION_TYPE_FIELD = os.getenv(
    "FEISHU_OBSERVATION_OPERATION_TYPE_FIELD", "当前工艺"
).strip()
FEISHU_OBSERVATION_OVERALL_FIELD = os.getenv(
    "FEISHU_OBSERVATION_OVERALL_FIELD", "当前判定状态"
).strip()
FEISHU_OBSERVATION_TEMP_STATUS_FIELD = os.getenv(
    "FEISHU_OBSERVATION_TEMP_STATUS_FIELD", "温度判定"
).strip()
FEISHU_OBSERVATION_HUMIDITY_STATUS_FIELD = os.getenv(
    "FEISHU_OBSERVATION_HUMIDITY_STATUS_FIELD", "湿度判定"
).strip()
FEISHU_OBSERVATION_DATA_QUALITY_FIELD = os.getenv(
    "FEISHU_OBSERVATION_DATA_QUALITY_FIELD", "在线状态"
).strip()
FEISHU_OBSERVATION_STANDARD_ID_FIELD = os.getenv(
    "FEISHU_OBSERVATION_STANDARD_ID_FIELD", ""
).strip()
FEISHU_OBSERVATION_STANDARD_REVISION_FIELD = os.getenv(
    "FEISHU_OBSERVATION_STANDARD_REVISION_FIELD", ""
).strip()
FEISHU_EVENT_DEVICE_FIELD = os.getenv(
    "FEISHU_EVENT_DEVICE_FIELD", "监测点"
).strip()
FEISHU_EVENT_STATUS_FIELD = os.getenv(
    "FEISHU_EVENT_STATUS_FIELD", "处理状态"
).strip()


def _get_shadow_contexts() -> dict[str, dict[str, str]]:
    """Parse optional device context overrides for the Shadow whitelist."""
    raw_value = os.getenv("SHADOW_DEVICE_CONTEXTS", "").strip()
    if not raw_value:
        return {}
    try:
        mapping = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("SHADOW_DEVICE_CONTEXTS 必须是有效的 JSON 对象") from exc
    if not isinstance(mapping, dict):
        raise ValueError("SHADOW_DEVICE_CONTEXTS 必须是 JSON 对象")

    contexts: dict[str, dict[str, str]] = {}
    for device, raw_context in mapping.items():
        normalized_device = str(device).strip().upper()
        if not normalized_device or not isinstance(raw_context, dict):
            raise ValueError(
                "SHADOW_DEVICE_CONTEXTS 的每个设备值必须是非空 JSON 对象"
            )
        area = str(raw_context.get("area", "")).strip()
        control_type = str(raw_context.get("control_type", "")).strip()
        if not area:
            raise ValueError(f"SHADOW_DEVICE_CONTEXTS 缺少区域: {normalized_device}")
        contexts[normalized_device] = {
            "area": area,
            "control_type": control_type,
        }
    return contexts


SHADOW_DEVICE_CONTEXTS = _get_shadow_contexts()


def ensure_runtime_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
