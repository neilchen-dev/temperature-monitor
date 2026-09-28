"""Whitelisted runtime-configurable settings with audit trail and hot reload.

Resolution order for every supported parameter::

    SQLite ``runtime_settings`` override  ->  ``config.py`` / ``.env`` default

Scope guarantees (deliberately narrow by design):

- Only explicitly whitelisted low-risk runtime parameters are editable here.
  ``AUTOMATION_MODE``, ``FEISHU_WRITE_ENABLED``, ``ACTIVE_DEVICE_IDS``,
  ``ACTIVE_CUTOVER_ACK`` and every secret stay deployment-level (``.env`` +
  restart); first-stage web editing intentionally does not touch them.
- Formal environmental standards (limits, control types, ``standard_id`` /
  ``revision`` chain) are never part of this layer.  The Feishu validated
  standard resolver remains their single source of truth; changing margins
  or notification switches here never changes the compliance decision.
- No secret value is readable or writable through this module.

Reliability model:

- Reads use the shared SQLite mirror connection (``services.db``) only
  when that mirror is enabled and already initialized; otherwise they fall
  back to config defaults.  Any read failure is logged and also falls back,
  so the acquisition / monitoring chain is never blocked by settings
  storage (fail-open to defaults, never an exception to the sample path).
- Writes validate the complete change set first, then commit the setting
  rows plus one ``runtime_setting_audit`` row per key inside a single
  transaction.  A failed write rolls back and leaves no partial state.
- Values are read from SQLite at evaluation time; nothing is cached into a
  module-level constant, so a committed change takes effect on the next
  evaluated sample without a process restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import math
import sqlite3
from typing import Any, Mapping

import config
from repositories.sqlite import SQLITE_WRITE_LOCK, run_sqlite_write_with_retry
from services import db

logger = logging.getLogger("temperature_monitor")

_AUDIT_DEFAULT_LIMIT = 50
_AUDIT_MAX_LIMIT = 100
_REASON_MAX_LENGTH = 500
_CHANGED_BY_MAX_LENGTH = 64


class SettingsValidationError(ValueError):
    """The requested change violates the whitelist or its typed schema."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class SettingsWriteError(RuntimeError):
    """A validated change could not be persisted; nothing was written."""


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    value_type: str  # "float" | "bool"
    config_attr: str
    label: str
    unit: str | None
    minimum: float | None
    maximum: float | None
    fallback: float | bool


_DEFINITIONS: tuple[SettingDefinition, ...] = (
    SettingDefinition(
        key="temperature_prewarning_margin_c",
        value_type="float",
        config_attr="TEMPERATURE_PREWARNING_MARGIN_C",
        label="温度预警提前量",
        unit="°C",
        minimum=0.0,
        maximum=5.0,
        fallback=0.2,
    ),
    SettingDefinition(
        key="humidity_prewarning_margin_rh",
        value_type="float",
        config_attr="HUMIDITY_PREWARNING_MARGIN_RH",
        label="湿度预警提前量",
        unit="%RH",
        minimum=0.0,
        maximum=20.0,
        fallback=2.0,
    ),
    SettingDefinition(
        key="temperature_prewarning_exit_margin_c",
        value_type="float",
        config_attr="TEMPERATURE_PREWARNING_EXIT_MARGIN_C",
        label="温度预警退出裕量",
        unit="°C",
        minimum=0.0,
        maximum=10.0,
        fallback=0.3,
    ),
    SettingDefinition(
        key="humidity_prewarning_exit_margin_rh",
        value_type="float",
        config_attr="HUMIDITY_PREWARNING_EXIT_MARGIN_RH",
        label="湿度预警退出裕量",
        unit="%RH",
        minimum=0.0,
        maximum=40.0,
        fallback=3.0,
    ),
    SettingDefinition(
        key="feishu_alarm_notify_enabled",
        value_type="bool",
        config_attr="FEISHU_ALARM_NOTIFY_ENABLED",
        label="飞书报警通知",
        unit=None,
        minimum=None,
        maximum=None,
        fallback=False,
    ),
    SettingDefinition(
        key="feishu_recovery_notify_enabled",
        value_type="bool",
        config_attr="FEISHU_RECOVERY_NOTIFY_ENABLED",
        label="飞书恢复通知",
        unit=None,
        minimum=None,
        maximum=None,
        fallback=False,
    ),
    SettingDefinition(
        key="feishu_prewarning_notify_enabled",
        value_type="bool",
        config_attr="FEISHU_PREWARNING_NOTIFY_ENABLED",
        label="飞书接近限值预警",
        unit=None,
        minimum=None,
        maximum=None,
        fallback=False,
    ),
    SettingDefinition(
        key="feishu_prewarning_recovery_notify_enabled",
        value_type="bool",
        config_attr="FEISHU_PREWARNING_RECOVERY_NOTIFY_ENABLED",
        label="飞书接近限值恢复通知",
        unit=None,
        minimum=None,
        maximum=None,
        fallback=False,
    ),
)

#: The complete whitelist.  Keys outside this set are rejected on write and
#: ignored on read; there is intentionally no generic key/value escape hatch.
SETTING_DEFINITIONS: dict[str, SettingDefinition] = {
    definition.key: definition for definition in _DEFINITIONS
}


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_value(definition: SettingDefinition) -> float | bool:
    """Read the config default dynamically (never cached at import time)."""
    raw = getattr(config, definition.config_attr, None)
    if raw is None:
        return definition.fallback
    if definition.value_type == "float":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return float(definition.fallback)
        return value if math.isfinite(value) else float(definition.fallback)
    return bool(raw)


def _format_value(definition: SettingDefinition, value: float | bool) -> str:
    if definition.value_type == "bool":
        return "true" if value else "false"
    return repr(float(value))


def _read_overrides() -> dict[str, tuple[str, str]]:
    """Load whitelisted overrides; any failure degrades to no overrides."""
    connection = db.peek_connection()
    if connection is None:
        return {}
    try:
        with SQLITE_WRITE_LOCK:
            rows = connection.execute(
                "SELECT key, value, value_type FROM runtime_settings"
            ).fetchall()
    except sqlite3.Error:
        logger.exception("读取 runtime_settings 失败，回退 config.py 默认值")
        return {}
    overrides: dict[str, tuple[str, str]] = {}
    for row in rows:
        key = str(row["key"])
        if key in SETTING_DEFINITIONS:
            overrides[key] = (str(row["value"]), str(row["value_type"]))
    return overrides


def _parse_stored(definition: SettingDefinition, stored: tuple[str, str]) -> float | bool | None:
    """Parse one stored override; None means corrupt -> fall back to default."""
    text, value_type = stored
    if value_type != definition.value_type:
        logger.warning(
            "runtime_settings 存储类型不匹配，忽略 override | key=%s | expected=%s | stored=%s",
            definition.key,
            definition.value_type,
            value_type,
        )
        return None
    if definition.value_type == "bool":
        normalized = text.strip().lower()
        if normalized in {"true", "false"}:
            return normalized == "true"
    else:
        try:
            value = float(text)
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value):
            minimum = definition.minimum
            maximum = definition.maximum
            if (minimum is None or value >= minimum) and (
                maximum is None or value <= maximum
            ):
                return value
    logger.warning(
        "runtime_settings 存储值非法，回退默认值 | key=%s | value=%r",
        definition.key,
        text,
    )
    return None


def _resolve(
    definition: SettingDefinition, overrides: Mapping[str, tuple[str, str]]
) -> tuple[float | bool, str]:
    stored = overrides.get(definition.key)
    if stored is not None:
        parsed = _parse_stored(definition, stored)
        if parsed is not None:
            return parsed, "override"
    return _default_value(definition), "default"


def get_setting(key: str) -> float | bool:
    """Return the current effective value for one whitelisted key."""
    definition = SETTING_DEFINITIONS.get(key)
    if definition is None:
        raise KeyError(f"未知运行配置项: {key}")
    value, _source = _resolve(definition, _read_overrides())
    return value


def prewarning_margins() -> dict[str, float]:
    """All four near-limit margins in one read (one SQLite query).

    The exit margins are normalized against the matching entry margin —
    the same invariant config.py applies at load time — so even a hand-edited
    database row can never shrink the hysteresis band below the entry band.
    """
    return _prewarning_margins_from(_read_overrides())


def _prewarning_margins_from(
    overrides: Mapping[str, tuple[str, str]]
) -> dict[str, float]:
    temperature, _ = _resolve(
        SETTING_DEFINITIONS["temperature_prewarning_margin_c"], overrides
    )
    humidity, _ = _resolve(
        SETTING_DEFINITIONS["humidity_prewarning_margin_rh"], overrides
    )
    temperature_exit, _ = _resolve(
        SETTING_DEFINITIONS["temperature_prewarning_exit_margin_c"], overrides
    )
    humidity_exit, _ = _resolve(
        SETTING_DEFINITIONS["humidity_prewarning_exit_margin_rh"], overrides
    )
    return {
        "temperature": float(temperature),
        "humidity": float(humidity),
        "temperature_exit": max(float(temperature), float(temperature_exit)),
        "humidity_exit": max(float(humidity), float(humidity_exit)),
    }


def feishu_notify_flags() -> dict[str, bool]:
    """All four Feishu IM notification switches in one read."""
    overrides = _read_overrides()
    flags: dict[str, bool] = {}
    for short_name, key in (
        ("alarm", "feishu_alarm_notify_enabled"),
        ("recovery", "feishu_recovery_notify_enabled"),
        ("prewarning", "feishu_prewarning_notify_enabled"),
        ("prewarning_recovery", "feishu_prewarning_recovery_notify_enabled"),
    ):
        value, _source = _resolve(SETTING_DEFINITIONS[key], overrides)
        flags[short_name] = bool(value)
    return flags


def settings_overview() -> dict[str, Any]:
    """Effective/default value plus schema metadata for every setting.

    Margin ``value`` fields carry the *effective* values the monitoring
    engine applies; exit margins follow the config.py invariant
    ``exit >= entry`` through the same ``max()`` normalization, so the
    console can never display (or apply) a hysteresis band narrower than
    the matching entry band.
    """
    overrides = _read_overrides()
    margins = _prewarning_margins_from(overrides)
    effective_margins = {
        "temperature_prewarning_margin_c": margins["temperature"],
        "humidity_prewarning_margin_rh": margins["humidity"],
        "temperature_prewarning_exit_margin_c": margins["temperature_exit"],
        "humidity_prewarning_exit_margin_rh": margins["humidity_exit"],
    }
    overview: dict[str, Any] = {}
    for key, definition in SETTING_DEFINITIONS.items():
        value, source = _resolve(definition, overrides)
        if key in effective_margins:
            value = effective_margins[key]
        overview[key] = {
            "value": value,
            "default": _default_value(definition),
            "type": definition.value_type,
            "unit": definition.unit,
            "label": definition.label,
            "min": definition.minimum,
            "max": definition.maximum,
            "source": source,
        }
    return overview


def _validate_input(definition: SettingDefinition, value: Any) -> float | bool:
    """Strictly validate one JSON-decoded input value against the schema."""
    if definition.value_type == "bool":
        if not isinstance(value, bool):
            raise SettingsValidationError(
                f"{definition.label} 必须是布尔值 true/false", field=definition.key
            )
        return value

    # JSON bool is an int subclass; a numeric field must never accept it.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsValidationError(
            f"{definition.label} 必须是数字", field=definition.key
    )
    number = float(value)
    if not math.isfinite(number):
        raise SettingsValidationError(
            f"{definition.label} 必须是有限数字（不允许 NaN/inf）", field=definition.key
        )
    minimum = definition.minimum
    maximum = definition.maximum
    if (minimum is not None and number < minimum) or (
        maximum is not None and number > maximum
    ):
        unit = f" {definition.unit}" if definition.unit else ""
        raise SettingsValidationError(
            f"{definition.label} 超出合法范围 [{minimum}, {maximum}]{unit}，"
            f"当前值: {number}",
            field=definition.key,
        )
    return number


def _validate_change_set(changes: Any) -> dict[str, float | bool]:
    if not isinstance(changes, Mapping):
        raise SettingsValidationError("changes 必须是 JSON 对象")
    if not changes:
        raise SettingsValidationError("changes 不能为空")
    unknown = sorted(str(key) for key in changes if str(key) not in SETTING_DEFINITIONS)
    if unknown:
        raise SettingsValidationError(
            "未知或不允许修改的配置项: " + ", ".join(unknown)
            + "；允许的配置项: " + ", ".join(SETTING_DEFINITIONS)
        )
    normalized: dict[str, float | bool] = {}
    for key, value in changes.items():
        normalized[str(key)] = _validate_input(SETTING_DEFINITIONS[str(key)], value)
    return normalized


def _validate_reason(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise SettingsValidationError("reason 不能为空：每次配置变更必须记录原因")
    normalized = reason.strip()
    if len(normalized) > _REASON_MAX_LENGTH:
        raise SettingsValidationError(
            f"reason 长度不能超过 {_REASON_MAX_LENGTH} 字符"
        )
    return normalized


def apply_changes(
    changes: Any,
    *,
    reason: Any,
    changed_by: str = "console",
) -> dict[str, Any]:
    """Validate, then atomically persist one settings change set.

    Every key gets exactly one ``runtime_setting_audit`` row.  Setting a
    value back to its config default clears the override row, returning the
    parameter to ``source=default`` semantics.
    """
    normalized = _validate_change_set(changes)
    validated_reason = _validate_reason(reason)
    actor = (changed_by or "console").strip()[:_CHANGED_BY_MAX_LENGTH] or "console"

    connection = db.peek_connection()
    if connection is None:
        raise SettingsWriteError("SQLite 本地镜像未启用或初始化失败，无法保存配置")

    now = _now_text()

    def _apply_locked() -> None:
        # Fresh list per attempt: run_sqlite_write_with_retry re-runs this
        # whole callback after a lock-conflict rollback, so rows appended by
        # an aborted attempt must not leak into the retry (exactly one audit
        # row per key per successful change).
        audit_rows: list[tuple[str, str, str]] = []
        # Read current state inside the write lock so the audit trail can
        # never observe a half-applied change set.  The entry/exit hysteresis
        # invariant itself is enforced at read time (config.py semantics).
        current: dict[str, float | bool] = {}
        for key in SETTING_DEFINITIONS:
            value, _source = _resolve(SETTING_DEFINITIONS[key], _read_overrides())
            current[key] = value

        for key, new_value in normalized.items():
            definition = SETTING_DEFINITIONS[key]
            old_value = current[key]
            if new_value == _default_value(definition):
                # Writing the default clears the override; the parameter
                # resumes following config.py / .env.
                connection.execute(
                    "DELETE FROM runtime_settings WHERE key = ?", (key,)
                )
            else:
                connection.execute(
                    """
                    INSERT INTO runtime_settings (
                        key, value, value_type, updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        value_type = excluded.value_type,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    (
                        key,
                        _format_value(definition, new_value),
                        definition.value_type,
                        now,
                        actor,
                    ),
                )
            audit_rows.append(
                (
                    key,
                    _format_value(definition, old_value),
                    _format_value(definition, new_value),
                )
            )
        for key, old_text, new_text in audit_rows:
            connection.execute(
                """
                INSERT INTO runtime_setting_audit (
                    key, old_value, new_value, changed_at, changed_by, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, old_text, new_text, now, actor, validated_reason),
            )
        connection.commit()

    try:
        run_sqlite_write_with_retry(connection, "runtime_settings.apply_changes", _apply_locked)
    except sqlite3.Error as exc:
        try:
            with SQLITE_WRITE_LOCK:
                if connection.in_transaction:
                    connection.rollback()
        except sqlite3.Error:
            logger.exception("runtime_settings 回滚失败")
        raise SettingsWriteError(f"配置写入数据库失败：{exc}") from exc

    logger.info(
        "runtime_settings 已更新并立即生效 | keys=%s | changed_by=%s | reason=%s",
        sorted(normalized),
        actor,
        validated_reason,
    )
    return {
        "updated": sorted(normalized),
        "settings": settings_overview(),
    }


def fetch_audit(limit: int = _AUDIT_DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Most recent setting-change audit rows, newest first."""
    bounded = max(1, min(int(limit), _AUDIT_MAX_LIMIT))
    connection = db.peek_connection()
    if connection is None:
        return []
    try:
        with SQLITE_WRITE_LOCK:
            rows = connection.execute(
                """
                SELECT key, old_value, new_value, changed_at, changed_by, reason
                FROM runtime_setting_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
    except sqlite3.Error:
        logger.exception("查询 runtime_setting_audit 失败")
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        definition = SETTING_DEFINITIONS.get(str(row["key"]))
        result.append(
            {
                "key": row["key"],
                "label": definition.label if definition else str(row["key"]),
                "old_value": row["old_value"],
                "new_value": row["new_value"],
                "changed_at": row["changed_at"],
                "changed_by": row["changed_by"],
                "reason": row["reason"],
            }
        )
    return result
