"""SQLite local mirror for Feishu-bound data.

Design notes / semantics:

- Feishu Bitable stays the system of record. The mirror is best-effort:
  initialization or write failures are counted, logged, and swallowed so
  they can never break the Feishu write path, dedup logic, or cleanup.
- ``temperature_reports`` is an append-only *event log*: a duplicate HA
  webhook/retry legitimately produces multiple rows. It is not deduplicated
  by design; use ``history_snapshots`` for deduplicated business records.
- ``history_snapshots`` is the business record mirror, idempotent via the
  composite primary key ``(device, sample_time_ms)``.
- ``sample_time_ms`` is UTC epoch milliseconds; ``sample_time_iso`` carries
  the local timezone offset for display. NULL temperature/humidity means
  either offline (``online_status = '离线'``) or a non-numeric source value
  (online but unparseable) — distinguish via ``online_status``.
- Concurrency assumes a single process / single instance (Waitress threads
  guarded by one lock). Multi-process or multi-container deployment would
  need WAL-friendly coordination beyond the current scope.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any

import config


logger = logging.getLogger("temperature_monitor")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS temperature_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    device TEXT NOT NULL,
    temperature_c REAL,
    humidity REAL,
    status TEXT,
    feishu_code INTEGER,
    feishu_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_temperature_reports_device_time
    ON temperature_reports(device, recorded_at);

CREATE TABLE IF NOT EXISTS history_snapshots (
    device TEXT NOT NULL,
    sample_time_ms INTEGER NOT NULL,
    sample_time_iso TEXT NOT NULL,
    area TEXT,
    temperature REAL,
    humidity REAL,
    online_status TEXT,
    temp_judgment TEXT,
    humidity_judgment TEXT,
    process TEXT,
    overall_judgment TEXT,
    work_status TEXT,
    alarm_status TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (device, sample_time_ms)
);
"""

_lock = threading.RLock()
_connection: sqlite3.Connection | None = None
_init_failed = False
_write_failures = 0


def _get_connection() -> sqlite3.Connection | None:
    global _connection, _init_failed

    if _init_failed or not config.SQLITE_ENABLED:
        return None
    if _connection is not None:
        return _connection

    try:
        config.SQLITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(config.SQLITE_DB_PATH),
            check_same_thread=False,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        with _lock:
            connection.executescript(_SCHEMA)
            connection.commit()
        _connection = connection
        logger.info(
            "SQLite 本地镜像已启用 | path=%s", config.SQLITE_DB_PATH
        )
        return _connection
    except sqlite3.Error:
        _init_failed = True
        logger.exception(
            "SQLite 初始化失败，本地镜像已停用 | path=%s", config.SQLITE_DB_PATH
        )
        return None


def init_db() -> None:
    """Initialize the local mirror early at startup; failures disable it."""
    _get_connection()


def close() -> None:
    """Close the connection (used by tests and shutdown)."""
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_enabled() -> bool:
    return bool(config.SQLITE_ENABLED) and _get_connection() is not None


def save_temperature_report(
    device: str,
    temperature_c: Any,
    humidity: Any,
    status: str,
    feishu_code: int,
    feishu_message: str,
) -> None:
    """Mirror a HA temperature report alongside the CSV history."""
    if not config.SQLITE_ENABLED:
        return

    connection = _get_connection()
    if connection is None:
        return

    try:
        with _lock:
            connection.execute(
                "INSERT INTO temperature_reports ("
                " recorded_at, device, temperature_c, humidity, status,"
                " feishu_code, feishu_message"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_text(),
                    device,
                    temperature_c if isinstance(temperature_c, (int, float)) else None,
                    humidity if isinstance(humidity, (int, float)) else None,
                    status,
                    feishu_code,
                    feishu_message,
                ),
            )
            connection.commit()
    except sqlite3.Error:
        global _write_failures
        _write_failures += 1
        logger.exception("SQLite 写入温度上报镜像失败 | device=%s", device)


_SNAPSHOT_COLUMN_MAP = (
    ("area", "区域"),
    ("online_status", "在线状态"),
    ("temp_judgment", "温度判定"),
    ("humidity_judgment", "湿度判定"),
    ("process", "当前工艺"),
    ("overall_judgment", "当前判定状态"),
    ("work_status", "当前作业状态"),
    ("alarm_status", "警报状态"),
)


def save_history_snapshot(
    device: str,
    sample_time: datetime,
    history_fields: dict[str, Any],
) -> None:
    """Mirror a history snapshot after its successful Feishu write.

    Keyed by (device, sample_time_ms) so repeated calls are idempotent.
    """
    if not config.SQLITE_ENABLED:
        return

    connection = _get_connection()
    if connection is None:
        return

    columns = ["device", "sample_time_ms", "sample_time_iso", "temperature", "humidity"]
    values: list[Any] = [
        device,
        int(history_fields.get("采集时间", int(sample_time.timestamp() * 1000))),
        sample_time.isoformat(),
        history_fields.get("当前温度"),
        history_fields.get("当前湿度"),
    ]
    for column, field_name in _SNAPSHOT_COLUMN_MAP:
        columns.append(column)
        values.append(history_fields.get(field_name))
    columns.append("created_at")
    values.append(_now_text())

    placeholders = ", ".join("?" for _ in columns)
    try:
        with _lock:
            connection.execute(
                f"INSERT OR REPLACE INTO history_snapshots ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )
            connection.commit()
    except sqlite3.Error:
        global _write_failures
        _write_failures += 1
        logger.exception("SQLite 写入历史快照镜像失败 | device=%s", device)


def fetch_history_snapshots(
    device: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Read mirrored snapshots for local querying and verification."""
    connection = _get_connection()
    if connection is None:
        return []

    query = "SELECT * FROM history_snapshots WHERE 1=1"
    params: list[Any] = []
    if device:
        query += " AND device = ?"
        params.append(device.upper())
    if start_ms is not None:
        query += " AND sample_time_ms >= ?"
        params.append(start_ms)
    if end_ms is not None:
        query += " AND sample_time_ms < ?"
        params.append(end_ms)
    query += " ORDER BY sample_time_ms ASC LIMIT ?"
    params.append(max(1, min(int(limit), 10000)))

    try:
        with _lock:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        logger.exception("SQLite 查询历史快照失败 | device=%s", device)
        return []


def get_stats() -> dict[str, Any]:
    """Mirror health snapshot for /health: failures and row counts.

    Row counts double as a rough lag indicator when compared against the
    Feishu-side record counts.
    """
    global _write_failures

    stats: dict[str, Any] = {
        "enabled": bool(config.SQLITE_ENABLED) and _get_connection() is not None,
        "write_failures": _write_failures,
        "temperature_report_count": 0,
        "history_snapshot_count": 0,
    }
    connection = _get_connection()
    if connection is None:
        return stats

    try:
        with _lock:
            stats["temperature_report_count"] = connection.execute(
                "SELECT COUNT(*) FROM temperature_reports"
            ).fetchone()[0]
            stats["history_snapshot_count"] = connection.execute(
                "SELECT COUNT(*) FROM history_snapshots"
            ).fetchone()[0]
    except sqlite3.Error:
        logger.exception("SQLite 统计信息查询失败")
    return stats


def fetch_daily_stats(
    device: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Per-device, per-local-day aggregates over mirrored snapshots.

    Abnormal counts exclude offline samples so a powered-off device does not
    look like a temperature excursion. Day grouping uses the local date from
    ``sample_time_iso`` (not UTC) so boundaries match the site timezone.
    """
    connection = _get_connection()
    if connection is None:
        return []

    query = """
        SELECT
            substr(sample_time_iso, 1, 10) AS local_date,
            device,
            COUNT(*) AS sample_count,
            AVG(temperature) AS avg_temperature,
            MIN(temperature) AS min_temperature,
            MAX(temperature) AS max_temperature,
            AVG(humidity) AS avg_humidity,
            MIN(humidity) AS min_humidity,
            MAX(humidity) AS max_humidity,
            SUM(CASE WHEN online_status = '离线' THEN 1 ELSE 0 END) AS offline_count,
            SUM(CASE
                WHEN online_status = '在线'
                 AND temp_judgment IS NOT NULL AND temp_judgment <> '正常'
                THEN 1 ELSE 0 END) AS temp_abnormal_count,
            SUM(CASE
                WHEN online_status = '在线'
                 AND humidity_judgment IS NOT NULL AND humidity_judgment <> '正常'
                THEN 1 ELSE 0 END) AS humidity_abnormal_count
        FROM history_snapshots
        WHERE 1=1
    """
    params: list[Any] = []
    if device:
        query += " AND device = ?"
        params.append(device.upper())
    if start_ms is not None:
        query += " AND sample_time_ms >= ?"
        params.append(start_ms)
    if end_ms is not None:
        query += " AND sample_time_ms < ?"
        params.append(end_ms)
    query += " GROUP BY local_date, device ORDER BY local_date, device"

    try:
        with _lock:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        logger.exception("SQLite 每日统计查询失败 | device=%s", device)
        return []


def fetch_device_stats() -> list[dict[str, Any]]:
    """Per-device overview: last snapshot values, counts, and report recency.

    Uses a ROW_NUMBER() window function (instead of SQLite-specific bare
    columns with MAX) so the query stays portable to PostgreSQL/MySQL:
    rn = 1 selects each device's latest snapshot by ``sample_time_ms``.
    """
    connection = _get_connection()
    if connection is None:
        return []

    snapshot_sql = """
        WITH ranked AS (
            SELECT
                device,
                sample_time_ms,
                sample_time_iso,
                temperature,
                humidity,
                online_status,
                ROW_NUMBER() OVER (
                    PARTITION BY device
                    ORDER BY sample_time_ms DESC
                ) AS rn,
                COUNT(*) OVER (PARTITION BY device) AS snapshot_count,
                SUM(CASE WHEN online_status = '离线' THEN 1 ELSE 0 END)
                    OVER (PARTITION BY device) AS offline_sample_count
            FROM history_snapshots
        )
        SELECT
            device,
            snapshot_count,
            offline_sample_count,
            sample_time_ms AS last_sample_ms,
            sample_time_iso AS last_sample_iso,
            temperature AS last_temperature,
            humidity AS last_humidity,
            online_status AS last_online_status
        FROM ranked
        WHERE rn = 1
        ORDER BY device
    """
    report_sql = """
        SELECT
            device,
            COUNT(*) AS report_count,
            MAX(recorded_at) AS last_report_at
        FROM temperature_reports
        GROUP BY device
    """

    try:
        with _lock:
            snapshot_rows = connection.execute(snapshot_sql).fetchall()
            report_rows = connection.execute(report_sql).fetchall()
    except sqlite3.Error:
        logger.exception("SQLite 设备统计查询失败")
        return []

    report_by_device = {row["device"]: dict(row) for row in report_rows}
    results: list[dict[str, Any]] = []
    for row in snapshot_rows:
        item = dict(row)
        report = report_by_device.get(item["device"], {})
        item["report_count"] = report.get("report_count", 0)
        item["last_report_at"] = report.get("last_report_at")
        results.append(item)
    # Devices seen in reports but never snapshotted are still surfaced so a
    # device mapping issue stays visible instead of silently disappearing.
    snapshotted = {row["device"] for row in snapshot_rows}
    for device, report in report_by_device.items():
        if device not in snapshotted:
            results.append({
                "device": device,
                "snapshot_count": 0,
                "offline_sample_count": 0,
                "last_sample_ms": None,
                "last_sample_iso": None,
                "last_temperature": None,
                "last_humidity": None,
                "last_online_status": None,
                "report_count": report.get("report_count", 0),
                "last_report_at": report.get("last_report_at"),
            })
    results.sort(key=lambda item: item["device"])
    return results
