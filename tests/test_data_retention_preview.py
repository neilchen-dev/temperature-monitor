from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import tools.data_retention_preview as retention


def test_preview_counts_offset_timestamps_and_never_modifies_rows(tmp_path, monkeypatch):
    fixed = datetime(2026, 10, 19, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(retention, "datetime", FrozenDateTime)
    source = tmp_path / "data.db"
    cutoff = fixed - timedelta(days=180)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE temperature_reports (recorded_at TEXT)")
        older = (cutoff - timedelta(hours=1)).astimezone(timezone(timedelta(hours=8)))
        newer = cutoff + timedelta(hours=1)
        connection.executemany("INSERT INTO temperature_reports VALUES (?)",
                               [(older.isoformat(),), (newer.isoformat(),)])
        for table in ("device_samples", "history_snapshots"):
            connection.execute(f"CREATE TABLE {table} (sample_time_ms INTEGER)")
            connection.executemany(f"INSERT INTO {table} VALUES (?)",
                                   [(int(cutoff.timestamp()*1000)-1,),
                                    (int(cutoff.timestamp()*1000)+1,)])
    before = source.read_bytes()
    result = retention.preview(source)
    assert result["deleted_rows"] == 0
    assert all(item["older_rows"] == 1 for item in result["tables"].values())
    assert source.read_bytes() == before


def test_preview_rejects_invalid_policy_and_does_not_create_database(tmp_path):
    source = tmp_path / "missing.db"
    with pytest.raises(ValueError):
        retention.preview(source, days=0)
    with pytest.raises(sqlite3.OperationalError):
        retention.preview(source)
    assert not source.exists()
