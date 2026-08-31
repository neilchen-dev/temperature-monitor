"""Application-side ordering gate for read-only Feishu operation observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.operation import OperationAction, OperationObservation, is_newer_operation


__all__ = [
    "OperationAction",
    "OperationObservation",
    "OperationApplyResult",
    "OperationObservationService",
    "OperationObservationStore",
    "is_newer_operation",
]


class OperationObservationStore(Protocol):
    def get_current(self, device_id: str) -> OperationObservation | None:
        """Return the last accepted observation for a device."""

    def save_current(self, observation: OperationObservation) -> None:
        """Persist the accepted current observation."""

    def record_stale(self, observation: OperationObservation) -> None:
        """Audit an observation that must not overwrite current state."""


@dataclass(frozen=True)
class OperationApplyResult:
    observation: OperationObservation
    accepted: bool
    reason: str




class OperationObservationService:
    """Apply only newer operation registrations to application state."""

    def __init__(self, *, store: OperationObservationStore) -> None:
        self.store = store

    def apply(self, observation: OperationObservation) -> OperationApplyResult:
        current = self.store.get_current(observation.device_id)
        if not is_newer_operation(observation, current):
            self.store.record_stale(observation)
            return OperationApplyResult(
                observation=observation,
                accepted=False,
                reason="stale_or_duplicate_source_record",
            )
        self.store.save_current(observation)
        return OperationApplyResult(
            observation=observation,
            accepted=True,
            reason="accepted_newer_source_record",
        )
