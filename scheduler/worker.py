"""A deliberately domain-agnostic scheduler for durable automation tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Mapping

from domain.models import AutomationTask
from repositories.automation_tasks import SQLiteAutomationTaskRepository


TaskHandler = Callable[[AutomationTask], None]


@dataclass(frozen=True)
class SchedulerRunReport:
    claimed: int
    succeeded: int
    failed: int
    skipped: int


class TaskScheduler:
    """Claim, dispatch, and finish tasks without knowing task business meaning."""

    def __init__(
        self,
        *,
        repository: SQLiteAutomationTaskRepository,
        handlers: Mapping[str, TaskHandler],
        worker_id: str = "scheduler",
        lease_for: timedelta = timedelta(minutes=5),
        poll_interval: float = 1.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.repository = repository
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.lease_for = lease_for
        self.poll_interval = poll_interval
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())

    def run_once(
        self,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> SchedulerRunReport:
        current_time = now or self.now_provider()
        tasks = self.repository.claim_due(
            now=current_time,
            limit=limit,
            worker_id=self.worker_id,
            lease_for=self.lease_for,
        )
        succeeded = 0
        failed = 0
        skipped = 0
        for task in tasks:
            handler = self.handlers.get(task.task_type)
            if handler is None:
                self.repository.mark_failed(
                    task.task_id,
                    finished_at=current_time,
                    error=f"no handler for task type {task.task_type}",
                    worker_id=self.worker_id,
                )
                failed += 1
                continue
            try:
                handler(task)
            except Exception as exc:  # noqa: BLE001 - persist handler failure
                self.repository.mark_failed(
                    task.task_id,
                    finished_at=current_time,
                    error=str(exc),
                    worker_id=self.worker_id,
                )
                failed += 1
                continue
            self.repository.mark_succeeded(
                task.task_id,
                finished_at=current_time,
                worker_id=self.worker_id,
            )
            succeeded += 1
        return SchedulerRunReport(
            claimed=len(tasks),
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
        )

    def run_forever(self, stop_event: object) -> None:
        """Run until ``stop_event.wait`` returns true.

        The small protocol keeps this class usable with ``threading.Event``
        without making scheduler tests depend on threads.
        """
        wait = getattr(stop_event, "wait", None)
        if not callable(wait):
            raise TypeError("stop_event must provide wait(timeout) -> bool")
        while True:
            self.run_once()
            if wait(self.poll_interval):
                return
