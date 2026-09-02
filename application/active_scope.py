"""Shared Active Canary device-scope policy."""

from __future__ import annotations

from collections.abc import Iterable

import config


def normalize_device_id(device_id: object) -> str | None:
    """Return a canonical device id, or ``None`` when it is unusable."""
    if not isinstance(device_id, str):
        return None
    normalized = device_id.strip().upper()
    return normalized or None


def normalize_device_ids(
    device_ids: Iterable[str] | str | None,
) -> frozenset[str]:
    """Normalize a comma-separated or iterable device allowlist."""
    if device_ids is None:
        return frozenset()
    raw_device_ids = (
        device_ids.split(",") if isinstance(device_ids, str) else device_ids
    )
    return frozenset(
        normalized
        for device_id in raw_device_ids
        if (normalized := normalize_device_id(device_id)) is not None
    )


def active_scope_allows(
    device_id: object,
    *,
    active_device_ids: Iterable[str] | str | None = None,
) -> bool:
    """Return whether one device is inside the Active Canary scope.

    An empty allowlist deliberately denies every device.  Callers decide
    whether a denied action should be planned, rejected, or otherwise
    reported; this policy only answers the scope question.
    """
    normalized_device_id = normalize_device_id(device_id)
    if normalized_device_id is None:
        return False
    configured_ids = normalize_device_ids(
        config.ACTIVE_DEVICE_IDS
        if active_device_ids is None
        else active_device_ids
    )
    return bool(configured_ids) and normalized_device_id in configured_ids
