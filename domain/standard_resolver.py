"""Pure contracts and selection rules for versioned standards."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Iterable
from typing import Protocol

from .models import EnvironmentStandard


class StandardResolutionError(ValueError):
    """Base error for an invalid or incomplete standard configuration."""


class StandardNotFoundError(StandardResolutionError):
    """No enabled standard applies to the requested context and timestamp."""


class StandardConfigurationConflictError(StandardResolutionError):
    """More than one standard has the same winning precedence."""


class StandardResolver(Protocol):
    """Resolve exactly one applicable standard without evaluating a sample."""

    def resolve(
        self,
        *,
        area_id: str,
        operation_type: str | None,
        timestamp: datetime,
        device_id: str | None = None,
    ) -> EnvironmentStandard:
        """Return the standard effective for the supplied business context."""


def select_standard(
    standards: Iterable[EnvironmentStandard],
    *,
    area_id: str,
    operation_type: str | None,
    timestamp: datetime,
    device_id: str | None = None,
) -> EnvironmentStandard:
    """Select one standard using deterministic precedence rules.

    Effective intervals are half-open: ``effective_from <= timestamp <
    effective_to``.  This permits adjacent revisions without an artificial
    overlap at the boundary.
    """
    if not area_id.strip():
        raise ValueError("area_id cannot be empty")

    normalized_device_id = device_id.strip().upper() if device_id is not None else None
    candidates = [
        standard
        for standard in standards
        if standard.enabled
        and standard.area == area_id
        and (
            standard.device_id is None
            or (
                normalized_device_id is not None
                and standard.device_id.strip().upper() == normalized_device_id
            )
        )
        and standard.effective_from <= timestamp
        and (
            standard.effective_to is None
            or timestamp < standard.effective_to
        )
        and (
            standard.operation_type is None
            or standard.operation_type == operation_type
        )
    ]
    if not candidates:
        raise StandardNotFoundError(
            f"no enabled standard for area={area_id!r}, "
            f"operation_type={operation_type!r}, timestamp={timestamp.isoformat()}"
        )

    def precedence(standard: EnvironmentStandard) -> tuple[int, int, int]:
        device_exact = int(
            normalized_device_id is not None
            and standard.device_id is not None
            and standard.device_id.strip().upper() == normalized_device_id
        )
        operation_exact = int(
            operation_type is not None
            and standard.operation_type == operation_type
        )
        return device_exact, operation_exact, standard.priority

    winning_precedence = max(precedence(standard) for standard in candidates)
    winners = [
        standard
        for standard in candidates
        if precedence(standard) == winning_precedence
    ]
    if len(winners) != 1:
        identities = ", ".join(
            f"{standard.standard_id}/{standard.revision}" for standard in winners
        )
        raise StandardConfigurationConflictError(
            "multiple standards have the same precedence: " + identities
        )
    return winners[0]


class StaticStandardResolver:
    """In-memory resolver for local development and deterministic tests."""

    def __init__(self, standards: Iterable[EnvironmentStandard]) -> None:
        self._standards = tuple(standards)

    def resolve(
        self,
        *,
        area_id: str,
        operation_type: str | None,
        timestamp: datetime,
        device_id: str | None = None,
    ) -> EnvironmentStandard:
        return select_standard(
            self._standards,
            area_id=area_id,
            operation_type=operation_type,
            timestamp=timestamp,
            device_id=device_id,
        )
