"""Read-only preview of local sample retention; never deletes business data."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time


def preview(source: Path, *, days: int = 180) -> dict:
    if days < 1:
        raise ValueError("days must be positive")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = {"mode": "preview_only", "days": days, "deleted_rows": 0, "tables": {}}
    connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA cache_size=-2048")
        for table, column in (("temperature_reports", "recorded_at"),
                              ("device_samples", "sample_time_ms"),
                              ("history_snapshots", "sample_time_ms")):
            deadline = time.monotonic() + 15
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            value = int(cutoff.timestamp() * 1000) if column.endswith("_ms") else cutoff.isoformat()
            predicate = f"{column} < ?" if column.endswith("_ms") else f"julianday({column}) < julianday(?)"
            try:
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {predicate}", (value,),
                ).fetchone()[0]
                result["tables"][table] = {"older_rows": count}
            except sqlite3.Error as exc:
                result["tables"][table] = {"error": str(exc)}
    finally:
        connection.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()
    output = preview(args.source, days=args.days)
    print(json.dumps(output, ensure_ascii=False))
    raise SystemExit(int(any("error" in row for row in output["tables"].values())))
