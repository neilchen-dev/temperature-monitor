"""Unified device model: one normalized sample shape for every data source.

Design notes:

- The SQLite ``device_samples`` table — not an in-memory registry — is the
  source of truth. Reads (/api/*) query the database directly, so state
  survives restarts and there is no process-local cache to invalidate.
- ``record_sample`` is an isolation boundary like ``services.db``: any
  failure (invalid input, sqlite error) is logged and swallowed so callers
  on the request path (HA webhook) are never affected.
- Sources currently: ``home_assistant`` (webhook hook), ``modbus`` (poller).
  OPC UA and friends only need to call ``record_sample`` with a new source.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
import threading
from collections.abc import Callable
from typing import Any

from domain.models import DataQualityStatus, MonitorSample
from services import db
from services.events import evaluate_transitions


logger = logging.getLogger("temperature_monitor")

SOURCE_HOME_ASSISTANT = "home_assistant"
SOURCE_MODBUS = "modbus"
KNOWN_SOURCES = {SOURCE_HOME_ASSISTANT, SOURCE_MODBUS}

# Unified statuses stored in device_samples.status / used by event states.
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"

SampleListener = Callable[[MonitorSample], None]
_sample_listeners: list[SampleListener] = []
_sample_listener_lock = threading.RLock()


def register_sample_listener(listener: SampleListener) -> None:
    """Register a non-blocking extension hook for normalized samples.

    The existing persistence and Feishu write path remains the caller's
    responsibility.  Runtime listeners are invoked only after that legacy
    path has accepted the sample, and listener failures are isolated.
    """
    if not callable(listener):
        raise TypeError("listener must be callable")
    with _sample_listener_lock:
        if listener not in _sample_listeners:
            _sample_listeners.append(listener)


def unregister_sample_listener(listener: SampleListener) -> None:
    with _sample_listener_lock:
        try:
            _sample_listeners.remove(listener)
        except ValueError:
            pass


def _notify_sample_listeners(sample: MonitorSample) -> None:
    with _sample_listener_lock:
        listeners = tuple(_sample_listeners)
    for listener in listeners:
        try:
            listener(sample)
        except Exception:  # noqa: BLE001 - extensions must not break acquisition
            logger.exception(
                "采样扩展处理失败 | device=%s | sample_time=%s",
                sample.device_id,
                sample.sample_time.isoformat(),
            )


def normalize_status(value: Any) -> str | None:
    """Map source-specific status words to 'online'/'offline'; None if unknown."""
    text = str(value if value is not None else "").strip().lower()
    if text in {"online", "on", "1", "true", "在线", "run", "running"}:
        return STATUS_ONLINE
    if text in {"offline", "off", "0", "false", "离线", "unavailable",
                "unknown", "none", "null", "nan", "stopped", "fault"}:
        return STATUS_OFFLINE
    return None


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def record_sample(
    device: str,
    source: str,
    temperature: Any,
    humidity: Any,
    status: str,
    sample_time_ms: int | None = None,
    only_on_status_change: bool = False,
) -> list[dict[str, Any]]:
    """Persist one unified sample and record any state transitions.

    Returns the transition events produced by this sample (empty list when
    none). Never raises.

    ``only_on_status_change=True`` is for failure paths (e.g. a dead PLC):
    an ``offline`` row is inserted only when the previous state was not
    already offline, so a 5s poll against a powered-off device does not
    spam the table with identical rows.
    """
    try:
        normalized_device = str(device or "").strip().upper()
        normalized_source = str(source or "").strip().lower()
        normalized_status = normalize_status(status)
        if not normalized_device:
            logger.warning("统一模型拒绝无设备名样本 | source=%s", normalized_source)
            return []
        if normalized_source not in KNOWN_SOURCES:
            logger.warning(
                "统一模型拒绝未知数据源 | device=%s | source=%s",
                normalized_device,
                normalized_source,
            )
            return []
        if normalized_status is None:
            logger.warning(
                "统一模型拒绝未知状态 | device=%s | status=%r",
                normalized_device,
                status,
            )
            return []

        now_ms = int(sample_time_ms if sample_time_ms is not None
                     else time.time() * 1000)
        try:
            sample_time_iso = (
                datetime.fromtimestamp(now_ms / 1000)
                .astimezone()
                .isoformat(timespec="seconds")
            )
        except (OSError, OverflowError, ValueError):
            # Windows 对 1970 前后的本地时间换算会失败；时间戳不可表示时
            # 用当前时间兜底，样本本身（毫秒值）仍然保留。
            sample_time_iso = (
                datetime.now().astimezone().isoformat(timespec="seconds")
            )
        current = {
            "temperature": _coerce_number(temperature),
            "humidity": _coerce_number(humidity),
            "status": normalized_status,
        }

        # db._lock is an RLock and db helpers re-acquire it, so the
        # read-baseline -> insert -> evaluate sequence stays race-free.
        # 状态机身份是 (device, source)：不同数据源各自维护基线，
        # 同一 device_id 的 HA 与 Modbus 不会互相触发状态转移。
        with db._lock:
            previous = db.fetch_previous_device_sample(
                normalized_device, source=normalized_source
            )
            if (only_on_status_change
                    and normalized_status == STATUS_OFFLINE
                    and previous is not None
                    and str(previous.get("status") or "").lower() == STATUS_OFFLINE):
                return []

            db.save_device_sample(
                device=normalized_device,
                source=normalized_source,
                sample_time_ms=now_ms,
                sample_time_iso=sample_time_iso,
                temperature=current["temperature"],
                humidity=current["humidity"],
                status=normalized_status,
            )

            transitions = evaluate_transitions(previous, current)
            for event in transitions:
                db.save_device_event(
                    device_id=normalized_device,
                    event_type=event["event_type"],
                    old_state=event["old_state"],
                    new_state=event["new_state"],
                    value=event["value"],
                    message=event["message"],
                    source=normalized_source,
                )

        _notify_sample_listeners(
            MonitorSample(
                device_id=normalized_device,
                sample_time=datetime.fromtimestamp(now_ms / 1000).astimezone(),
                temperature=current["temperature"],
                humidity=current["humidity"],
                online_status=normalized_status,
                data_quality=(
                    DataQualityStatus.OFFLINE
                    if normalized_status == STATUS_OFFLINE
                    else None
                ),
            )
        )
        if transitions:
            logger.info(
                "设备状态变化 | device=%s | events=%s",
                normalized_device,
                ", ".join(f"{e['old_state']}->{e['new_state']}" for e in transitions),
            )
        return transitions
    except Exception:
        logger.exception("统一设备样本入库失败 | device=%s | source=%s", device, source)
        return []
