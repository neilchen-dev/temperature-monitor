"""Modbus polling collector (pymodbus 3.15, sync client) over TCP or RTU.

Scope kept deliberately small:

- transport is chosen by ``ModbusEndpoint``: ``tcp`` (ModbusTcpClient) or
  ``rtu`` (ModbusSerialClient over USB-RS485 / serial). Both pymodbus clients
  share the same duck-typed surface (``connected`` / ``connect()`` /
  ``read_holding_registers`` / ``read_input_registers`` / ``close``), so the
  poll loop is transport-agnostic and both transports feed the *identical*
  unified device model (``source`` stays ``"modbus"``).
- register layout (env-overridable via MODBUS_REGISTER_MAP); every
  measurement field may live in holding (FC03, default) or input (FC04)
  registers; ``device_status`` is optional — without it, a successful read
  means the device is online.
- connectivity failures raise ``ModbusException``/``OSError`` from pymodbus
  and pyserial; ``poll_once`` converts every failure into a ``None`` return
  plus a log entry, so the poll loop can never crash the process.
- RTU reconnect is only guaranteed while the serial port path stays the same
  (e.g. ``COM3`` unplug/replug); if the OS re-enumerates the device to a new
  name, update MODBUS_SERIAL_PORT and restart — no auto-discovery.
- one RS485 bus is half-duplex: at most one poller per serial port (the
  single-endpoint config guarantees this; a future multi-drop loop must keep
  it).
- runtime stats (last success / last error) are health information for
  /api/system/status, not business state; they reset on restart.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger("temperature_monitor")

# pymodbus 是可选依赖：未安装时模块仍可导入（用于纯函数与配置校验），
# 只有真正构造 poller 时才要求安装，缺失由 collector 捕获并禁用采集。
try:
    from pymodbus.exceptions import ModbusException

    _PYMODBUS_EXCEPTIONS: tuple[type[BaseException], ...] = (ModbusException,)
except ImportError:  # pragma: no cover - only hit when pymodbus is absent
    _PYMODBUS_EXCEPTIONS = ()

TRANSPORT_TCP = "tcp"
TRANSPORT_RTU = "rtu"
VALID_TRANSPORTS = {TRANSPORT_TCP, TRANSPORT_RTU}
VALID_PARITIES = {"N", "E", "O"}
REGISTER_TYPE_HOLDING = "holding"
REGISTER_TYPE_INPUT = "input"
VALID_REGISTER_TYPES = {REGISTER_TYPE_HOLDING, REGISTER_TYPE_INPUT}

DEFAULT_REGISTER_MAP: dict[str, dict[str, Any]] = {
    "temperature": {
        "address": 0, "scale": 0.1, "encoding": "int16",
        "type": REGISTER_TYPE_HOLDING,
    },
    "humidity": {
        "address": 1, "scale": 0.1, "encoding": "uint16",
        "type": REGISTER_TYPE_HOLDING,
    },
    "device_status": {"address": 2, "online_value": 1, "type": REGISTER_TYPE_HOLDING},
}

_KNOWN_FIELD_KEYS = {"address", "scale", "encoding", "online_value", "type"}
_REQUIRED_FIELDS = {"temperature", "humidity"}
TEMPERATURE_RANGE = (-50.0, 100.0)
HUMIDITY_RANGE = (0.0, 100.0)
# 相同错误连续出现时按该周期整条记录，避免 5 秒一轮打爆日志。
REPEATED_ERROR_LOG_INTERVAL = 12
# 同类型寄存器映射允许拆成的最大连续区间数（每个区间一次读请求）。
MAX_RUNS_PER_TYPE = 16


class ModbusConfigError(ValueError):
    """Raised when endpoint config or MODBUS_REGISTER_MAP is invalid."""


class ModbusReadError(RuntimeError):
    """A poll failed at the protocol level (kept as a stable error category)."""


class ModbusShortResponseError(ModbusReadError):
    """Device answered but with fewer registers than requested."""


@dataclass(frozen=True)
class ModbusEndpoint:
    """One Modbus connection target; transport decides which fields matter."""

    transport: str = TRANSPORT_TCP
    host: str = "127.0.0.1"
    port: int = 5020
    serial_port: str = ""
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    bytesize: int = 8
    timeout: float = 5.0

    def describe(self) -> str:
        """Human-readable endpoint for *logs only* (never the status API)."""
        if self.transport == TRANSPORT_RTU:
            return f"{self.serial_port}@{self.baudrate} {self.bytesize}{self.parity}{self.stopbits}"
        return f"{self.host}:{self.port}"


def parse_modbus_endpoint(
    *,
    transport: str,
    host: str,
    port: int,
    serial_port: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    bytesize: int,
    timeout: float,
) -> ModbusEndpoint:
    """Validate and build a ModbusEndpoint; raises ModbusConfigError."""
    normalized_transport = str(transport or "").strip().lower()
    if normalized_transport not in VALID_TRANSPORTS:
        raise ModbusConfigError(
            f"MODBUS_TRANSPORT 只支持 {'/'.join(sorted(VALID_TRANSPORTS))}，"
            f"当前值: {transport!r}"
        )

    if normalized_transport == TRANSPORT_RTU:
        if not str(serial_port or "").strip():
            raise ModbusConfigError(
                "transport=rtu 时必须设置 MODBUS_SERIAL_PORT"
                "（Windows 例如 COM3，Linux 例如 /dev/ttyUSB0）"
            )
        if not isinstance(baudrate, int) or not 1200 <= baudrate <= 115200:
            raise ModbusConfigError(
                f"MODBUS_BAUDRATE 必须在 1200~115200 之间，当前值: {baudrate!r}"
            )
        normalized_parity = str(parity or "").strip().upper()
        if normalized_parity not in VALID_PARITIES:
            raise ModbusConfigError(
                f"MODBUS_PARITY 只支持 {'/'.join(sorted(VALID_PARITIES))}，"
                f"当前值: {parity!r}"
            )
        if stopbits not in (1, 2):
            raise ModbusConfigError(
                f"MODBUS_STOPBITS 只能是 1 或 2，当前值: {stopbits!r}"
            )
        if bytesize not in (7, 8):
            raise ModbusConfigError(
                f"MODBUS_BYTESIZE 只能是 7 或 8，当前值: {bytesize!r}"
            )
    else:
        if not str(host or "").strip():
            raise ModbusConfigError("transport=tcp 时 MODBUS_HOST 不能为空")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ModbusConfigError(
                f"MODBUS_PORT 必须在 1~65535 之间，当前值: {port!r}"
            )

    if not isinstance(timeout, (int, float)) or float(timeout) < 1.0:
        raise ModbusConfigError(f"MODBUS_TIMEOUT_SECONDS 必须 >= 1，当前值: {timeout!r}")

    return ModbusEndpoint(
        transport=normalized_transport,
        host=str(host or "").strip() or "127.0.0.1",
        port=int(port),
        serial_port=str(serial_port or "").strip(),
        baudrate=int(baudrate),
        parity=str(parity or "").strip().upper(),
        stopbits=int(stopbits),
        bytesize=int(bytesize),
        timeout=float(timeout),
    )


def build_modbus_client(endpoint: ModbusEndpoint):
    """Create the pymodbus client for the endpoint's transport.

    Imported lazily so a missing optional dependency (pymodbus / pyserial)
    disables the collector instead of breaking app startup. The serial
    client's default framer is already RTU.
    """
    if endpoint.transport == TRANSPORT_RTU:
        from pymodbus.client import ModbusSerialClient

        return ModbusSerialClient(
            endpoint.serial_port,
            baudrate=endpoint.baudrate,
            parity=endpoint.parity,
            stopbits=endpoint.stopbits,
            bytesize=endpoint.bytesize,
            timeout=endpoint.timeout,
            retries=1,
        )

    from pymodbus.client import ModbusTcpClient

    return ModbusTcpClient(
        endpoint.host,
        port=endpoint.port,
        timeout=endpoint.timeout,
        retries=1,
    )


def parse_register_map(raw: str | None) -> dict[str, dict[str, Any]]:
    """Parse and validate the register map; empty/None returns the default.

    Fields: ``temperature`` and ``humidity`` are required,
    ``device_status`` is optional (without it, a successful read means
    online). Measurement fields accept ``address`` / ``scale`` / ``encoding``
    / ``type`` (holding|input, default holding); ``device_status`` accepts
    ``address`` / ``online_value`` / ``type``.
    """
    if raw is None or not raw.strip():
        return copy.deepcopy(DEFAULT_REGISTER_MAP)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModbusConfigError(f"MODBUS_REGISTER_MAP 不是有效 JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ModbusConfigError("MODBUS_REGISTER_MAP 必须是非空 JSON 对象")
    if not _REQUIRED_FIELDS.issubset(parsed):
        raise ModbusConfigError(
            "MODBUS_REGISTER_MAP 至少包含字段: " + ", ".join(sorted(_REQUIRED_FIELDS))
            + "；device_status 可选"
        )
    unknown_fields = set(parsed) - set(DEFAULT_REGISTER_MAP)
    if unknown_fields:
        raise ModbusConfigError(
            "MODBUS_REGISTER_MAP 包含未知字段: " + ", ".join(sorted(unknown_fields))
        )

    validated: dict[str, dict[str, Any]] = {}
    for field_name, spec in parsed.items():
        if not isinstance(spec, dict):
            raise ModbusConfigError(f"字段 {field_name} 的映射必须是 JSON 对象")
        unknown_keys = set(spec) - _KNOWN_FIELD_KEYS
        if unknown_keys:
            raise ModbusConfigError(
                f"字段 {field_name} 包含未知配置项: {', '.join(sorted(unknown_keys))}"
            )

        address = spec.get("address")
        if not isinstance(address, int) or isinstance(address, bool) \
                or not 0 <= address <= 65534:
            raise ModbusConfigError(f"字段 {field_name} 的 address 必须是 0~65534 整数")

        register_type = spec.get("type", REGISTER_TYPE_HOLDING)
        if register_type not in VALID_REGISTER_TYPES:
            raise ModbusConfigError(
                f"字段 {field_name} 的 type 只支持 {'/'.join(sorted(VALID_REGISTER_TYPES))}"
            )

        field: dict[str, Any] = {"address": address, "type": register_type}
        if field_name == "device_status":
            online_value = spec.get("online_value", 1)
            if not isinstance(online_value, int) or isinstance(online_value, bool):
                raise ModbusConfigError(
                    f"字段 {field_name} 的 online_value 必须是整数"
                )
            field["online_value"] = online_value
        else:
            encoding = spec.get("encoding", "uint16")
            if encoding not in {"int16", "uint16"}:
                raise ModbusConfigError(
                    f"字段 {field_name} 的 encoding 只支持 int16/uint16"
                )
            scale = spec.get("scale", 1.0)
            if not isinstance(scale, (int, float)) or isinstance(scale, bool) \
                    or not math.isfinite(float(scale)):
                raise ModbusConfigError(f"字段 {field_name} 的 scale 必须是有限数字")
            field["encoding"] = encoding
            field["scale"] = float(scale)
        validated[field_name] = field

    # 同类型地址拆成连续区间分段读取：真实设备的稀疏寄存器之间
    # 可能是保留/非法区域，无条件读取 min..max 会触发 Illegal Data Address。
    # 限制区间数量，防止病态映射产生过多请求。
    for register_type in VALID_REGISTER_TYPES:
        addresses = sorted(
            f["address"] for f in validated.values()
            if f.get("type", REGISTER_TYPE_HOLDING) == register_type
        )
        if not addresses:
            continue
        runs = _contiguous_runs(addresses)
        if len(runs) > MAX_RUNS_PER_TYPE:
            raise ModbusConfigError(
                f"{register_type} 寄存器被拆成 {len(runs)} 个连续区间"
                f"（上限 {MAX_RUNS_PER_TYPE}），映射过于稀疏"
            )
    return validated


def _contiguous_runs(sorted_addresses: list[int]) -> list[tuple[int, int]]:
    """Group sorted addresses into (start, end) runs of consecutive values."""
    runs: list[tuple[int, int]] = []
    run_start = previous = sorted_addresses[0]
    for address in sorted_addresses[1:]:
        if address != previous + 1:
            runs.append((run_start, previous))
            run_start = address
        previous = address
    runs.append((run_start, previous))
    return runs


def decode_register(field: dict[str, Any], raw: int) -> float:
    """Apply int16/uint16 interpretation and scale to one raw register."""
    if field.get("encoding") == "int16" and raw >= 0x8000:
        raw -= 0x10000
    return raw * field.get("scale", 1.0)


def _in_range(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


class ModbusPoller:
    """Polls one Modbus endpoint (TCP or RTU) and feeds the unified model."""

    def __init__(
        self,
        device_id: str,
        endpoint: ModbusEndpoint,
        unit_id: int,
        poll_interval: float,
        register_map: dict[str, dict[str, Any]],
        record_sample,
    ) -> None:
        if not isinstance(unit_id, int) or isinstance(unit_id, bool) \
                or not 1 <= unit_id <= 247:
            raise ModbusConfigError(
                f"unit id 必须在 1~247 之间（Modbus 合法从站地址），当前值: {unit_id!r}"
            )
        self.device_id = device_id
        self.endpoint = endpoint
        self.unit_id = unit_id
        self.poll_interval = poll_interval
        self.register_map = register_map
        self._record_sample = record_sample
        self._client = build_modbus_client(endpoint)
        self._last_success: float | None = None
        self._last_error_summary: str | None = None
        self._consecutive_failures = 0
        self._stop = threading.Event()

    # -- runtime stats ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Health snapshot for /api/system/status; no endpoints or secrets."""
        return {
            "transport": self.endpoint.transport,
            "device_id": self.device_id,
            "last_success": (
                time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z", time.localtime(self._last_success)
                )
                if self._last_success
                else None
            ),
            "last_error_summary": self._last_error_summary,
            "consecutive_failures": self._consecutive_failures,
        }

    # -- polling ----------------------------------------------------------
    def _read_run(
        self, register_type: str, start: int, count: int
    ) -> list[int]:
        """Read one contiguous register run; validates the response length."""
        if register_type == REGISTER_TYPE_INPUT:
            response = self._client.read_input_registers(
                start, count=count, device_id=self.unit_id
            )
        else:
            response = self._client.read_holding_registers(
                start, count=count, device_id=self.unit_id
            )
        if response.isError():
            raise ModbusReadError(f"Modbus 异常响应({register_type}): {response}")
        registers = getattr(response, "registers", None)
        if not isinstance(registers, list) or len(registers) < count:
            # 短响应/畸形响应必须在这里拦下：裸索引会抛 IndexError，
            # 而 IndexError 不在 poll_once 的捕获范围内，会杀死采集线程。
            raise ModbusShortResponseError(
                f"响应寄存器数量不足({register_type} @{start}+{count}): "
                f"期望 {count}，实际 {len(registers) if registers else 0}"
            )
        return registers[:count]

    def _read_registers_by_type(self, register_type: str) -> dict[int, int]:
        """Read all fields of one register type, segmented into runs.

        Returns address -> raw value for this type ONLY. Holding and input
        are independent address spaces; keeping them in per-type dicts
        prevents same-numbered addresses from overwriting each other.
        """
        addresses = sorted(
            field["address"] for field in self.register_map.values()
            if field.get("type", REGISTER_TYPE_HOLDING) == register_type
        )
        if not addresses:
            return {}

        raw_values: dict[int, int] = {}
        for run_start, run_end in _contiguous_runs(addresses):
            count = run_end - run_start + 1
            registers = self._read_run(register_type, run_start, count)
            raw_values.update(
                (address, registers[address - run_start])
                for address in range(run_start, run_end + 1)
            )
        return raw_values

    def poll_once(self) -> dict[str, Any] | None:
        """One poll cycle; returns the unified sample or None on failure."""
        try:
            if not self._client.connected and not self._client.connect():
                # 端点详情只写入服务日志（_mark_failure 会带上下文），
                # 不进异常消息，避免串口路径/IP 经 /api/system/status 泄漏。
                raise ConnectionError("无法连接 Modbus 设备（端点详见服务日志）")

            raw_by_type: dict[str, dict[int, int]] = {
                REGISTER_TYPE_HOLDING: {},
                REGISTER_TYPE_INPUT: {},
            }
            for register_type in (REGISTER_TYPE_HOLDING, REGISTER_TYPE_INPUT):
                raw_by_type[register_type] = self._read_registers_by_type(register_type)

            def _raw(field: dict[str, Any]) -> int:
                return raw_by_type[field["type"]][field["address"]]

            temperature = decode_register(
                self.register_map["temperature"],
                _raw(self.register_map["temperature"]),
            )
            humidity = decode_register(
                self.register_map["humidity"],
                _raw(self.register_map["humidity"]),
            )

            status_field = self.register_map.get("device_status")
            if status_field is None:
                # 无状态字的简易变送器：能读到数据即视为在线。
                online = True
            else:
                online = _raw(status_field) == status_field["online_value"]

            temperature_value = (
                round(temperature, 2)
                if _in_range(temperature, TEMPERATURE_RANGE) else None
            )
            if temperature_value is None:
                logger.warning(
                    "Modbus 温度超出合理范围，按无值处理 | device=%s | value=%s",
                    self.device_id, temperature,
                )
            humidity_value = (
                round(humidity, 2) if _in_range(humidity, HUMIDITY_RANGE) else None
            )
            if humidity_value is None:
                logger.warning(
                    "Modbus 湿度超出合理范围，按无值处理 | device=%s | value=%s",
                    self.device_id, humidity,
                )

            self._mark_success()
            return {
                "device": self.device_id,
                "source": "modbus",
                "temperature": temperature_value,
                "humidity": humidity_value,
                "status": "online" if online else "offline",
            }
        except (RuntimeError, OSError, ValueError, *_PYMODBUS_EXCEPTIONS) as exc:
            # ModbusException（含 ConnectionException/超时）来自 pymodbus，
            # pyserial 的 SerialException 是 OSError 子类；任何失败只记日志
            # 并返回 None，采集线程与 Flask 主服务互不影响。
            # 主动关闭传输客户端：pymodbus 的 serial connected 只检查内部
            # socket 是否存在，USB 拔出后不关闭就永远不会重新 connect()。
            self._safe_close_client()
            self._mark_failure(exc)
            return None

    def run_forever(self) -> None:
        """Poll loop for the collector thread; never raises."""
        logger.info(
            "Modbus 采集线程启动 | device=%s | transport=%s | endpoint=%s | "
            "unit=%s | interval=%ss",
            self.device_id, self.endpoint.transport, self.endpoint.describe(),
            self.unit_id, self.poll_interval,
        )
        try:
            while not self._stop.is_set():
                sample = self.poll_once()
                if sample is not None:
                    self._record_sample(
                        device=sample["device"],
                        source=sample["source"],
                        temperature=sample["temperature"],
                        humidity=sample["humidity"],
                        status=sample["status"],
                    )
                else:
                    # 连接失败也要推进离线状态转移，但只在状态变化时落库。
                    self._record_sample(
                        device=self.device_id,
                        source="modbus",
                        temperature=None,
                        humidity=None,
                        status="offline",
                        only_on_status_change=True,
                    )
                self._stop.wait(self.poll_interval)
        finally:
            # 拔出状态下 close 本身也可能抛异常；安全退出优先。
            self._safe_close_client()
            logger.info("Modbus 采集线程退出 | device=%s", self.device_id)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        """Close the underlying transport client (idempotent)."""
        self._safe_close_client()

    # -- internals ---------------------------------------------------------
    def _safe_close_client(self) -> None:
        """Close the client, tolerating an already-dead transport (unplugged
        USB adapters can raise from close itself)."""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup, never propagate
            logger.debug("Modbus 客户端关闭时异常（忽略）", exc_info=True)

    def _mark_success(self) -> None:
        self._last_success = time.time()
        self._last_error_summary = None
        self._consecutive_failures = 0

    def _mark_failure(self, exc: Exception) -> None:
        self._consecutive_failures += 1
        # 对外（/api/system/status）只暴露稳定的异常类别：OS/pyserial 的
        # 异常消息可能携带串口路径等端点细节；完整消息只写服务日志。
        summary = type(exc).__name__
        changed = summary != self._last_error_summary
        due = self._consecutive_failures % REPEATED_ERROR_LOG_INTERVAL == 1
        if changed or due:
            logger.warning(
                "Modbus 轮询失败 | device=%s | transport=%s | endpoint=%s | "
                "%s: %s | 连续失败=%s",
                self.device_id, self.endpoint.transport, self.endpoint.describe(),
                type(exc).__name__, exc, self._consecutive_failures,
            )
        self._last_error_summary = summary
