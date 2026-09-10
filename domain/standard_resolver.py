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


def logical_selector_key(standard: EnvironmentStandard) -> tuple[object, ...]:
    """Return the selector identity shared by a revision chain.

    ``standard_id`` is the stable logical-standard key.  A revision may change
    thresholds and precedence, so ``priority`` is deliberately not part of
    this key; it is evaluated after a logical chain has been collapsed to one
    effective revision.  Selector changes are only safe when their effective
    intervals do not overlap and are therefore rejected by strict snapshot
    validation when they do overlap.
    """
    return (
        standard.area,
        standard.device_id.strip().upper() if standard.device_id else None,
        standard.operation_type,
        standard.control_type.value if standard.control_type is not None else None,
    )


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

    # A source snapshot intentionally contains historical and current
    # revisions together.  Collapse each logical chain before applying normal
    # resolver precedence; otherwise two valid overlapping revisions would tie
    # and look like an ambiguous configuration.  The latest effective
    # revision is the successor at a given timestamp.  Equal starts remain an
    # error because there is no deterministic supersession order.
    by_logical_id: dict[str, list[EnvironmentStandard]] = {}
    for standard in candidates:
        by_logical_id.setdefault(standard.standard_id, []).append(standard)

    collapsed: list[EnvironmentStandard] = []
    for logical_id, revisions in by_logical_id.items():
        selector_keys = {logical_selector_key(revision) for revision in revisions}
        if len(selector_keys) != 1:
            identities = ", ".join(
                f"{revision.standard_id}/{revision.revision}"
                for revision in revisions
            )
            raise StandardConfigurationConflictError(
                "logical standard has incompatible active revision selectors: "
                + identities
            )
        latest_start = max(revision.effective_from for revision in revisions)
        latest = [
            revision
            for revision in revisions
            if revision.effective_from == latest_start
        ]
        if len(latest) != 1:
            identities = ", ".join(
                f"{revision.standard_id}/{revision.revision}"
                for revision in latest
            )
            raise StandardConfigurationConflictError(
                "logical standard revisions have the same effective_from: "
                + identities
            )
        collapsed.append(latest[0])
    candidates = collapsed

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
