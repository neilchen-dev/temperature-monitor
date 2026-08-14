from __future__ import annotations

import math
from typing import Any

import config


INVALID_VALUES = {"", "none", "null", "unknown", "unavailable", "nan"}
OFFLINE_VALUES = {"offline", "离线", "unavailable", "unknown", "0", "false"}


def parse_number(value: Any, name: str) -> float:
    if isinstance(value, str) and value.strip().lower() in INVALID_VALUES:
        raise ValueError(f"{name}无有效数值: {value!r}")

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}不是数字: {value!r}") from exc

    if not math.isfinite(number):
        raise ValueError(f"{name}不是有限数字: {value!r}")
    return number


def normalize_temperature(value: Any) -> float:
    temperature = parse_number(value, "温度")
    if config.SOURCE_TEMPERATURE_UNIT == "F":
        temperature = (temperature - 32.0) * 5.0 / 9.0
    elif config.SOURCE_TEMPERATURE_UNIT != "C":
        raise ValueError("SOURCE_TEMPERATURE_UNIT 只能是 F 或 C")

    if not -50.0 <= temperature <= 100.0:
        raise ValueError(f"摄氏温度超出有效范围: {temperature:.2f}")
    return round(temperature, 2)


def normalize_humidity(value: Any) -> float:
    humidity = parse_number(value, "湿度")
    if not 0.0 <= humidity <= 100.0:
        raise ValueError(f"湿度超出有效范围: {humidity:.2f}")
    return round(humidity, 2)


def is_offline_status(value: Any) -> bool:
    return str(value).strip().lower() in OFFLINE_VALUES
