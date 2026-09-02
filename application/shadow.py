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
    resolved_control_type: str | None = None
    control_type_source: str | None = None
    control_type_consistency: str | None = None
    # The immediately preceding Python projection proves that a differing
    # Feishu value is stale, rather than merely different under a business rule.
    previous_state: Mapping[str, Any] | None = None


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
    # 已写恢复时间但仍未人工关闭的飞书事件数（“待人工闭环”）。
    # 它们不是当前活动报警，而是报警生命周期的收尾尾巴。
    pending_closure_count: int | None = None


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
    resolved_control_type: str | None = None,
    control_type_source: str | None = None,
    control_type_consistency: str | None = None,
    previous_state: Mapping[str, Any] | None = None,
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
        resolved_control_type=resolved_control_type,
        control_type_source=control_type_source,
        control_type_consistency=control_type_consistency,
        previous_state=previous_state,
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
        and expected.previous_state is None
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
        details["feishu_timestamp_skew_seconds"] = abs(latency)
        details["allowed_feishu_delay_seconds"] = max_feishu_delay.total_seconds()
        previous_state_proves_delay = (
            expected.previous_state is not None
            and abs(latency) <= max_feishu_delay.total_seconds()
            and _observed_matches_previous_expected(
                previous_state=expected.previous_state,
                observed=observed,
                differences=differences,
                details=details,
            )
        )
        legacy_timestamp_delay = (
            expected.previous_state is None
            and 0 <= latency <= max_feishu_delay.total_seconds()
        )
        if previous_state_proves_delay or legacy_timestamp_delay:
            details["feishu_delay_evidence"] = (
                "observed_matches_previous_expected"
                if previous_state_proves_delay
                else "legacy_observed_timestamp_precedes_expected"
            )
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
    pending_count = observed.pending_closure_count
    if observed_count is not None and observed_count > 1:
        details["active_event_count"] = {
            "expected": expected.active_event_count,
            "observed": observed_count,
        }
        return "EVENT_DUPLICATED"
    if expected.event_exists:
        has_observed_alarm = (observed_count is not None and observed_count > 0) or (
            observed_count is None and observed.event_exists
        )
        if not has_observed_alarm:
            # 飞书工作流在 START_RECOVERY 时就会写“恢复时间”，但事件保持
            # 打开等人工闭环。因此 RECOVERY 投影下“只有已恢复的打开事件”
            # 属于设计内的中间态，不算 EVENT_MISSING。
            recovered_tail_satisfies = (
                expected.alarm_state == AlarmLifecycleState.RECOVERY.value
                and pending_count is not None
                and pending_count > 0
            )
            if not recovered_tail_satisfies:
                details["event_exists"] = {"expected": True, "observed": False}
                if observed_count is not None:
                    details["active_event_count"] = {
                        "expected": ">=1",
                        "observed": observed_count,
                    }
                return "EVENT_MISSING"
        return None
    has_observed_alarm = (observed_count is not None and observed_count > 0) or (
        observed_count is None and observed.event_exists
    )
    if not has_observed_alarm:
        return None
    if observed_count == 0 and pending_count is not None and pending_count > 0:
        # CLOSE_ALARM_EVENT 有意不关闭飞书事件（人工闭环边界）。Python 已
        # 回到 NORMAL 时，只剩余“已写恢复时间、等人工关闭”的事件是设计内
        # 的终态，不是重复报警。
        details["pending_closure_count"] = {"expected": 0, "observed": pending_count}
        return None
    details["event_exists"] = {"expected": False, "observed": True}
    return "EVENT_DUPLICATED"


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
            differences.append("OVERALL_STATUS_MISMATCH")
            details["overall_status"] = {
                "expected": expected.overall_status,
                "observed": observed.overall_status,
            }
    monitor_result_difference_types = {
        "applicability": "APPLICABILITY_MISMATCH",
        "data_quality": "DATA_QUALITY_MISMATCH",
        "temperature_status": "TEMPERATURE_STATUS_MISMATCH",
        "humidity_status": "HUMIDITY_STATUS_MISMATCH",
    }
    for field_name, difference_type in monitor_result_difference_types.items():
        expected_value = getattr(expected, field_name)
        observed_value = getattr(observed, field_name)
        if expected_value is not None and observed_value is not None:
            if expected_value != observed_value:
                differences.append(difference_type)
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


def _observed_matches_previous_expected(
    *,
    previous_state: Mapping[str, Any],
    observed: ObservedAutomationState,
    differences: list[str],
    details: Mapping[str, Any],
) -> bool:
    """Return whether every mismatch is the immediately preceding Python value.

    Record-level Feishu timestamps can be later than ``expected_at`` because the
    comparison reads Feishu after Python creates its projection.  Timestamp
    proximity alone therefore cannot prove delay.  Requiring the observed value
    to equal the preceding Python projection prevents a persistent business-rule
    difference from receiving a fresh delay window on every sample.
    """
    fields_by_difference = {
        "ALARM_STATE_MISMATCH": ("alarm_state",),
        "OVERALL_STATUS_MISMATCH": ("overall_status",),
        "APPLICABILITY_MISMATCH": ("applicability",),
        "DATA_QUALITY_MISMATCH": ("data_quality",),
        "TEMPERATURE_STATUS_MISMATCH": ("temperature_status",),
        "HUMIDITY_STATUS_MISMATCH": ("humidity_status",),
    }
    compared_any = False
    for difference in differences:
        if difference == "STANDARD_MISMATCH":
            fields = ("standard_id", "standard_revision")
        elif difference == "OPERATION_STATE_MISMATCH":
            fields = tuple(
                field_name
                for field_name in ("operation_state", "operation_type")
                if field_name in details
            )
        elif difference in {"EVENT_MISSING", "EVENT_DUPLICATED"}:
            fields = ("event_exists",)
        else:
            fields = fields_by_difference.get(difference, ())
        if not fields:
            return False
        for field_name in fields:
            if field_name not in previous_state:
                return False
            if previous_state[field_name] != getattr(observed, field_name):
                return False
            compared_any = True
    return compared_any


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

    def record_failure(
        self,
        *,
        device_id: str,
        sample_time: datetime,
        expected: Mapping[str, Any],
        error: str,
        created_at: datetime,
    ) -> None:
        """Persist a failed observation attempt so it is visible in automation_runs."""
        if self.recorder is None:
            return
        self.recorder.record_comparison(
            device_id=device_id,
            sample_time=sample_time,
            expected=dict(expected),
            observed={"error": error},
            diff=AutomationDiff(
                matched=False,
                difference_type=("OBSERVATION_ERROR",),
                details={"error": error},
            ),
            created_at=created_at,
        )


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
        "resolved_control_type",
        "control_type_source",
        "control_type_consistency",
        "pending_closure_count",
    ):
        value = getattr(state, field_name, None)
        if value is not None:
            result[field_name] = value
    if state.active_event_ids:
        result["active_event_ids"] = list(state.active_event_ids)
    timestamp = getattr(state, "expected_at", None) or getattr(state, "observed_at", None)
    if timestamp is not None:
        result["state_at"] = timestamp.isoformat()
    return result
