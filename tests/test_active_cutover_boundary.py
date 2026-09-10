from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from application.action_executor import ActionExecutionStatus, ActionExecutor
from domain.models import AlarmAction, AlarmActionType, AutomationTaskStatus, DeviceContext
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from runtime.shadow_runner import ShadowRuntime
from scheduler.worker import TaskScheduler


T0 = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)


def _task(
    repository: SQLiteAutomationTaskRepository,
    *,
    task_type: str,
    key: str,
    created_at: datetime,
):
    return repository.create_or_get(
        task_type=task_type,
        entity_type="EVENT",
        entity_id="event-1",
        due_at=created_at,
        payload={"device_id": "TH-01", "local_event_id": "event-1"},
        dedupe_key=key,
        created_at=created_at,
    )


def test_shadow_reconcile_is_quarantined_when_active_starts() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        repository.set_runtime_context(mode="shadow", now=T0)
        historical = _task(
            repository,
            task_type="RECONCILE_ALARM_EVENT",
            key="RECONCILE_ALARM_EVENT:event-shadow",
            created_at=T0,
        )

        activation = repository.set_runtime_context(
            mode="active", now=T0 + timedelta(minutes=10)
        )
        assert repository.quarantine_legacy_external_tasks(
            now=T0 + timedelta(minutes=10), active_epoch=activation.active_epoch
        ) == 1
        assert repository.get(historical.task_id).status is AutomationTaskStatus.LEGACY_PENDING

        remote_effects: list[str] = []
        scheduler = TaskScheduler(
            repository=repository,
            handlers={
                "RECONCILE_ALARM_EVENT": lambda task: remote_effects.append(task.task_id)
            },
        )
        report = scheduler.run_once(now=T0 + timedelta(minutes=10))
        assert report.claimed == 0
        assert remote_effects == []
    finally:
        connection.close()


def test_closed_event_with_pending_binding_becomes_shadow_only_at_cutover() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        events = SQLiteEnvironmentEventRepository(connection)
        tasks = SQLiteAutomationTaskRepository(connection)
        event = events.create_or_get_active(
            device_id="TH-01",
            event_key=f"ENV:TH-01:{T0.isoformat()}",
            opened_at=T0,
            payload={
                "feishu_binding_status": "PENDING",
                "feishu_create_attempted": True,
            },
        )
        events.mark_recovered(event.event_id, recovered_at=T0 + timedelta(minutes=2))
        activation = tasks.set_runtime_context(mode="active", now=T0 + timedelta(minutes=10))
        runtime = SimpleNamespace(
            active_canary_enabled=True,
            active_device_ids=("TH-01",),
            devices={"TH-01": DeviceContext("TH-01", "test")},
            event_repository=events,
            task_repository=tasks,
            _standards_ready=lambda: True,
        )

        ShadowRuntime._ensure_event_reconciliation_tasks(
            runtime, now=T0 + timedelta(minutes=10)
        )

        final = events.get(event.event_id)
        assert final.payload["feishu_binding_status"] == "LEGACY_PENDING"
        assert final.payload["external_effect_policy"] == "SHADOW_ONLY"
        assert connection.execute(
            "SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'RECONCILE_ALARM_EVENT'"
        ).fetchone()[0] == 0
        assert activation.active_epoch
    finally:
        connection.close()


def test_current_active_epoch_allows_create_and_notify_tasks() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        activation = repository.set_runtime_context(mode="active", now=T0)
        create = _task(
            repository,
            task_type="RECONCILE_ALARM_EVENT",
            key="RECONCILE_ALARM_EVENT:event-new",
            created_at=T0 + timedelta(seconds=1),
        )
        notify = _task(
            repository,
            task_type="NOTIFY_ALARM",
            key="NOTIFY_ALARM:event-new",
            created_at=T0 + timedelta(seconds=1),
        )
        assert create.created_mode == "active"
        assert create.active_epoch == activation.active_epoch
        assert notify.created_mode == "active"
        assert repository.external_effect_allowed(create)
        assert repository.external_effect_allowed(notify)

        effects: list[str] = []
        scheduler = TaskScheduler(
            repository=repository,
            handlers={
                "RECONCILE_ALARM_EVENT": lambda task: effects.append("CREATE"),
                "NOTIFY_ALARM": lambda task: effects.append("NOTIFY"),
            },
        )
        report = scheduler.run_once(now=T0 + timedelta(seconds=2))
        assert report.succeeded == 2
        assert set(effects) == {"CREATE", "NOTIFY"}
    finally:
        connection.close()


def test_current_epoch_pending_effect_does_not_self_block_executor() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        repository.set_runtime_context(mode="active", now=T0)
        task = _task(
            repository,
            task_type="RECONCILE_ALARM_EVENT",
            key="RECONCILE_ALARM_EVENT:event-pending",
            created_at=T0 + timedelta(seconds=1),
        )
        notify_task = _task(
            repository,
            task_type="NOTIFY_ALARM",
            key="NOTIFY_ALARM:event-pending",
            created_at=T0 + timedelta(seconds=1),
        )
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-01",),
            standards_ready_provider=lambda: repository.active_readiness(
                now=T0 + timedelta(seconds=2)
            )["active_readiness"],
            active_epoch_provider=lambda: repository.runtime_context().active_epoch,
            active_cutover_at_provider=lambda: repository.runtime_context().active_cutover_at,
            handlers={
                AlarmActionType.CREATE_ALARM_EVENT: lambda action: calls.append("CREATE"),
                AlarmActionType.NOTIFY_ALARM: lambda action: calls.append("NOTIFY"),
            },
        )
        create_action = AlarmAction(
            action_type=AlarmActionType.CREATE_ALARM_EVENT,
            device_id="TH-01",
            alarm_id="event-pending",
        )
        notify_action = AlarmAction(
            action_type=AlarmActionType.NOTIFY_ALARM,
            device_id="TH-01",
            alarm_id="event-pending",
        )

        executions = executor.execute(
            (create_action, notify_action),
            context={
                "device_id": "TH-01",
                "created_mode": task.created_mode,
                "active_epoch": task.active_epoch,
                "automation_task_id": task.task_id,
                "task_created_at": task.created_at.isoformat(),
            },
            created_at=T0 + timedelta(seconds=2),
        )

        summary = repository.active_readiness(now=T0 + timedelta(seconds=2))
        assert summary["current_epoch_pending_external_effects"] == 2
        assert summary["active_readiness"] is True
        assert all(item.status is ActionExecutionStatus.SUCCEEDED for item in executions)
        assert calls == ["CREATE", "NOTIFY"]

        claimed = repository.claim_due(
            now=T0 + timedelta(seconds=3),
            limit=2,
            worker_id="scheduler",
        )
        assert {item.task_id for item in claimed} == {task.task_id, notify_task.task_id}
        repository.mark_succeeded(
            task.task_id,
            finished_at=T0 + timedelta(seconds=3),
            worker_id="scheduler",
        )
        repository.mark_succeeded(
            notify_task.task_id,
            finished_at=T0 + timedelta(seconds=3),
            worker_id="scheduler",
        )
        settled = repository.active_readiness(now=T0 + timedelta(seconds=3))
        assert settled["current_epoch_pending_external_effects"] == 0
        assert settled["pending_external_effects"] == 0
    finally:
        connection.close()


def test_active_crash_retry_reclaims_same_current_epoch_task() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        repository.set_runtime_context(mode="active", now=T0)
        task = _task(
            repository,
            task_type="NOTIFY_ALARM",
            key="NOTIFY_ALARM:event-crash",
            created_at=T0,
        )
        first = repository.claim_due(now=T0, worker_id="worker-a", lease_for=timedelta(seconds=5))
        assert first[0].task_id == task.task_id
        second = repository.claim_due(
            now=T0 + timedelta(seconds=6), worker_id="worker-b", lease_for=timedelta(seconds=5)
        )
        assert second[0].task_id == task.task_id
        assert second[0].attempt_count == 2
        assert repository.external_effect_allowed(second[0])
    finally:
        connection.close()


def test_action_boundary_refuses_a_stale_epoch_before_handler() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        first = repository.set_runtime_context(mode="active", now=T0)
        repository.set_runtime_context(mode="shadow", now=T0 + timedelta(minutes=1))
        current = repository.set_runtime_context(mode="active", now=T0 + timedelta(minutes=2))
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-01",),
            standards_ready_provider=lambda: True,
            active_epoch_provider=lambda: current.active_epoch,
            handlers={AlarmActionType.CREATE_ALARM_EVENT: lambda action: calls.append("POST")},
        )
        execution = executor.execute(
            (
                AlarmAction(
                    action_type=AlarmActionType.CREATE_ALARM_EVENT,
                    device_id="TH-01",
                    alarm_id="event-old-epoch",
                ),
            ),
            context={
                "device_id": "TH-01",
                "created_mode": "active",
                "active_epoch": first.active_epoch,
            },
            created_at=T0 + timedelta(minutes=2),
        )[0]
        assert execution.status is ActionExecutionStatus.PLANNED
        assert calls == []
    finally:
        connection.close()


def test_action_boundary_refuses_a_task_created_before_cutover() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        activation = repository.set_runtime_context(mode="active", now=T0)
        calls: list[str] = []
        executor = ActionExecutor(
            mode="active",
            active_device_ids=("TH-01",),
            standards_ready_provider=lambda: True,
            active_epoch_provider=lambda: activation.active_epoch,
            active_cutover_at_provider=lambda: activation.active_cutover_at,
            handlers={AlarmActionType.CREATE_ALARM_EVENT: lambda action: calls.append("POST")},
        )
        execution = executor.execute(
            (
                AlarmAction(
                    action_type=AlarmActionType.CREATE_ALARM_EVENT,
                    device_id="TH-01",
                    alarm_id="event-before-cutover",
                ),
            ),
            context={
                "device_id": "TH-01",
                "created_mode": "active",
                "active_epoch": activation.active_epoch,
                "automation_task_id": "task-before-cutover",
                "task_created_at": (T0 - timedelta(seconds=1)).isoformat(),
            },
            created_at=T0 + timedelta(seconds=1),
        )[0]
        assert execution.status is ActionExecutionStatus.PLANNED
        assert "before the current Active cutover" in (execution.error or "")
        assert calls == []
    finally:
        connection.close()


def test_active_restart_reuses_epoch_but_older_history_does_not() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        first_activation = repository.set_runtime_context(mode="active", now=T0)
        current = _task(
            repository,
            task_type="NOTIFY_ALARM",
            key="NOTIFY_ALARM:event-current",
            created_at=T0,
        )
        restarted = repository.set_runtime_context(
            mode="active", now=T0 + timedelta(minutes=1)
        )
        assert restarted.active_epoch == first_activation.active_epoch
        assert repository.claim_due(now=T0 + timedelta(minutes=1))[0].task_id == current.task_id
        repository.mark_succeeded(
            current.task_id,
            finished_at=T0 + timedelta(minutes=1, seconds=1),
        )

        repository.set_runtime_context(mode="shadow", now=T0 + timedelta(minutes=2))
        historical = _task(
            repository,
            task_type="NOTIFY_RECOVERY",
            key="NOTIFY_RECOVERY:event-old",
            created_at=T0 + timedelta(minutes=2),
        )
        assert historical.created_mode == "shadow"
        new_activation = repository.set_runtime_context(
            mode="active", now=T0 + timedelta(minutes=3)
        )
        assert new_activation.active_epoch != first_activation.active_epoch
        assert repository.quarantine_legacy_external_tasks(
            now=T0 + timedelta(minutes=3), active_epoch=new_activation.active_epoch
        ) == 1
        assert repository.get(historical.task_id).status is AutomationTaskStatus.LEGACY_PENDING
    finally:
        connection.close()


def test_rollback_shadow_then_reactivate_has_predictable_boundary() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteAutomationTaskRepository(connection)
        epoch_one = repository.set_runtime_context(mode="active", now=T0)
        old = _task(
            repository,
            task_type="RECONCILE_ALARM_EVENT",
            key="RECONCILE_ALARM_EVENT:event-old-epoch",
            created_at=T0,
        )
        repository.set_runtime_context(mode="shadow", now=T0 + timedelta(minutes=1))
        assert repository.quarantine_legacy_external_tasks(now=T0 + timedelta(minutes=1)) == 1
        assert repository.get(old.task_id).status is AutomationTaskStatus.LEGACY_PENDING

        epoch_two = repository.set_runtime_context(mode="active", now=T0 + timedelta(minutes=2))
        fresh = _task(
            repository,
            task_type="RECONCILE_ALARM_EVENT",
            key="RECONCILE_ALARM_EVENT:event-fresh",
            created_at=T0 + timedelta(minutes=2),
        )
        assert epoch_two.active_epoch != epoch_one.active_epoch
        assert fresh.active_epoch == epoch_two.active_epoch
        claimed = repository.claim_due(now=T0 + timedelta(minutes=2))
        assert [task.task_id for task in claimed] == [fresh.task_id]
    finally:
        connection.close()


def test_task_migration_is_additive_and_preserves_legacy_rows() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE automation_tasks (
                id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                dedupe_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                claimed_at TEXT,
                lease_until TEXT,
                worker_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO automation_tasks (
                id, task_type, entity_type, entity_id, due_at, status,
                payload_json, dedupe_key, created_at, updated_at
            ) VALUES ('legacy-1', 'RECONCILE_ALARM_EVENT', 'EVENT', 'event-1',
                      ?, 'PENDING', '{}', 'legacy-key', ?, ?)
            """,
            (T0.isoformat(), T0.isoformat(), T0.isoformat()),
        )
        connection.commit()

        repository = SQLiteAutomationTaskRepository(connection)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(automation_tasks)")
        }
        assert {"created_mode", "active_epoch"} <= columns
        legacy = repository.get("legacy-1")
        assert legacy.payload == {}
        assert legacy.created_mode is None
        assert legacy.active_epoch is None
        assert repository.set_runtime_context(mode="active", now=T0 + timedelta(minutes=1))
        assert repository.quarantine_legacy_external_tasks(now=T0 + timedelta(minutes=1)) == 1
        assert repository.get("legacy-1").status is AutomationTaskStatus.LEGACY_PENDING
    finally:
        connection.close()
