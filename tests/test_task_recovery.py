"""Scheduler 任务崩溃恢复 / lease 回收一致性测试（P1）。

场景：worker A claim 后崩溃 → lease 过期 → worker B 重新 claim →
A 的迟到 finish 被拒绝 → B 正常完成。另覆盖 FAILED 不阻塞新任务、
过期 RUNNING 任务不被无限重复执行。
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from repositories.automation_tasks import (
    SQLiteAutomationTaskRepository,
    TaskStateError,
)


TZ = ZoneInfo("Asia/Shanghai")


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.connection.close)
        self.repository = SQLiteAutomationTaskRepository(self.connection)
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=TZ)

    def _create(self, dedupe_key: str, due_at: datetime | None = None):
        return self.repository.create_or_get(
            task_type="VERIFY_ALARM",
            entity_type="DEVICE",
            entity_id="TH-10",
            due_at=due_at or self.now,
            payload={"device_id": "TH-10"},
            dedupe_key=dedupe_key,
            created_at=self.now,
        )

    def test_crashed_worker_lease_is_reclaimed(self) -> None:
        task = self._create("R1")
        claimed = self.repository.claim_due(
            now=self.now, worker_id="worker-a", lease_for=timedelta(minutes=5)
        )
        self.assertEqual([t.task_id for t in claimed], [task.task_id])

        # worker A 崩溃：没有 finish；lease 过期后 worker B 接管。
        later = self.now + timedelta(minutes=6)
        reclaimed = self.repository.claim_due(
            now=later, worker_id="worker-b", lease_for=timedelta(minutes=5)
        )
        self.assertIn(task.task_id, [t.task_id for t in reclaimed])
        state = self.repository.get(task.task_id)
        self.assertEqual(state.worker_id, "worker-b")
        self.assertEqual(state.attempt_count, 2)

        # 崩溃 worker A 的迟到 finish 必须被拒绝（lease 已归 B）。
        with self.assertRaises(TaskStateError):
            self.repository.mark_succeeded(
                task.task_id, finished_at=later + timedelta(seconds=1), worker_id="worker-a"
            )

        self.repository.mark_succeeded(
            task.task_id,
            finished_at=later + timedelta(seconds=1),
            worker_id="worker-b",
        )
        final = self.repository.get(task.task_id)
        self.assertEqual(final.status.value, "SUCCEEDED")

    def test_expired_lease_finish_is_rejected_even_by_owner(self) -> None:
        task = self._create("R2")
        self.repository.claim_due(
            now=self.now, worker_id="worker-a", lease_for=timedelta(minutes=5)
        )
        # A 没死，只是处理太久：lease 已过期但没人 reclaim。
        late_finish = self.now + timedelta(minutes=6)
        with self.assertRaises(TaskStateError):
            self.repository.mark_succeeded(
                task.task_id, finished_at=late_finish, worker_id="worker-a"
            )

    def test_failed_task_is_terminal_and_never_reclaimed(self) -> None:
        task = self._create("R3")
        self.repository.claim_due(
            now=self.now, worker_id="worker-a", lease_for=timedelta(minutes=5)
        )
        self.repository.mark_failed(
            task.task_id,
            finished_at=self.now + timedelta(seconds=1),
            error="boom",
            worker_id="worker-a",
        )
        later = self.now + timedelta(hours=2)
        reclaimed = self.repository.claim_due(
            now=later, worker_id="worker-b", lease_for=timedelta(minutes=5)
        )
        self.assertNotIn(task.task_id, [t.task_id for t in reclaimed])
        # FAILED 任务不阻塞新任务（不同 dedupe key）。
        fresh = self._create("R4", due_at=later)
        claimed = self.repository.claim_due(
            now=later, worker_id="worker-b", lease_for=timedelta(minutes=5)
        )
        self.assertEqual([t.task_id for t in claimed], [fresh.task_id])

    def test_running_task_with_active_lease_is_not_reclaimed(self) -> None:
        self._create("R5")
        self.repository.claim_due(
            now=self.now, worker_id="worker-a", lease_for=timedelta(minutes=5)
        )
        claimed = self.repository.claim_due(
            now=self.now + timedelta(minutes=1),
            worker_id="worker-b",
            lease_for=timedelta(minutes=5),
        )
        self.assertEqual(claimed, ())

    def test_pending_task_survives_restart(self) -> None:
        """重启后未执行任务仍可被 claim（durable queue 语义）。"""
        task = self._create("R6", due_at=self.now + timedelta(minutes=10))
        after_restart = self.now + timedelta(minutes=11)
        claimed = self.repository.claim_due(
            now=after_restart, worker_id="worker-new", lease_for=timedelta(minutes=5)
        )
        self.assertEqual([t.task_id for t in claimed], [task.task_id])


if __name__ == "__main__":
    unittest.main()
