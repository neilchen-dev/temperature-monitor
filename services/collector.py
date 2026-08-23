"""Lifecycle for background data-acquisition threads.

Contract with the rest of the app:

- ``start_collectors`` is called exactly once from ``app.run_server`` (the
  production entry point). Flask app creation stays side-effect free, so
  tests never spawn threads. A module-level guard also makes a second call
  a logged no-op, which keeps a hypothetical gunicorn/multi-worker future
  from doubling polls *within one process*; across processes the operator
  must enable MODBUS_ENABLED on exactly one worker (documented in
  .env.example).
- Any collector-specific failure (bad config, missing dependency, dead PLC)
  only disables that collector with an ERROR log — Flask keeps serving.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import config
from services import devices
from services.modbus_client import (
    ModbusConfigError,
    ModbusPoller,
    parse_modbus_endpoint,
    parse_register_map,
)


logger = logging.getLogger("temperature_monitor")

_start_lock = threading.Lock()
_started = False
_modbus_thread: threading.Thread | None = None
_modbus_poller: ModbusPoller | None = None
_modbus_error: str | None = None


def start_collectors() -> None:
    """Start enabled collectors once; safe to call again (no-op)."""
    global _started, _modbus_thread, _modbus_poller, _modbus_error

    with _start_lock:
        if _started:
            logger.info("采集线程已启动过，跳过重复启动")
            return
        _started = True

        if not config.MODBUS_ENABLED:
            logger.info("Modbus 采集未启用（MODBUS_ENABLED=false）")
            return

        try:
            endpoint = parse_modbus_endpoint(
                transport=config.MODBUS_TRANSPORT,
                host=config.MODBUS_HOST,
                port=config.MODBUS_PORT,
                serial_port=config.MODBUS_SERIAL_PORT,
                baudrate=config.MODBUS_BAUDRATE,
                parity=config.MODBUS_PARITY,
                stopbits=config.MODBUS_STOPBITS,
                bytesize=config.MODBUS_BYTESIZE,
                timeout=config.MODBUS_TIMEOUT_SECONDS,
            )
            register_map = parse_register_map(config.MODBUS_REGISTER_MAP)
            poller = ModbusPoller(
                device_id=config.MODBUS_DEVICE_ID,
                endpoint=endpoint,
                unit_id=config.MODBUS_UNIT_ID,
                poll_interval=config.MODBUS_POLL_INTERVAL_SECONDS,
                register_map=register_map,
                record_sample=devices.record_sample,
            )
        except (ModbusConfigError, ImportError, OSError, ValueError) as exc:
            _modbus_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Modbus 采集启用失败，已禁用（服务其余功能不受影响） | %s",
                _modbus_error,
            )
            return

        _modbus_poller = poller
        _modbus_thread = threading.Thread(
            target=poller.run_forever,
            name="modbus-collector",
            daemon=True,
        )
        _modbus_thread.start()
        logger.info(
            "Modbus 采集已启动 | device=%s | transport=%s | endpoint=%s | "
            "unit=%s | interval=%ss",
            config.MODBUS_DEVICE_ID,
            endpoint.transport,
            endpoint.describe(),
            config.MODBUS_UNIT_ID,
            config.MODBUS_POLL_INTERVAL_SECONDS,
        )


def get_collector_status() -> dict[str, Any]:
    """Collector health for /api/system/status.

    Deliberately excludes endpoints, ports, and any configuration detail.
    """
    if not config.MODBUS_ENABLED:
        return {"modbus": {"enabled": False}}

    thread_alive = _modbus_thread is not None and _modbus_thread.is_alive()
    status: dict[str, Any] = {"enabled": True, "running": thread_alive}
    if _modbus_error is not None:
        status["error_summary"] = _modbus_error
    if _modbus_poller is not None:
        status.update(_modbus_poller.status())
    return {"modbus": status}


def stop_collectors() -> None:
    """Stop and reset collectors (used by tests and clean shutdown)."""
    global _started, _modbus_thread, _modbus_poller, _modbus_error

    with _start_lock:
        poller, thread = _modbus_poller, _modbus_thread
        if poller is not None:
            poller.stop()
        if thread is not None and thread is not threading.current_thread():
            # 等线程退出，避免它仍持有 SQLite 连接时资源被清理。
            thread.join(timeout=5.0)
        _modbus_poller = None
        _modbus_thread = None
        _modbus_error = None
        _started = False
