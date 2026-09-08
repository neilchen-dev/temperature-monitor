"""Validated synchronization of standards into the local SQLite cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Protocol

from domain.models import ControlType, EnvironmentStandard
from repositories.standard_resolver import SQLiteStandardRepository


class StandardSource(Protocol):
    """Provide a complete, already-mapped standard snapshot."""

    def fetch_standards(self) -> tuple[EnvironmentStandard, ...]:
        """Return all rows that should be considered by the local cache."""


class StandardSyncStatus(str):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class StandardSyncReport:
    status: str
    standard_count: int
    activated: bool
    sync_id: str | None
    errors: tuple[str, ...] = ()
    changed: bool = False


def _intervals_overlap(left: EnvironmentStandard, right: EnvironmentStandard) -> bool:
    left_end = left.effective_to
    right_end = right.effective_to
    latest_start = max(left.effective_from, right.effective_from)
    if left_end is None:
        earliest_end = right_end
    elif right_end is None:
        earliest_end = left_end
    else:
        earliest_end = min(left_end, right_end)
    return earliest_end is None or latest_start < earliest_end


def validate_standard_snapshot(
    standards: tuple[EnvironmentStandard, ...],
) -> tuple[str, ...]:
    """Validate a full snapshot before any active cache row is changed."""
    errors: list[str] = []
    if not standards:
        errors.append("standard snapshot cannot be empty")

    identities: set[tuple[str, str]] = set()
    for standard in standards:
        identity = (standard.standard_id, standard.revision)
        if identity in identities:
            errors.append(
                f"duplicate standard revision: {standard.standard_id}/{standard.revision}"
            )
        identities.add(identity)
        if not isinstance(standard.priority, int) or isinstance(standard.priority, bool):
            errors.append(f"priority must be an integer: {identity[0]}/{identity[1]}")
        if not isinstance(standard.enabled, bool):
            errors.append(f"enabled must be boolean: {identity[0]}/{identity[1]}")
        if not isinstance(standard.device_id, str) or not standard.device_id.strip():
            errors.append(f"device_id is required: {identity[0]}/{identity[1]}")
        if not isinstance(standard.control_type, ControlType):
            errors.append(f"control_type must be a supported enum: {identity[0]}/{identity[1]}")
        if not isinstance(standard.revision, str) or not standard.revision.strip():
            errors.append(f"revision/version is required: {identity[0]}/{identity[1]}")
        for field_name in (
            "temperature_min",
            "temperature_max",
            "humidity_min",
            "humidity_max",
        ):
            value = getattr(standard, field_name)
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{field_name} is required and numeric: {identity[0]}/{identity[1]}")
            elif not math.isfinite(float(value)):
                errors.append(f"{field_name} must be finite: {identity[0]}/{identity[1]}")
        if (
            standard.temperature_min is not None
            and standard.temperature_max is not None
            and standard.temperature_min >= standard.temperature_max
        ):
            errors.append(f"temp_min must be less than temp_max: {identity[0]}/{identity[1]}")
        if (
            standard.humidity_min is not None
            and standard.humidity_max is not None
            and standard.humidity_min >= standard.humidity_max
        ):
            errors.append(f"humidity_min must be less than humidity_max: {identity[0]}/{identity[1]}")

    for index, left in enumerate(standards):
        if not left.enabled:
            continue
        for right in standards[index + 1 :]:
            if not right.enabled:
                continue
            same_precedence_group = (
                left.device_id == right.device_id
                and
                left.area == right.area
                and left.operation_type == right.operation_type
                and left.priority == right.priority
            )
            if same_precedence_group and _intervals_overlap(left, right):
                errors.append(
                    "overlapping standards with same area, operation_type and priority: "
                    f"{left.standard_id}/{left.revision}, "
                    f"{right.standard_id}/{right.revision}"
                )
    return tuple(errors)


class StandardSyncService:
    """Fetch, validate, and atomically activate a complete standard snapshot."""

    def __init__(
        self,
        *,
        source: StandardSource,
        repository: SQLiteStandardRepository,
        source_name: str = "feishu:standard-source",
    ) -> None:
        if not source_name.strip().lower().startswith("feishu:"):
            raise ValueError("standard sync source must be feishu:*")
        self.source = source
        self.repository = repository
        self.source_name = source_name

    def sync(self, *, now: datetime) -> StandardSyncReport:
        try:
            standards = tuple(self.source.fetch_standards())
        except Exception as exc:  # noqa: BLE001 - convert source failure to audit row
            error = f"source fetch failed: {exc}"
            sync_id = self.repository.record_sync_failure(
                source=self.source_name,
                standard_count=0,
                errors=(error,),
                started_at=now,
                finished_at=now,
            )
            return StandardSyncReport(
                status=StandardSyncStatus.FAILED,
                standard_count=0,
                activated=False,
                sync_id=sync_id,
                errors=(error,),
            )

        errors = validate_standard_snapshot(standards)
        if errors:
            sync_id = self.repository.record_sync_failure(
                source=self.source_name,
                standard_count=len(standards),
                errors=errors,
                started_at=now,
                finished_at=now,
            )
            return StandardSyncReport(
                status=StandardSyncStatus.FAILED,
                standard_count=len(standards),
                activated=False,
                sync_id=sync_id,
                errors=errors,
            )

        try:
            previous_snapshot_id = self.repository.active_snapshot_id()
            sync_id = self.repository.apply_snapshot(
                standards,
                source=self.source_name,
                synced_at=now,
            )
            changed = previous_snapshot_id != self.repository.active_snapshot_id()
        except Exception as exc:  # noqa: BLE001 - preserve old cache on failure
            error = f"cache activation failed: {exc}"
            failure_id = self.repository.record_sync_failure(
                source=self.source_name,
                standard_count=len(standards),
                errors=(error,),
                started_at=now,
                finished_at=now,
            )
            return StandardSyncReport(
                status=StandardSyncStatus.FAILED,
                standard_count=len(standards),
                activated=False,
                sync_id=failure_id,
                errors=(error,),
            )
        return StandardSyncReport(
            status=StandardSyncStatus.SUCCEEDED,
            standard_count=len(standards),
            activated=True,
            sync_id=sync_id,
            changed=changed,
        )
