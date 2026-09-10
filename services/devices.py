"""Unified device model: one normalized sample shape for every data source.

Design notes:

- The SQLite ``device_samples`` table remains the source of truth for real
  measurements.  ``device_presence`` is the separate durable source of truth
  for availability and heartbeat freshness. Reads (/api/*) query the database
  directly, so state survives restarts and there is no process-local cache to
  invalidate.
- ``record_sample`` is an isolation boundary like ``services.db``: any
  failure (invalid input, sqlite error) is logged and swallowed so callers
  on the request path (HA webhook) are never affected.
- Sources currently: ``home_assistant`` (measurement webhook and heartbeat),
  ``modbus`` (poller). OPC UA and friends only need to call ``record_sample``
  with a new source.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
import threading
from collections.abc import Callable
from typing import Any

from domain.models import DataQualityStatus, MonitorSample
from services import db
from services.events import evaluate_transitions

import config


logger = logging.getLogger("temperature_monitor")

SOURCE_HOME_ASSISTANT = "home_assistant"
SOURCE_MODBUS = "modbus"
KNOWN_SOURCES = {SOURCE_HOME_ASSISTANT, SOURCE_MODBUS}

# 统一设备模型运行健康（进程内计数，供 /api/system/status 暴露）。
# record_sample 保持旁路隔离（异常吞掉、旧采集链路不受影响），但断流
# 不再不可见：错误次数、最后错误、最后成功时间、/temperature 存活时间
# 都在这里累积，degraded 判定结合 db 镜像的最新落库时间。
_device_model_stats: dict[str, Any] = {
    "error_count": 0,
    "last_error": None,
    "last_error_time": None,
    "last_success_time": None,
    "last_heartbeat_success_time": None,
    "last_temperature_request_time": None,
    "last_heartbeat_request_time": None,
}
_device_model_stats_lock = threading.Lock()


def note_temperature_request(device: str) -> None:
    """Record that /temperature received a report (degraded detection input)."""
    with _device_model_stats_lock:
        _device_model_stats["last_temperature_request_time"] = time.time()


def note_heartbeat_request(device: str) -> None:
    """Record that the HA liveness endpoint received a heartbeat."""
    del device  # reserved for future per-device request counters
    with _device_model_stats_lock:
        _device_model_stats["last_heartbeat_request_time"] = time.time()


def _note_record_success() -> None:
    with _device_model_stats_lock:
        _device_model_stats["last_success_time"] = time.time()


def _note_heartbeat_success() -> None:
    with _device_model_stats_lock:
        _device_model_stats["last_heartbeat_success_time"] = time.time()


def _note_record_error(exc: BaseException) -> None:
    with _device_model_stats_lock:
        _device_model_stats["error_count"] += 1
        _device_model_stats["last_error"] = f"{type(exc).__name__}: {exc}"
        _device_model_stats["last_error_time"] = time.time()


def _reset_device_model_stats() -> None:
    """Test/diagnostic helper: clear in-memory health counters."""
    with _device_model_stats_lock:
        _device_model_stats.update(
            {
                "error_count": 0,
                "last_error": None,
                "last_error_time": None,
                "last_success_time": None,
                "last_heartbeat_success_time": None,
                "last_temperature_request_time": None,
                "last_heartbeat_request_time": None,
            }
        )


def _iso_local_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def get_device_model_health(*, now: float | None = None) -> dict[str, Any]:
    """Unified-device-model health snapshot for /api/system/status.

    Measurement freshness describes the last real value report.  Effective
    freshness additionally accepts a recent heartbeat while the source is
    available, so a stable sensor value does not become stale merely because
    HA emitted no changed event.
    """
    current = time.time() if now is None else now
    with _device_model_stats_lock:
        stats = dict(_device_model_stats)
    try:
        last_persisted_ms = db.fetch_device_summary().get("last_sample_time_ms")
    except Exception:  # noqa: BLE001 - health endpoint must never raise
        logger.exception("读取统一设备模型健康快照失败")
        last_persisted_ms = None
    stale_devices: list[str] = []
    unavailable_devices: list[str] = []
    missing_devices: list[str] = []
    device_states: list[dict[str, Any]] = []
    try:
        presence_rows = db.fetch_latest_device_presence()
        # Fallback keeps diagnostics/tests compatible with a pre-migration
        # mirror and is conservative: a legacy sample is also the baseline
        # heartbeat, so it can only be fresh for the normal window.
        if not presence_rows:
            presence_rows = [
                {
                    "device": row.get("device"),
                    "source": row.get("source"),
                    "last_measurement_at_ms": row.get("sample_time_ms"),
                    "last_heartbeat_at_ms": row.get("sample_time_ms"),
                    # Old test/diagnostic rows may omit status; their
                    # presence is inferred from the fact that a sample row
                    # exists, preserving the pre-migration stale check.
                    "availability": row.get("status") or "online",
                    "temperature": row.get("temperature"),
                    "humidity": row.get("humidity"),
                    "updated_at": row.get("sample_time_iso"),
                }
                for row in db.fetch_latest_device_states()
            ]

        # Prefer HA presence when a device has both HA and Modbus sources;
        # otherwise use the newest available source.  This prevents a stale
        # HA entity from being masked by an unrelated source's poll.
        selected: dict[str, dict[str, Any]] = {}
        for row in presence_rows:
            device = str(row.get("device") or "").strip().upper()
            if not device:
                continue
            candidate = dict(row)
            candidate["device"] = device
            current_row = selected.get(device)
            if current_row is None:
                selected[device] = candidate
                continue
            if str(candidate.get("source") or "").lower() == SOURCE_HOME_ASSISTANT:
                selected[device] = candidate

        for device in config.SHADOW_DEVICE_IDS:
            normalized_device = str(device).strip().upper()
            row = selected.get(normalized_device)
            if row is None:
                missing_devices.append(normalized_device)
                continue

            availability = normalize_status(row.get("availability")) or "unknown"
            measurement_ms = row.get("last_measurement_at_ms")
            heartbeat_ms = row.get("last_heartbeat_at_ms")

            def age_seconds(value: Any) -> float | None:
                if value is None:
                    return None
                return round(max(0.0, current - int(value) / 1000.0), 3)

            measurement_age = age_seconds(measurement_ms)
            heartbeat_age = age_seconds(heartbeat_ms)
            measurement_fresh = (
                measurement_age is not None
                and measurement_age <= config.DEVICE_MODEL_STALE_SECONDS
            )
            heartbeat_fresh = (
                heartbeat_age is not None
                and heartbeat_age <= config.DEVICE_MODEL_STALE_SECONDS
            )
            effective_fresh = (
                availability == STATUS_ONLINE
                and (measurement_fresh or heartbeat_fresh)
            )
            state = {
                "device": normalized_device,
                "source": row.get("source"),
                "temperature": row.get("temperature"),
                "humidity": row.get("humidity"),
                "availability": availability,
                "last_measurement_at": _iso_local_timestamp(
                    int(measurement_ms) / 1000.0 if measurement_ms is not None else None
                ),
                "last_heartbeat_at": _iso_local_timestamp(
                    int(heartbeat_ms) / 1000.0 if heartbeat_ms is not None else None
                ),
                "last_measurement_age_seconds": measurement_age,
                "last_heartbeat_age_seconds": heartbeat_age,
                "measurement_fresh": measurement_fresh,
                "heartbeat_fresh": heartbeat_fresh,
                "effective_fresh": effective_fresh,
            }
            device_states.append(state)
            if availability == STATUS_OFFLINE:
                unavailable_devices.append(normalized_device)
            elif not effective_fresh:
                stale_devices.append(normalized_device)
    except Exception:  # noqa: BLE001 - health endpoint must never raise
        logger.exception("读取逐台统一设备模型健康快照失败")
    degraded_reasons: list[str] = []
    request_time = stats.get("last_temperature_request_time")
    if (
        request_time is not None
        and current - request_time <= config.DEVICE_MODEL_STALE_SECONDS
    ):
        if last_persisted_ms is None:
            degraded_reasons.append(
                "temperature reports active but no unified sample persisted"
            )
        elif (
            not device_states
            and current - last_persisted_ms / 1000.0
            > config.DEVICE_MODEL_STALE_SECONDS
        ):
            degraded_reasons.append(
                "temperature reports active but unified samples are stale"
            )
    if missing_devices:
        degraded_reasons.append(
            "configured shadow devices have no unified sample: "
            + ",".join(missing_devices)
        )
    if stale_devices:
        degraded_reasons.append(
            "configured shadow devices have stale effective presence: "
            + ",".join(stale_devices)
        )
    if unavailable_devices:
        degraded_reasons.append(
            "configured shadow devices are unavailable: "
            + ",".join(unavailable_devices)
        )
    return {
        "device_sample_error_count": stats.get("error_count", 0),
        "device_sample_last_error": stats.get("last_error"),
        "device_sample_last_error_time": _iso_local_timestamp(
            stats.get("last_error_time")
        ),
        "last_successful_sample_time": _iso_local_timestamp(
            stats.get("last_success_time")
        ),
        "last_successful_heartbeat_time": _iso_local_timestamp(
            stats.get("last_heartbeat_success_time")
        ),
        "last_persisted_sample_time_ms": last_persisted_ms,
        "stale_threshold_seconds": config.DEVICE_MODEL_STALE_SECONDS,
        "device_states": sorted(device_states, key=lambda item: item["device"]),
        "stale_devices": stale_devices,
        "unavailable_devices": unavailable_devices,
        "missing_devices": missing_devices,
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
    }

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


@dataclass(frozen=True)
class SamplePersistOutcome:
    """Phase-A persistence result for one incoming sample.

    - ``sample``: normalized sample (from the new row, or the previous row
      on a dedupe hit). None when rejected or on internal failure.
    - ``persisted``: True only when a new device_samples row was committed.
    - ``duplicate``: True when an identical-content repeat within the
      dedupe window was collapsed onto the previous sample.
    - ``transitions``: state-transition events produced (duplicates and
      skips produce none).
    - ``reason``: rejection/skip cause when ``sample`` is None.
    """

    sample: MonitorSample | None
    sample_time_ms: int | None
    duplicate: bool
    persisted: bool
    transitions: list[dict[str, Any]]
    reason: str | None

    @property
    def durable(self) -> bool:
        """True when this business sample exists in the local store."""
        return self.persisted or self.duplicate


@dataclass(frozen=True)
class HeartbeatPersistOutcome:
    """Presence update result; heartbeat rows never enter measurement history."""

    sample: MonitorSample | None
    heartbeat_time_ms: int | None
    persisted: bool
    reason: str | None


def persist_heartbeat(
    device: str,
    source: str,
    availability: str,
    temperature: Any = None,
    humidity: Any = None,
) -> HeartbeatPersistOutcome:
    """Persist source liveness and build a runtime heartbeat observation.

    The server receipt time is used for freshness.  Optional values are the
    source's current values for evaluation only; they do not advance the
    measurement timestamp or create a historical measurement row.
    """
    try:
        normalized_device = str(device or "").strip().upper()
        normalized_source = str(source or "").strip().lower()
        normalized_status = normalize_status(availability)
        if not normalized_device:
            return HeartbeatPersistOutcome(None, None, False, "empty_device")
        if normalized_source not in KNOWN_SOURCES:
            return HeartbeatPersistOutcome(None, None, False, "unknown_source")
        if normalized_status is None:
            return HeartbeatPersistOutcome(None, None, False, "unknown_status")

        current_temperature = _coerce_number(temperature)
        current_humidity = _coerce_number(humidity)
        heartbeat_time_ms = int(time.time() * 1000)
        presence = db.save_device_heartbeat(
            device=normalized_device,
            source=normalized_source,
            heartbeat_at_ms=heartbeat_time_ms,
            availability=normalized_status,
            temperature=(
                current_temperature if normalized_status == STATUS_ONLINE else None
            ),
            humidity=(
                current_humidity if normalized_status == STATUS_ONLINE else None
            ),
        )
        if presence is None:
            return HeartbeatPersistOutcome(
                None, heartbeat_time_ms, False, "persistence_unavailable"
            )

        # A heartbeat with no values still updates health immediately.  The
        # runtime receives the last known value when available so a stable
        # over-limit sample can continue its pending/alarm timer.
        if normalized_status == STATUS_OFFLINE:
            effective_temperature = None
            effective_humidity = None
        else:
            effective_temperature = (
                current_temperature
                if current_temperature is not None
                else presence.get("temperature")
            )
            effective_humidity = (
                current_humidity
                if current_humidity is not None
                else presence.get("humidity")
            )
        measurement_ms = presence.get("last_measurement_at_ms")
        heartbeat_time = datetime.fromtimestamp(
            heartbeat_time_ms / 1000
        ).astimezone()
        measurement_time = (
            datetime.fromtimestamp(int(measurement_ms) / 1000).astimezone()
            if measurement_ms is not None
            else None
        )
        sample = MonitorSample(
            device_id=normalized_device,
            sample_time=heartbeat_time,
            temperature=effective_temperature,
            humidity=effective_humidity,
            online_status=normalized_status,
            data_quality=(
                DataQualityStatus.OFFLINE
                if normalized_status == STATUS_OFFLINE
                else None
            ),
            record_type="HEARTBEAT",
            measurement_time=measurement_time,
            heartbeat_time=heartbeat_time,
            availability=normalized_status,
        )
        _note_heartbeat_success()
        return HeartbeatPersistOutcome(sample, heartbeat_time_ms, True, None)
    except Exception as exc:  # noqa: BLE001 - heartbeat is an isolation boundary
        _note_record_error(exc)
        logger.exception(
            "统一设备 heartbeat 入库失败 | device=%s | source=%s",
            device,
            source,
        )
        return HeartbeatPersistOutcome(None, None, False, "exception")


def record_heartbeat(
    device: str,
    source: str,
    availability: str,
    temperature: Any = None,
    humidity: Any = None,
) -> MonitorSample | None:
    """Persist one heartbeat and dispatch it to Runtime/Shadow listeners."""
    outcome = persist_heartbeat(
        device=device,
        source=source,
        availability=availability,
        temperature=temperature,
        humidity=humidity,
    )
    if outcome.sample is not None:
        dispatch_sample(outcome.sample)
    return outcome.sample


def record_sample(
    device: str,
    source: str,
    temperature: Any,
    humidity: Any,
    status: str,
    sample_time_ms: int | None = None,
    only_on_status_change: bool = False,
) -> list[dict[str, Any]]:
    """Persist one unified sample, record transitions, and dispatch listeners.

    Compatibility wrapper for the legacy acquisition chain (Modbus poller):
    persist + notify in one call. The /temperature route uses the split
    :func:`persist_sample` / :func:`dispatch_sample` pair instead so local
    durability never depends on the Feishu projection.

    Returns the transition events produced by this sample (empty list when
    none). Never raises.

    ``only_on_status_change=True`` is for failure paths (e.g. a dead PLC):
    an ``offline`` row is inserted only when the previous state was not
    already offline, so a 5s poll against a powered-off device does not
    spam the table with identical rows.
    """
    outcome = persist_sample(
        device,
        source,
        temperature,
        humidity,
        status,
        sample_time_ms=sample_time_ms,
        only_on_status_change=only_on_status_change,
    )
    if outcome.sample is not None:
        dispatch_sample(outcome.sample)
    return list(outcome.transitions)


def dispatch_sample(sample: MonitorSample) -> None:
    """Phase B: hand a persisted sample to Runtime/Shadow listeners.

    Pure notification — no persistence. Listener failures are isolated.
    """
    _notify_sample_listeners(sample)


def sample_from_row(
    device: str, row: dict[str, Any], now_ms: int
) -> MonitorSample:
    """Build the normalized MonitorSample for a persisted/previous row."""
    status = str(row.get("status") or STATUS_ONLINE)
    sample_time = datetime.fromtimestamp(now_ms / 1000).astimezone()
    presence = db.fetch_device_presence(device, row.get("source"))
    measurement_ms = (
        presence.get("last_measurement_at_ms")
        if presence is not None
        else now_ms
    )
    heartbeat_ms = (
        presence.get("last_heartbeat_at_ms")
        if presence is not None
        else None
    )
    return MonitorSample(
        device_id=device,
        sample_time=sample_time,
        temperature=row.get("temperature"),
        humidity=row.get("humidity"),
        online_status=status,
        data_quality=(
            DataQualityStatus.OFFLINE if status == STATUS_OFFLINE else None
        ),
        record_type="MEASUREMENT",
        measurement_time=(
            datetime.fromtimestamp(int(measurement_ms) / 1000).astimezone()
            if measurement_ms is not None
            else None
        ),
        heartbeat_time=(
            datetime.fromtimestamp(int(heartbeat_ms) / 1000).astimezone()
            if heartbeat_ms is not None
            else None
        ),
        availability=(presence.get("availability") if presence else status),
    )


def persist_sample(
    device: str,
    source: str,
    temperature: Any,
    humidity: Any,
    status: str,
    sample_time_ms: int | None = None,
    only_on_status_change: bool = False,
    dedupe_window_ms: int = 0,
) -> SamplePersistOutcome:
    """Phase A: durably persist one unified sample locally. No listener dispatch.

    Never raises and never touches the network. ``dedupe_window_ms > 0``
    treats a repeat submission with *identical* content (temperature,
    humidity, status) for the same (device, source) within the window as
    the same business sample: no new row, no repeated events — the previous
    sample identity is returned with ``duplicate=True``. HA payloads carry
    no source timestamp, so content+window is the only safe request-level
    dedupe identity (a real sensor state change alters the content).
    """
    try:
        normalized_device = str(device or "").strip().upper()
        normalized_source = str(source or "").strip().lower()
        normalized_status = normalize_status(status)
        if not normalized_device:
            logger.warning("统一模型拒绝无设备名样本 | source=%s", normalized_source)
            return SamplePersistOutcome(None, None, False, False, [], "empty_device")
        if normalized_source not in KNOWN_SOURCES:
            logger.warning(
                "统一模型拒绝未知数据源 | device=%s | source=%s",
                normalized_device,
                normalized_source,
            )
            return SamplePersistOutcome(None, None, False, False, [], "unknown_source")
        if normalized_status is None:
            logger.warning(
                "统一模型拒绝未知状态 | device=%s | status=%r",
                normalized_device,
                status,
            )
            return SamplePersistOutcome(None, None, False, False, [], "unknown_status")

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
                return SamplePersistOutcome(
                    None, None, False, False, [], "status_unchanged"
                )

            if (dedupe_window_ms > 0
                    and _is_duplicate_sample(previous, current, now_ms,
                                             dedupe_window_ms)):
                logger.info(
                    "重复上报去重命中 | device=%s | source=%s | sample_time_ms=%s"
                    " | previous_sample_time_ms=%s",
                    normalized_device,
                    normalized_source,
                    now_ms,
                    previous.get("sample_time_ms"),
                )
                previous_ms = int(previous["sample_time_ms"])
                return SamplePersistOutcome(
                    sample_from_row(normalized_device, previous, previous_ms),
                    previous_ms,
                    True,
                    False,
                    [],
                    None,
                )

            persisted = db.save_device_sample(
                device=normalized_device,
                source=normalized_source,
                sample_time_ms=now_ms,
                sample_time_iso=sample_time_iso,
                temperature=current["temperature"],
                humidity=current["humidity"],
                status=normalized_status,
                heartbeat_at_ms=int(time.time() * 1000),
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

        sample = MonitorSample(
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
            record_type="MEASUREMENT",
            measurement_time=datetime.fromtimestamp(now_ms / 1000).astimezone(),
            heartbeat_time=datetime.now().astimezone(),
            availability=normalized_status,
        )
        if transitions:
            logger.info(
                "设备状态变化 | device=%s | events=%s",
                normalized_device,
                ", ".join(f"{e['old_state']}->{e['new_state']}" for e in transitions),
            )
        _note_record_success()
        return SamplePersistOutcome(
            sample, now_ms, False, persisted, transitions, None
        )
    except Exception as exc:
        # 旁路隔离不变：旧采集链路继续可用。但断流不再静默——计数与
        # 最后错误暴露在 /api/system/status 的 device_model 段。
        _note_record_error(exc)
        logger.exception("统一设备样本入库失败 | device=%s | source=%s", device, source)
        return SamplePersistOutcome(None, None, False, False, [], "exception")


def _is_duplicate_sample(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    now_ms: int,
    dedupe_window_ms: int,
) -> bool:
    """True when ``previous`` is a recent row with identical content.

    Requires a non-negative, in-window age delta so a clock skew backwards
    never silently drops a reading.
    """
    if previous is None:
        return False
    previous_ms = previous.get("sample_time_ms")
    if previous_ms is None:
        return False
    delta = now_ms - int(previous_ms)
    if delta < 0 or delta > dedupe_window_ms:
        return False
    return (
        str(previous.get("status") or "").lower() == current["status"]
        and previous.get("temperature") == current["temperature"]
        and previous.get("humidity") == current["humidity"]
    )
