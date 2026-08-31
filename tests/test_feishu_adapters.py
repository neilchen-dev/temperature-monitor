from __future__ import annotations

import unittest
from datetime import datetime, timezone

from application.operation_sync import (
    OperationAction,
    OperationObservation,
    OperationObservationService,
)
from integrations.feishu_operation import (
    FeishuOperationAdapter,
    FeishuOperationFieldMap,
)
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_records import FeishuBitableRecordSource
from integrations.feishu_standard import (
    FeishuStandardAdapter,
    FeishuStandardFieldMap,
)
from integrations.feishu_standard_config import (
    FEISHU_STANDARD_FIELD_MAP,
    FEISHU_STANDARD_TABLE_ID,
    build_feishu_standard_adapter,
)


class _Source:
    def __init__(self, records: tuple[FeishuRawRecord, ...]) -> None:
        self.records = records
        self.requested_table_id: str | None = None

    def read_records(self, table_id: str) -> tuple[FeishuRawRecord, ...]:
        self.requested_table_id = table_id
        return self.records


class FeishuStandardAdapterTests(unittest.TestCase):
    def test_real_standard_table_mapping_is_frozen_explicitly(self) -> None:
        self.assertEqual(FEISHU_STANDARD_TABLE_ID, "tbl4S6Q0VOYjK92t")
        self.assertEqual(FEISHU_STANDARD_FIELD_MAP.standard_id, "标准编号")
        self.assertEqual(FEISHU_STANDARD_FIELD_MAP.device_id, "适用设备")
        self.assertEqual(FEISHU_STANDARD_FIELD_MAP.temperature_min, "温度下限（°C）")
        self.assertEqual(FEISHU_STANDARD_FIELD_MAP.enabled, "是否启用")
        adapter = build_feishu_standard_adapter()
        self.assertEqual(adapter.table_id, FEISHU_STANDARD_TABLE_ID)
        self.assertIsInstance(adapter.source, FeishuBitableRecordSource)

    def test_real_source_shape_is_normalized_without_writes(self) -> None:
        source = FeishuBitableRecordSource(
            fetch_records=lambda table_id: (
                {
                    "record_id": "rec-real-shape",
                    "fields": {"标准编号": "ENV-001"},
                    "created_time": 1_756_376_400_000,
                    "last_modified_time": 1_756_376_401_000,
                },
            )
        )

        records = source.read_records("tbl-standard")

        self.assertEqual(records[0].record_id, "rec-real-shape")
        self.assertEqual(records[0].fields["标准编号"], "ENV-001")
        self.assertEqual(records[0].created_at, 1_756_376_400_000)
        self.assertEqual(records[0].updated_at, 1_756_376_401_000)

    def test_explicit_field_map_normalizes_without_feishu_write(self) -> None:
        created_at = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
        source = _Source(
            (
                FeishuRawRecord(
                    record_id="rec-standard-1",
                    fields={
                        "id": "ENV-PE-001",
                        "device": "TH-10",
                        "revision": "Rev.B",
                        "area": "PE仓库",
                        "operation": None,
                        "tmin": 10,
                        "tmax": 45,
                        "hmin": 30,
                        "hmax": 70,
                        "from": "2026-01-01T00:00:00+00:00",
                        "to": None,
                        "priority": 20,
                        "enabled": "启用",
                        "source": "NE-QMS-QP034",
                        "clause": "5.2.3",
                    },
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
        )
        adapter = FeishuStandardAdapter(
            source=source,
            table_id="future-standard-table-id",
            fields=FeishuStandardFieldMap(
                standard_id="id",
                revision="revision",
                area="area",
                device_id="device",
                operation_type="operation",
                temperature_min="tmin",
                temperature_max="tmax",
                humidity_min="hmin",
                humidity_max="hmax",
                effective_from="from",
                effective_to="to",
                priority="priority",
                enabled="enabled",
                source_document="source",
                clause="clause",
            ),
        )
        records = adapter.fetch_source_records()
        self.assertEqual(source.requested_table_id, "future-standard-table-id")
        self.assertEqual(records[0].source_record_id, "rec-standard-1")
        self.assertEqual(records[0].standard.standard_id, "ENV-PE-001")
        self.assertEqual(records[0].standard.device_id, "TH-10")
        self.assertEqual(records[0].standard.priority, 20)
        self.assertTrue(records[0].standard.enabled)


class FeishuOperationAdapterTests(unittest.TestCase):
    def test_operation_registration_is_normalized_and_work_order_is_optional(self) -> None:
        created_at = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
        adapter = FeishuOperationAdapter(
            source=_Source(
                (
                    FeishuRawRecord(
                        record_id="rec-operation-1",
                        fields={
                            "device": " th-03 ",
                            "area": "精密装配间",
                            "action": "开始作业",
                            "process": "未关联工艺文件（TH-03）",
                            "work_order": None,
                        },
                        created_at=created_at,
                    ),
                )
            ),
            table_id="operation-registration",
            fields=FeishuOperationFieldMap(
                device_id="device",
                area_id="area",
                action="action",
                operation_type="process",
                work_order="work_order",
            ),
        )
        observation = adapter.fetch_observations(observed_at=created_at)[0]
        self.assertEqual(observation.device_id, "TH-03")
        self.assertEqual(observation.action, OperationAction.START)
        self.assertIsNone(observation.work_order)
        self.assertEqual(observation.source_record_id, "rec-operation-1")

    def test_all_confirmed_operation_actions_are_supported(self) -> None:
        created_at = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
        adapter = FeishuOperationAdapter(
            source=_Source(()),
            table_id="operation-registration",
            fields=FeishuOperationFieldMap(
                device_id="device",
                area_id="area",
                action="action",
            ),
        )
        actions = []
        for index, raw_action in enumerate(("开始作业", "工艺切换", "结束作业")):
            observation = adapter.normalize_record(
                FeishuRawRecord(
                    record_id=f"rec-operation-{index}",
                    fields={"device": "TH-03", "area": "精密装配间", "action": raw_action},
                    created_at=created_at,
                ),
                observed_at=created_at,
            )
            actions.append(observation.action)
        self.assertEqual(
            actions,
            [OperationAction.START, OperationAction.SWITCH, OperationAction.END],
        )


class _OperationStore:
    def __init__(self, current: OperationObservation | None) -> None:
        self.current = current
        self.saved: list[OperationObservation] = []
        self.stale: list[OperationObservation] = []

    def get_current(self, device_id: str) -> OperationObservation | None:
        return self.current if self.current and self.current.device_id == device_id else None

    def save_current(self, observation: OperationObservation) -> None:
        self.current = observation
        self.saved.append(observation)

    def record_stale(self, observation: OperationObservation) -> None:
        self.stale.append(observation)


class OperationOrderingTests(unittest.TestCase):
    def test_old_record_is_audited_and_cannot_overwrite_new_state(self) -> None:
        newer = OperationObservation(
            device_id="TH-03",
            area_id="精密装配间",
            action=OperationAction.START,
            operation_type="工艺A",
            work_order=None,
            source_record_id="new",
            source_created_at=datetime(2026, 8, 28, 13, 1),
            observed_at=datetime(2026, 8, 28, 13, 2),
        )
        older = OperationObservation(
            device_id="TH-03",
            area_id="精密装配间",
            action=OperationAction.END,
            operation_type=None,
            work_order=None,
            source_record_id="old",
            source_created_at=datetime(2026, 8, 28, 13, 0),
            observed_at=datetime(2026, 8, 28, 13, 2),
        )
        store = _OperationStore(newer)
        result = OperationObservationService(store=store).apply(older)
        self.assertFalse(result.accepted)
        self.assertEqual(store.current, newer)
        self.assertEqual(store.stale, [older])
