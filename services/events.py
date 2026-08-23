"""Device state-transition events.

Pure transition logic lives here (``evaluate_transitions``); persistence goes
through ``services.db.save_device_event``. Events are appended *only* when a
state actually changes — a steady poll stream never produces rows.

States:
- status: ``online`` / ``offline`` (events store them uppercase)
- temperature band: ``NORMAL`` / ``TEMPERATURE_HIGH`` compared against
  ``config.EVENT_TEMPERATURE_HIGH_C`` (disabled when the threshold is None).
"""

from __future__ import annotations

from typing import Any

import config

EVENT_STATUS_CHANGE = "status_change"
EVENT_TEMPERATURE_ALERT = "temperature_alert"


def _temperature_band(temperature: Any, threshold: float | None) -> str | None:
    """NORMAL / TEMPERATURE_HIGH, or None when it cannot be evaluated."""
    if threshold is None or not isinstance(temperature, (int, float)):
        return None
    return "TEMPERATURE_HIGH" if float(temperature) > threshold else "NORMAL"


def evaluate_transitions(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    temperature_high_c: float | None = None,
) -> list[dict[str, Any]]:
    """Compare two unified samples and return the transition event dicts.

    ``previous is None`` means the device has no baseline yet; the first
    sample establishes state without emitting events. ``temperature_high_c``
    overrides ``config.EVENT_TEMPERATURE_HIGH_C`` when provided (tests).
    """
    if temperature_high_c is None:
        temperature_high_c = config.EVENT_TEMPERATURE_HIGH_C

    if previous is None:
        return []

    events: list[dict[str, Any]] = []
    previous_status = str(previous.get("status") or "").lower()
    current_status = str(current.get("status") or "").lower()
    if previous_status and current_status and previous_status != current_status:
        events.append({
            "event_type": EVENT_STATUS_CHANGE,
            "old_state": previous_status.upper(),
            "new_state": current_status.upper(),
            "value": None,
            "message": f"{previous_status.upper()} -> {current_status.upper()}",
        })

    previous_band = _temperature_band(previous.get("temperature"), temperature_high_c)
    current_band = _temperature_band(current.get("temperature"), temperature_high_c)
    if (
        previous_band is not None
        and current_band is not None
        and previous_band != current_band
    ):
        current_temperature = float(current["temperature"])
        comparison = ">" if current_band == "TEMPERATURE_HIGH" else "<="
        events.append({
            "event_type": EVENT_TEMPERATURE_ALERT,
            "old_state": previous_band,
            "new_state": current_band,
            "value": current_temperature,
            "message": (
                f"{previous_band} -> {current_band}: {current_temperature:.1f}C "
                f"{comparison} threshold {float(temperature_high_c):.1f}C"
            ),
        })
    return events
