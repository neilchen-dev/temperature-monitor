"""Canonical expected/observed state comparison for Shadow mode."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol

from domain.models import AlarmLifecycleState, OperationState


@dataclass(frozen=True)
class ExpectedAutomationState:
    device_id: str
    alarm_state: str
    operation_state: str
    event_exists: bool
    overall_status: str | None = None
    standard_id: str | None = None
    standard_revision: str | None = None
    active_event_count: int | None = None
    expected_at: datetime | None = None
    applicability: str | None = None
    data_quality: str | None = None
    temperature_status: str | None = None
    humidity_status: str | None = None
    active_event_ids: tuple[str, ...] = ()
    operation_type: str | None = None


@dataclass(frozen=True)
class ObservedAutomationState:
    device_id: str
    alarm_state: str
    operation_state: str
    event_exists: bool
    overall_status: str | None = None
    standard_id: str | None = None
    standard_revision: str | None = None
    active_event_count: int | None = None
    observed_at: datetime | None = None
    applicability: str | None = None
    data_quality: str | None = None
    temperature_status: str | None = None
    humidity_status: str | None = None
    active_event_ids: tuple[str, ...] = ()
    operation_type: str | None = None


@dataclass(frozen=True)
class AutomationDiff:
    matched: bool
    difference_type: tuple[str, ...]
    details: Mapping[str, Any]


class ObservationAdapter(Protocol):
    def observe(self, device_id: str) -> ObservedAutomationState:
        """Return normalized state; external field names stay in the adapter."""


class ShadowComparisonRecorder(Protocol):
    def record_comparison(
        self,
        *,
        device_id: str,
        sample_time: datetime,
        expected: Mapping[str, Any],
        observed: Mapping[str, Any],
        diff: AutomationDiff,
        created_at: datetime,
    ) -> str:
        """Persist one expected/observed comparison."""


def expected_state_from(
    *,
    device_id: str,
    alarm_state: AlarmLifecycleState | str,
    operation_state: OperationState,
    overall_status: str | None = None,
    standard_id: str | None = None,
    standard_revision: str | None = None,
    active_event_count: int | None = None,
    expected_at: datetime | None = None,
    applicability: str | None = None,
    data_quality: str | None = None,
    temperature_status: str | None = None,
    humidity_status: str | None = None,
    active_event_ids: tuple[str, ...] = (),
    operation_type: str | None = None,
) -> ExpectedAutomationState:
    normalized_alarm_state = AlarmLifecycleState(alarm_state).value
    normalized_operation_state = (
        operation_state.state.value
        if hasattr(operation_state.state, "value")
        else str(operation_state.state)
    )
    return ExpectedAutomationState(
        device_id=device_id,
        alarm_state=normalized_alarm_state,
        operation_state=normalized_operation_state,
        event_exists=normalized_alarm_state
        in {AlarmLifecycleState.ALARM.value, AlarmLifecycleState.RECOVERY.value},
        overall_status=overall_status,
        standard_id=standard_id,
        standard_revision=standard_revision,
        active_event_count=active_event_count,
        expected_at=expected_at,
        applicability=applicability,
        data_quality=data_quality,
        temperature_status=temperature_status,
        humidity_status=humidity_status,
        active_event_ids=active_event_ids,
        operation_type=operation_type,
    )


def compare_states(
    expected: ExpectedAutomationState,
    observed: ObservedAutomationState,
    *,
    expected_at: datetime | None = None,
    max_feishu_delay: timedelta = timedelta(seconds=60),
) -> AutomationDiff:
    if expected.device_id != observed.device_id:
        raise ValueError("expected and observed device IDs must match")
    details: dict[str, Any] = {}
    legacy_shape = (
        expected.expected_at is None
        and observed.observed_at is None
        and expected.overall_status is None
        and observed.overall_status is None
        and expected.standard_id is None
        and observed.standard_id is None
        and expected.standard_revision is None
        and observed.standard_revision is None
        and expected.active_event_count is None
        and observed.active_event_count is None
        and expected.applicability is None
        and observed.applicability is None
        and expected.data_quality is None
        and observed.data_quality is None
        and expected.temperature_status is None
        and observed.temperature_status is None
        and expected.humidity_status is None
        and observed.humidity_status is None
        and not expected.active_event_ids
        and not observed.active_event_ids
        and expected.operation_type is None
        and observed.operation_type is None
        and expected_at is None
    )
    if legacy_shape:
        differences: list[str] = []
        for field_name in ("alarm_state", "operation_state", "event_exists"):
            expected_value = getattr(expected, field_name)
            observed_value = getattr(observed, field_name)
            if expected_value != observed_value:
                differences.append(field_name.upper())
                details[field_name] = {
                    "expected": expected_value,
                    "observed": observed_value,
                }
        return AutomationDiff(
            matched=not differences,
            difference_type=tuple(differences),
            details=details,
        )

    event_difference = _event_difference(expected, observed, details)
    if event_difference == "EVENT_DUPLICATED":
        return AutomationDiff(False, (event_difference,), details)

    differences = _canonical_differences(expected, observed, details)
    if event_difference is not None:
        differences.insert(0, event_difference)
    if not differences:
        return AutomationDiff(True, (), details)

    actual_expected_at = expected.expected_at or expected_at
    latency = _latency_seconds(actual_expected_at, observed.observed_at)
    if latency is not None:
        details["feishu_latency_seconds"] = latency
        details["allowed_feishu_delay_seconds"] = max_feishu_delay.total_seconds()
        if 0 <= latency <= max_feishu_delay.total_seconds():
            return AutomationDiff(False, ("FEISHU_DELAY",), details)

    if event_difference is not None:
        return AutomationDiff(False, (event_difference,), details)
    return AutomationDiff(
        matched=False,
        difference_type=tuple(differences),
        details=details,
    )


def _event_difference(
    expected: ExpectedAutomationState,
    observed: ObservedAutomationState,
    details: dict[str, Any],
) -> str | None:
    observed_count = observed.active_event_count
    if observed_count is not None and observed_count > 1:
        details["active_event_count"] = {
            "expected": expected.active_event_count,
            "observed": observed_count,
        }
        return "EVENT_DUPLICATED"
    if expected.event_exists and (
        observed_count == 0 or (observed_count is None and not observed.event_exists)
    ):
        details["event_exists"] = {"expected": True, "observed": False}
        if observed_count is not None:
            details["active_event_count"] = {"expected": ">=1", "observed": observed_count}
        return "EVENT_MISSING"
    if not expected.event_exists and (
        (observed_count is not None and observed_count > 0) or observed.event_exists
    ):
        details["event_exists"] = {"expected": False, "observed": True}
        return "EVENT_DUPLICATED"
    return None


def _canonical_differences(
    expected: ExpectedAutomationState,
    observed: ObservedAutomationState,
    details: dict[str, Any],
) -> list[str]:
    differences: list[str] = []
    # The current Feishu device table exposes the resolved limits and formula
    # results, but has no standard-id/revision columns.  Do not turn that
    # known schema limitation into a false mismatch.  If a deployment opts in
    # to those observation fields, the comparison remains strict.
    observed_standard_is_available = (
        observed.standard_id is not None or observed.standard_revision is not None
    )
    if observed_standard_is_available and (
        expected.standard_id != observed.standard_id
        or expected.standard_revision != observed.standard_revision
    ):
        differences.append("STANDARD_MISMATCH")
        details["standard"] = {
            "expected": [expected.standard_id, expected.standard_revision],
            "observed": [observed.standard_id, observed.standard_revision],
        }

    if expected.operation_state != observed.operation_state:
        differences.append("OPERATION_STATE_MISMATCH")
        details["operation_state"] = {
            "expected": expected.operation_state,
            "observed": observed.operation_state,
        }
    if expected.operation_type is not None and observed.operation_type is not None:
        if expected.operation_type != observed.operation_type:
            differences.append("OPERATION_STATE_MISMATCH")
            details["operation_type"] = {
                "expected": expected.operation_type,
                "observed": observed.operation_type,
            }
    if expected.alarm_state != observed.alarm_state:
        differences.append("ALARM_STATE_MISMATCH")
        details["alarm_state"] = {
            "expected": expected.alarm_state,
            "observed": observed.alarm_state,
        }
    if expected.overall_status is not None and observed.overall_status is not None:
        if expected.overall_status != observed.overall_status:
            differences.append("ALARM_STATE_MISMATCH")
            details["overall_status"] = {
                "expected": expected.overall_status,
                "observed": observed.overall_status,
            }
    for field_name in (
        "applicability",
        "data_quality",
        "temperature_status",
        "humidity_status",
    ):
        expected_value = getattr(expected, field_name)
        observed_value = getattr(observed, field_name)
        if expected_value is not None and observed_value is not None:
            if expected_value != observed_value:
                differences.append("ALARM_STATE_MISMATCH")
                details[field_name] = {
                    "expected": expected_value,
                    "observed": observed_value,
                }
    return list(dict.fromkeys(differences))


def _latency_seconds(
    expected_at: datetime | None,
    observed_at: datetime | None,
) -> float | None:
    if expected_at is None or observed_at is None:
        return None
    return (expected_at - observed_at).total_seconds()


class ShadowComparisonService:
    """Compare normalized states and optionally persist the comparison."""

    def __init__(
        self,
        *,
        observation_adapter: ObservationAdapter,
        recorder: ShadowComparisonRecorder | None = None,
        max_feishu_delay: timedelta = timedelta(seconds=60),
    ) -> None:
        self.observation_adapter = observation_adapter
        self.recorder = recorder
        self.max_feishu_delay = max_feishu_delay

    def compare(
        self,
        *,
        expected: ExpectedAutomationState,
        sample_time: datetime,
        created_at: datetime,
    ) -> AutomationDiff:
        observed = self.observation_adapter.observe(expected.device_id)
        diff = compare_states(
            expected,
            observed,
            expected_at=sample_time,
            max_feishu_delay=self.max_feishu_delay,
        )
        if self.recorder is not None:
            self.recorder.record_comparison(
                device_id=expected.device_id,
                sample_time=sample_time,
                expected=_state_dict(expected),
                observed=_state_dict(observed),
                diff=diff,
                created_at=created_at,
            )
        return diff


def _state_dict(state: ExpectedAutomationState | ObservedAutomationState) -> dict[str, Any]:
    result: dict[str, Any] = {
        "device_id": state.device_id,
        "alarm_state": state.alarm_state,
        "operation_state": state.operation_state,
        "event_exists": state.event_exists,
    }
    for field_name in (
        "overall_status",
        "standard_id",
        "standard_revision",
        "active_event_count",
        "applicability",
        "data_quality",
        "temperature_status",
        "humidity_status",
        "operation_type",
    ):
        value = getattr(state, field_name)
        if value is not None:
            result[field_name] = value
    if state.active_event_ids:
        result["active_event_ids"] = list(state.active_event_ids)
    timestamp = getattr(state, "expected_at", None) or getattr(state, "observed_at", None)
    if timestamp is not None:
        result["state_at"] = timestamp.isoformat()
    return result
