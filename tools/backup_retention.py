"""Preview retention of dated automated backups; deletion requires --apply.

Manual/canary snapshots, unrelated files, symlinks and live WAL databases are
excluded. A backup must exceed both the age and newest-count retention limits.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat
import time

_MANAGED_NAME = re.compile(r"temperature_monitor_\d{8}T\d{6}Z\.db\Z")


@dataclass(frozen=True)
class Backup:
    path: Path
    size: int
    modified_ns: int
    inode: int


def plan_retention(directory: Path, *, keep_latest: int = 3, older_than_days: int = 30,
                   now: float | None = None) -> tuple[Backup, ...]:
    if keep_latest < 1 or older_than_days < 1:
        raise ValueError("retention must keep at least one backup for at least one day")
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("backup directory must be an existing non-symlink directory")
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    backups = []
    for path in directory.iterdir():
        if not _MANAGED_NAME.fullmatch(path.name):
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            continue
        # An attached WAL/SHM may indicate an actively used or incomplete DB.
        if any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            continue
        backups.append(Backup(path, info.st_size, info.st_mtime_ns, info.st_ino))
    backups.sort(key=lambda backup: (backup.modified_ns, backup.path.name), reverse=True)
    return tuple(backup for backup in backups[keep_latest:] if backup.modified_ns / 1e9 < cutoff)


def apply_retention(backups: tuple[Backup, ...]) -> int:
    """Recheck file identity before deleting a previewed candidate."""
    reclaimed = 0
    for backup in backups:
        info = backup.path.lstat()
        if not stat.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns, info.st_ino) != (backup.size, backup.modified_ns, backup.inode):
            raise RuntimeError(f"backup changed since preview: {backup.path.name}")
        if any(backup.path.with_name(backup.path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise RuntimeError(f"backup has active SQLite sidecars: {backup.path.name}")
        backup.path.unlink()
        reclaimed += backup.size
    return reclaimed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--keep-latest", type=int, default=3)
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    backups = plan_retention(args.directory, keep_latest=args.keep_latest, older_than_days=args.older_than_days)
    output = {"mode": "apply" if args.apply else "preview", "candidates": [backup.path.name for backup in backups],
              "candidate_bytes": sum(backup.size for backup in backups)}
    if args.apply:
        output["reclaimed_bytes"] = apply_retention(backups)
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
