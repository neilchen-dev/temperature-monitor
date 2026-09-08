"""WAL-safe SQLite backup and verification helper for deployment workflows."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def quick_check(database: str | Path) -> str:
    """Run SQLite's integrity quick check without modifying the database."""
    path = Path(database)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else ""


def backup_sqlite(source: str | Path, destination: str | Path) -> Path:
    """Create a consistent online backup, including pages currently in WAL."""
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("SQLite backup destination must differ from source")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(
        f"file:{source_path}?mode=ro", uri=True, timeout=30
    )
    destination_connection = sqlite3.connect(str(destination_path), timeout=30)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()

    result = quick_check(destination_path)
    if result != "ok":
        raise RuntimeError(
            f"SQLite backup quick_check failed for {destination_path}: {result}"
        )
    return destination_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    backup_path = backup_sqlite(args.source, args.backup)
    print(f"backup_path={backup_path}")
    print("quick_check=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
