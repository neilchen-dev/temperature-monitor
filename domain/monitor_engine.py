"""Pure environmental compliance evaluation."""

from __future__ import annotations

import math

from .models import (
    ApplicabilityStatus,
    ControlType,
    DataQualityStatus,
    DeviceContext,
    EnvironmentStandard,
    MonitorResult,
    MonitorSample,
    OperationState,
    OperationStatus,
    OverallStatus,
    TemperatureStatus,
)


def _numeric(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _evaluate_dimension(
    *,
    value: object,
    lower: float | None,
    upper: float | None,
    name: str,
) -> tuple[TemperatureStatus, tuple[str, ...]]:
    """Evaluate one dimension using inclusive boundaries."""
    if lower is None and upper is None:
        # No limit means this dimension is not applicable for this standard.
        return TemperatureStatus.NORMAL, ()
    if not _numeric(value):
        return TemperatureStatus.UNKNOWN, (f"{name}_missing_or_invalid",)

    numeric_value = float(value)
    if lower is not None and numeric_value < lower:
        return TemperatureStatus.LOW, (f"{name}_below_lower_limit",)
    if upper is not None and numeric_value > upper:
        return TemperatureStatus.HIGH, (f"{name}_above_upper_limit",)
    return TemperatureStatus.NORMAL, ()


def _quality_from_sample(
    *,
    sample: MonitorSample,
    standard: EnvironmentStandard | None,
) -> DataQualityStatus:
    explicit_quality = sample.data_quality
    if explicit_quality is not None:
        try:
            return DataQualityStatus(explicit_quality)
        except ValueError:
            normalized = str(explicit_quality).strip().lower()
            if normalized in {"offline", "离线"}:
                return DataQualityStatus.OFFLINE
            if normalized in {"missing", "数据缺失"}:
                return DataQualityStatus.MISSING
            if normalized in {"error", "数据异常"}:
                return DataQualityStatus.ERROR
            return DataQualityStatus.ERROR

    online_status = str(sample.online_status or "").strip().lower()
    if online_status in {"offline", "离线", "off-line"}:
        return DataQualityStatus.OFFLINE
    if online_status in {"error", "数据异常", "invalid"}:
        return DataQualityStatus.ERROR
    if online_status in {"missing", "数据缺失"}:
        return DataQualityStatus.MISSING
    if online_status and online_status not in {"online", "在线", "正常"}:
        return DataQualityStatus.ERROR

    if standard is None:
        return DataQualityStatus.GOOD

    constrained_values = (
        (sample.temperature, standard.temperature_min, standard.temperature_max),
        (sample.humidity, standard.humidity_min, standard.humidity_max),
    )
    for value, lower, upper in constrained_values:
        if lower is None and upper is None:
            continue
        if value is None:
            return DataQualityStatus.MISSING
        if not _numeric(value):
            return DataQualityStatus.ERROR
    return DataQualityStatus.GOOD


def _control_type(value: ControlType | str | None) -> ControlType | None:
    if value is None:
        return None
    if isinstance(value, ControlType):
        return value
    normalized = str(value).strip()
    aliases = {
        "全天控制": ControlType.ALL_DAY,
        "作业期间控制": ControlType.OPERATION_PERIOD,
        "仅监测": ControlType.MONITOR_ONLY,
        "ALL_DAY": ControlType.ALL_DAY,
        "OPERATION_PERIOD": ControlType.OPERATION_PERIOD,
        "MONITOR_ONLY": ControlType.MONITOR_ONLY,
    }
    return aliases.get(normalized)


def _is_not_applicable(
    *,
    device: DeviceContext,
    operation_state: OperationState | None,
) -> bool:
    control_type = _control_type(device.control_type)
    operation_status = None
    if operation_state is not None:
        operation_status = OperationStatus(operation_state.state)
        if operation_status is OperationStatus.NOT_APPLICABLE:
            # An all-day control has no operation context by design, but its
            # environmental standard still applies.  N/A only suppresses
            # operation-gated monitoring.
            return control_type is not ControlType.ALL_DAY
    if control_type is ControlType.MONITOR_ONLY:
        return True
    return (
        control_type is ControlType.OPERATION_PERIOD
        and (operation_status is None or operation_status is not OperationStatus.OPERATING)
    )


def evaluate_monitor_state(
    *,
    device: DeviceContext,
    sample: MonitorSample,
    standard: EnvironmentStandard | None,
    operation_state: OperationState | None = None,
) -> MonitorResult:
    """Return a deterministic compliance result for one sample.

    This function intentionally does not read state, inspect a database, sleep,
    send notifications, or write Feishu.  A known violation takes precedence
    over an unknown unrelated dimension so a real breach is not masked by a
    missing measurement; otherwise any unknown dimension produces UNKNOWN.
    """
    if sample.device_id != device.device_id:
        raise ValueError("sample.device_id must match device.device_id")

    quality = _quality_from_sample(sample=sample, standard=standard)
    if standard is None:
        temperature_status = TemperatureStatus.UNKNOWN
        humidity_status = TemperatureStatus.UNKNOWN
        temperature_reasons = ("no_applicable_standard",)
        humidity_reasons: tuple[str, ...] = ()
    else:
        temperature_status, temperature_reasons = _evaluate_dimension(
            value=sample.temperature,
            lower=standard.temperature_min,
            upper=standard.temperature_max,
            name="temperature",
        )
        humidity_status, humidity_reasons = _evaluate_dimension(
            value=sample.humidity,
            lower=standard.humidity_min,
            upper=standard.humidity_max,
            name="humidity",
        )
    reasons = temperature_reasons + humidity_reasons

    statuses = (temperature_status, humidity_status)
    applicability = (
        ApplicabilityStatus.NO_STANDARD
        if standard is None
        else (
            ApplicabilityStatus.NOT_APPLICABLE
            if _is_not_applicable(device=device, operation_state=operation_state)
            else ApplicabilityStatus.APPLICABLE
        )
    )
    if quality is DataQualityStatus.OFFLINE:
        reasons = ("device_offline",) + reasons
    elif quality is DataQualityStatus.MISSING:
        reasons = ("sample_data_missing",) + reasons
    elif quality is DataQualityStatus.ERROR:
        reasons = ("sample_data_error",) + reasons

    if applicability is not ApplicabilityStatus.APPLICABLE:
        overall_status = OverallStatus.UNKNOWN
    elif quality is not DataQualityStatus.GOOD:
        overall_status = OverallStatus.UNKNOWN
    elif any(status in {TemperatureStatus.LOW, TemperatureStatus.HIGH} for status in statuses):
        overall_status = OverallStatus.VIOLATION
    elif any(status is TemperatureStatus.UNKNOWN for status in statuses):
        overall_status = OverallStatus.UNKNOWN
    else:
        overall_status = OverallStatus.NORMAL

    return MonitorResult(
        device_id=sample.device_id,
        sample_time=sample.sample_time,
        temperature=sample.temperature,
        humidity=sample.humidity,
        temperature_status=temperature_status,
        humidity_status=humidity_status,
        overall_status=overall_status,
        standard_id=standard.standard_id if standard is not None else None,
        standard_revision=standard.revision if standard is not None else None,
        reasons=reasons,
        applicability=applicability,
        data_quality=quality,
    )


class MonitorEngine:
    """Stable object-style entry point over the pure evaluator function."""

    @staticmethod
    def evaluate(
        *,
        device: DeviceContext,
        sample: MonitorSample,
        standard: EnvironmentStandard | None,
        operation_state: OperationState | None = None,
    ) -> MonitorResult:
        return evaluate_monitor_state(
            device=device,
            sample=sample,
            standard=standard,
            operation_state=operation_state,
        )
