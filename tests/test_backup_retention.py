import os
import time
from pathlib import Path

import pytest

from tools.backup_retention import apply_retention, plan_retention


def backup(directory: Path, index: int, days: int) -> Path:
    path = directory / f'temperature_monitor_202609{index:02}T000000Z.db'
    path.write_bytes(b'backup')
    age = time.time() - days * 86400
    os.utime(path, (age, age))
    return path


def test_retention_requires_age_and_count_and_only_managed_names(tmp_path):
    newest = backup(tmp_path, 1, 40)
    old = backup(tmp_path, 2, 50)
    recent = backup(tmp_path, 3, 2)
    for name in ['pre-active.db','temperature_monitor_manual_20260901T000000Z.db','notes.json','temperature_monitor.db']:
        (tmp_path/name).write_bytes(b'protected')
    plan = plan_retention(tmp_path,keep_latest=2,older_than_days=30)
    assert [item.path for item in plan] == [old]
    assert old.exists()  # preview is read-only
    assert apply_retention(plan) == 6
    assert not old.exists()
    assert newest.exists() and recent.exists()
    assert (tmp_path/'pre-active.db').exists()


def test_sidecars_symlinks_and_changed_files_are_protected(tmp_path):
    backup(tmp_path,1,40)
    active = backup(tmp_path,2,50)
    active.with_name(active.name+'-wal').touch()
    outside = tmp_path/'other.db'
    outside.write_bytes(b'keep')
    (tmp_path/'temperature_monitor_20260903T000000Z.db').symlink_to(outside)
    assert plan_retention(tmp_path,keep_latest=1) == ()
    old = backup(tmp_path,4,60)
    plan = plan_retention(tmp_path,keep_latest=1)
    old.write_bytes(b'changed contents')
    with pytest.raises(RuntimeError, match='changed'):
        apply_retention(plan)
    assert old.exists() and outside.exists() and active.exists()


@pytest.mark.parametrize('keep,days',[(0,30),(3,0),(-1,1)])
def test_invalid_policy_cannot_remove_backups(tmp_path,keep,days):
    with pytest.raises(ValueError):
        plan_retention(tmp_path,keep_latest=keep,older_than_days=days)
