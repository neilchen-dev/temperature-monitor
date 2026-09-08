from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from tools.sqlite_backup import backup_sqlite, quick_check


def test_wal_backup_and_restore_preserve_committed_data() -> None:
    prefix = Path.cwd() / f".sqlite-backup-test-{uuid4().hex}"
    source = prefix.with_suffix(".source.db")
    backup = prefix.with_suffix(".backup.db")
    restored = prefix.with_suffix(".restored.db")
    connection = None

    try:
        connection = sqlite3.connect(source)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO readings(value) VALUES ('committed in wal')")
        connection.commit()
        # Keep the source connection open: the online backup must see the same
        # committed state even while a WAL exists.
        assert backup_sqlite(source, backup) == backup
        assert quick_check(backup) == "ok"
        backup_sqlite(backup, restored)
        restored_connection = sqlite3.connect(restored)
        try:
            row = restored_connection.execute(
                "SELECT value FROM readings WHERE id = 1"
            ).fetchone()
        finally:
            restored_connection.close()
    finally:
        if connection is not None:
            connection.close()
        for path in (source, backup, restored):
            path.unlink(missing_ok=True)
            path.with_name(path.name + "-wal").unlink(missing_ok=True)
            path.with_name(path.name + "-shm").unlink(missing_ok=True)
    assert row == ("committed in wal",)
