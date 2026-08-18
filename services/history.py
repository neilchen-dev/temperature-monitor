from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from services.feishu import (
    create_history_record,
    delete_history_records,
    find_expired_history_record_ids,
    get_latest_history_timestamp,
    list_realtime_snapshots,
    normalize_field_value,
)


logger = logging.getLogger("temperature_monitor")
EXPECTED_HISTORY_DEVICES = tuple(f"TH-{index:02d}" for index in range(1, 12))
SNAPSHOT_FIELDS = [
    "设备编号",
    "区域",
    "当前温度",
    "当前湿度",
    "在线状态",
    "温度判定",
    "湿度判定",
    "当前工艺",
    "当前判定状态",
    "当前作业状态",
    "警报状态",
]
TEXT_HISTORY_FIELDS = [
    "设备编号",
    "区域",
    "在线状态",
    "温度判定",
    "湿度判定",
    "当前工艺",
    "当前判定状态",
    "当前作业状态",
    "警报状态",
]

_sample_run_lock = threading.Lock()
_latest_sample_cache: dict[str, datetime | None] = {}
_cleanup_date_by_table: dict[str, date] = {}


class HistoryConfigurationError(RuntimeError):
    pass


def validate_history_config() -> ZoneInfo:
    if not config.HISTORY_API_KEY:
        raise HistoryConfigurationError("缺少 HISTORY_API_KEY")
    if len(config.HISTORY_API_KEY.encode("utf-8")) < 32:
        raise HistoryConfigurationError("HISTORY_API_KEY 必须至少 32 字节")
    if config.HISTORY_INTERVAL_MINUTES <= 0 or config.HISTORY_INTERVAL_MINUTES > 1440:
        raise HistoryConfigurationError("HISTORY_INTERVAL_MINUTES 必须在 1 到 1440 之间")
    if config.HISTORY_RETENTION_DAYS <= 0:
        raise HistoryConfigurationError("HISTORY_RETENTION_DAYS 必须大于 0")
    if not 0 <= config.HISTORY_CLEANUP_HOUR <= 23:
        raise HistoryConfigurationError("HISTORY_CLEANUP_HOUR 必须在 0 到 23 之间")

    expected = set(EXPECTED_HISTORY_DEVICES)
    actual = set(config.HISTORY_TABLE_MAP)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"缺少设备: {', '.join(missing)}")
        if extra:
            details.append(f"未知设备: {', '.join(extra)}")
        raise HistoryConfigurationError(
            "HISTORY_TABLE_MAP 必须完整包含 TH-01 至 TH-11；" + "；".join(details)
        )

    table_ids = list(config.HISTORY_TABLE_MAP.values())
    if len(set(table_ids)) != len(table_ids):
        raise HistoryConfigurationError("HISTORY_TABLE_MAP 中的 table_id 不能重复")
    invalid_ids = [table_id for table_id in table_ids if not table_id.startswith("tbl")]
    if invalid_ids:
        raise HistoryConfigurationError("HISTORY_TABLE_MAP 包含无效 table_id")

    try:
        return ZoneInfo(config.HISTORY_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise HistoryConfigurationError(
            f"无效 HISTORY_TIMEZONE: {config.HISTORY_TIMEZONE}"
        ) from exc


def floor_sample_time(now: datetime | None = None) -> datetime:
    timezone = validate_history_config()
    local_now = now.astimezone(timezone) if now else datetime.now(timezone)
    minutes_since_midnight = local_now.hour * 60 + local_now.minute
    bucket_minutes = (
        minutes_since_midnight // config.HISTORY_INTERVAL_MINUTES
    ) * config.HISTORY_INTERVAL_MINUTES
    midnight = datetime.combine(local_now.date(), datetime_time.min, tzinfo=timezone)
    return midnight + timedelta(minutes=bucket_minutes)


def _coerce_history_datetime(value: Any, timezone: ZoneInfo) -> datetime | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        return _coerce_history_datetime(value[0] if value else None, timezone)
    if isinstance(value, dict):
        for key in ("timestamp", "value", "text"):
            if key in value:
                return _coerce_history_datetime(value[key], timezone)
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, timezone)

    text = str(value).strip()
    if not text:
        return None
    try:
        return _coerce_history_datetime(float(text), timezone)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            raise RuntimeError(f"无法解析历史采集时间: {text}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _normalize_number(value: Any) -> int | float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return value
        return None
    try:
        numeric = float(str(value).strip())
    except ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def build_history_fields(fields: dict[str, Any], sample_time: datetime) -> dict[str, Any]:
    history_fields: dict[str, Any] = {
        field_name: normalize_field_value(fields.get(field_name))
        for field_name in TEXT_HISTORY_FIELDS
    }
    online = history_fields["在线状态"] == "在线"
    history_fields["当前温度"] = (
        _normalize_number(fields.get("当前温度")) if online else None
    )
    history_fields["当前湿度"] = (
        _normalize_number(fields.get("当前湿度")) if online else None
    )
    history_fields["采集时间"] = int(sample_time.timestamp() * 1000)
    return history_fields


def _index_realtime_snapshots(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected = set(EXPECTED_HISTORY_DEVICES)
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []

    for record in records:
        fields = record.get("fields", {})
        device = normalize_field_value(fields.get("设备编号")).upper()
        if device not in expected:
            continue
        if device in indexed:
            duplicates.append(device)
        indexed[device] = fields

    if duplicates:
        raise RuntimeError(
            "实时总表设备编号重复: " + ", ".join(sorted(set(duplicates)))
        )
    missing = sorted(expected - set(indexed))
    if missing:
        raise RuntimeError("实时总表缺少设备: " + ", ".join(missing))
    return indexed


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def run_cleanup_if_due(sample_time: datetime) -> dict[str, Any]:
    cleanup_enabled = bool(config.HISTORY_CLEANUP_ENABLED)
    if sample_time.hour < config.HISTORY_CLEANUP_HOUR:
        return {
            "status": "not_due",
            "enabled": cleanup_enabled,
        }

    today = sample_time.date()
    cutoff_date = today - timedelta(days=config.HISTORY_RETENTION_DAYS)
    results: dict[str, Any] = {}
    failures: dict[str, str] = {}

    for device in EXPECTED_HISTORY_DEVICES:
        table_id = config.HISTORY_TABLE_MAP[device]
        if _cleanup_date_by_table.get(table_id) == today:
            results[device] = {"status": "already_checked"}
            continue
        try:
            expired_ids = find_expired_history_record_ids(
                table_id,
                cutoff_date.isoformat(),
            )
            if cleanup_enabled:
                deleted = 0
                for batch in _chunked(expired_ids, 500):
                    result = delete_history_records(table_id, batch)
                    if int(result.get("code", -1)) != 0:
                        raise RuntimeError(
                            f"code={result.get('code')}, msg={result.get('msg')}"
                        )
                    deleted += len(batch)
                results[device] = {
                    "status": "deleted",
                    "candidate_count": len(expired_ids),
                    "deleted_count": deleted,
                }
            else:
                results[device] = {
                    "status": "preflight_only",
                    "candidate_count": len(expired_ids),
                    "deleted_count": 0,
                }
            _cleanup_date_by_table[table_id] = today
        except Exception as exc:  # per-table retry is intentionally deferred
            logger.exception("历史清理检查失败 | device=%s", device)
            failures[device] = str(exc)

    status = "partial" if failures else (
        "completed" if cleanup_enabled else "disabled_preflight"
    )
    return {
        "status": status,
        "enabled": cleanup_enabled,
        "cutoff_date": cutoff_date.isoformat(),
        "tables": results,
        "failures": failures,
    }


def sample_history(now: datetime | None = None) -> tuple[dict[str, Any], int]:
    cleanup_enabled = bool(config.HISTORY_CLEANUP_ENABLED)
    if not _sample_run_lock.acquire(blocking=False):
        return {
            "status": "already_running",
            "cleanup_enabled": cleanup_enabled,
        }, 202

    started = time.monotonic()
    try:
        timezone = validate_history_config()
        sample_time = floor_sample_time(now)
        records = list_realtime_snapshots(SNAPSHOT_FIELDS)
        snapshots = _index_realtime_snapshots(records)

        created: list[str] = []
        skipped: list[str] = []
        failures: dict[str, str] = {}

        for device in EXPECTED_HISTORY_DEVICES:
            table_id = config.HISTORY_TABLE_MAP[device]
            try:
                if device not in _latest_sample_cache:
                    latest_raw = get_latest_history_timestamp(table_id)
                    _latest_sample_cache[device] = _coerce_history_datetime(
                        latest_raw,
                        timezone,
                    )
                latest = _latest_sample_cache[device]
                if latest is not None and latest >= sample_time:
                    skipped.append(device)
                    continue

                history_fields = build_history_fields(snapshots[device], sample_time)
                result = create_history_record(table_id, history_fields)
                if int(result.get("code", -1)) != 0:
                    raise RuntimeError(
                        f"code={result.get('code')}, msg={result.get('msg')}"
                    )
                _latest_sample_cache[device] = sample_time
                created.append(device)
            except Exception as exc:
                logger.exception("历史快照写入失败 | device=%s", device)
                failures[device] = str(exc)

        cleanup = run_cleanup_if_due(sample_time)
        duration_ms = int((time.monotonic() - started) * 1000)
        if failures and not created and not skipped:
            status_text = "error"
            http_status = 502
        elif failures:
            status_text = "partial"
            http_status = 207
        else:
            status_text = "success"
            http_status = 200

        logger.info(
            "历史采样完成 | sample_time=%s | created=%s | skipped=%s | "
            "failed=%s | cleanup_enabled=%s | duration_ms=%s",
            sample_time.isoformat(),
            len(created),
            len(skipped),
            len(failures),
            cleanup_enabled,
            duration_ms,
        )
        return {
            "status": status_text,
            "sample_time": sample_time.isoformat(),
            "created": created,
            "skipped_duplicates": skipped,
            "failures": failures,
            "cleanup_enabled": cleanup_enabled,
            "cleanup": cleanup,
            "duration_ms": duration_ms,
        }, http_status
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.exception("历史采样失败")
        return {
            "status": "error",
            "error": str(exc),
            "cleanup_enabled": cleanup_enabled,
            "duration_ms": duration_ms,
        }, 502
    finally:
        _sample_run_lock.release()
