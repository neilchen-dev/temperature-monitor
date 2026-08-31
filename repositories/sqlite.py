"""Small SQLite connection factory for application-owned repositories."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a repository connection; callers own its lifecycle."""
    path_value = str(path)
    if path_value != ":memory:":
        Path(path_value).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        path_value,
        check_same_thread=False,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    if path_value != ":memory:":
        connection.execute("PRAGMA journal_mode=WAL")
    return connection
