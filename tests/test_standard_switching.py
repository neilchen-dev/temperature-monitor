"""标准切换中的报警状态机行为测试（P2）。

场景：连续超限 / 报警 / 恢复过程中标准发生切换（同步刷新标准表）：
- PENDING 中切宽 → 回 NORMAL，VERIFY 任务取消
- ALARM 中切宽 → 进入 RECOVERY，本地事件写恢复时间并最终关闭
- RECOVERY 中切回窄 → 重新 VIOLATION → 直接回 ALARM（复用同一事件）
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from domain.alarm_state_machine import AlarmStateMachine
from domain.models import DeviceContext, EnvironmentStandard, MonitorSample
from domain.standard_resolver import select_standard
from application.action_executor import ActionExecutor
from application.actions import ApplicationActionMapper
from application.monitor_service import MonitorApplicationService
from repositories.automation_tasks import SQLiteAutomationTaskRepository
from repositories.environment_events import SQLiteEnvironmentEventRepository
from repositories.runtime_state import (
    SQLiteAlarmStateRepository,
    SQLiteLatestSampleRepository,
)


TZ = ZoneInfo("Asia/Shanghai")
AREA = "测试区"


def _standard(temp_max: float) -> EnvironmentStandard:
    return EnvironmentStandard(
        standard_id="ENV-TEST",
        revision="Rev.A",
        area=AREA,
        operation_type=None,
        temperature_min=20.0,
        temperature_max=temp_max,
        humidity_min=40.0,
        humidity_max=60.0,
        effective_from=datetime(2026, 1, 1, tzinfo=TZ),
        effective_to=None,
        source_document="SOP-TEST",
        clause="1.0",
    )


class _SwitchableResolver:
    """测试用：可在运行中切换生效标准集合。"""

    def __init__(self) -> None:
        self.standards: list[EnvironmentStandard] = []

    def resolve(self, *, area_id, operation_type, timestamp, device_id=None):
        return select_standard(
            self.standards,
            area_id=area_id,
            operation_type=operation_type,
            timestamp=timestamp,
            device_id=device_id,
        )


class _StaticOperationState:
    def __init__(self, state) -> None:
        self._state = state

    def get(self, device_id: str):
        return self._state


class StandardSwitchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=TZ)
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.connection.close)
        self.resolver = _SwitchableResolver()
        self.resolver.standards = [_standard(26.0)]  # 窄标准
        from domain.models import OperationState, OperationStatus

        self.service = MonitorApplicationService(
            operation_state_provider=_StaticOperationState(
                OperationState(
                    area_id=AREA,
                    state=OperationStatus.NOT_APPLICABLE,
                    operation_type=None,
                    work_order=None,
                    started_at=None,
                    ended_at=None,
                )
            ),
            standard_resolver=self.resolver,
            alarm_state_repository=SQLiteAlarmStateRepository(self.connection),
            alarm_state_machine=AlarmStateMachine(),
            action_mapper=ApplicationActionMapper(),
            action_executor=ActionExecutor(mode="shadow"),
            now_provider=lambda: self.now,
            task_repository=SQLiteAutomationTaskRepository(self.connection),
            event_repository=SQLiteEnvironmentEventRepository(self.connection),
            latest_sample_repository=SQLiteLatestSampleRepository(self.connection),
        )
        self.device = DeviceContext(
            device_id="TH-T", area=AREA, control_type="ALL_DAY"
        )

    def _handle(self, temp: float):
        return self.service.handle_sample(
            device=self.device,
            sample=MonitorSample("TH-T", self.now, temp, 50.0, online_status="online"),
            now=self.now,
        )

    def _advance(self, minutes: float) -> None:
        self.now = self.now + timedelta(minutes=minutes)

    def _state(self) -> str:
        row = self.connection.execute(
            "SELECT state FROM alarm_states WHERE device_id = 'TH-T'"
        ).fetchone()
        return row[0] if row else "NORMAL"

    def _open_event_ids(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT event_id FROM environment_events WHERE status <> 'CLOSED'"
        ).fetchall()
        return [row[0] for row in rows]

    def test_pending_switch_to_wider_standard_recovers(self) -> None:
        self._handle(24.0)  # baseline NORMAL
        self._handle(28.0)  # 超限 → PENDING
        self.assertEqual(self._state(), "PENDING")

        # PENDING 中标准放宽到 30：28 变为正常 → 取消验证并回 NORMAL。
        self.resolver.standards = [_standard(30.0)]
        result = self._handle(28.0)
        self.assertEqual(result.transition.next.state.value, "NORMAL")
        self.assertEqual(self._state(), "NORMAL")
        cancelled = self.connection.execute(
            "SELECT status FROM automation_tasks WHERE task_type = 'VERIFY_ALARM'"
        ).fetchone()
        self.assertEqual(cancelled["status"], "CANCELLED")

    def test_alarm_switch_to_wider_standard_recovers_and_closes_event(self) -> None:
        self._handle(24.0)
        self._handle(28.0)
        self._advance(5)
        self._handle(28.0)
        self.assertEqual(self._state(), "ALARM")
        self.assertEqual(len(self._open_event_ids()), 1)

        # ALARM 中标准放宽：28 变为正常 → 进入 RECOVERY。
        self.resolver.standards = [_standard(30.0)]
        result = self._handle(28.0)
        self.assertEqual(result.transition.next.state.value, "RECOVERY")

        # 60 秒恢复确认后关闭本地事件并回 NORMAL。
        self._advance(1)
        result = self._handle(28.0)
        self.assertEqual(result.transition.next.state.value, "NORMAL")
        self.assertEqual(self._state(), "NORMAL")
        self.assertEqual(self._open_event_ids(), [])

    def test_recovery_switch_back_to_narrow_returns_to_alarm(self) -> None:
        self._handle(24.0)
        self._handle(28.0)
        self._advance(5)
        self._handle(28.0)
        self.assertEqual(self._state(), "ALARM")
        # 切宽 → 恢复窗口。
        self.resolver.standards = [_standard(30.0)]
        self._handle(28.0)
        self.assertEqual(self._state(), "RECOVERY")

        # RECOVERY 中切回窄标准：28 重新超限 → 直接回 ALARM，复用同一事件。
        self.resolver.standards = [_standard(26.0)]
        result = self._handle(28.0)
        self.assertEqual(result.transition.next.state.value, "ALARM")
        self.assertEqual(self._state(), "ALARM")
        self.assertEqual(len(self._open_event_ids()), 1)

    def test_standard_swap_keeps_single_active_event(self) -> None:
        """多次标准切换 + 超限/恢复交替，本地始终至多一条打开事件。"""
        self._handle(24.0)
        self._handle(28.0)
        self._advance(5)
        self._handle(28.0)
        self.resolver.standards = [_standard(30.0)]
        self._handle(28.0)  # RECOVERY
        self.resolver.standards = [_standard(26.0)]
        self._handle(28.0)  # back to ALARM
        self._advance(5)
        self._handle(28.0)
        self._handle(24.0)  # RECOVERY
        self._advance(1)
        self._handle(24.0)  # NORMAL
        self.assertEqual(self._state(), "NORMAL")
        self.assertEqual(self._open_event_ids(), [])
        total = self.connection.execute(
            "SELECT COUNT(*) FROM environment_events"
        ).fetchone()[0]
        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()
