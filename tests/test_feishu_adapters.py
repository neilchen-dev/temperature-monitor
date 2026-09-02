from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

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
from integrations.feishu_observation import (
    FeishuBitableObservationSource,
    FeishuObservationAdapter,
    FeishuObservationFieldMap,
    business_closed,
)
from integrations.feishu_standard import (
    FeishuStandardAdapter,
    FeishuStandardFieldMap,
    _parse_datetime,
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
    def test_naive_iso_datetime_uses_configured_business_timezone(self) -> None:
        parsed = _parse_datetime("2026-01-01T00:00:00")

        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))

    def test_iso_datetime_with_offset_stays_aware(self) -> None:
        parsed = _parse_datetime("2026-01-01T00:00:00+08:00")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))

    def test_z_datetime_is_parsed_as_utc(self) -> None:
        parsed = _parse_datetime("2026-01-01T00:00:00Z")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

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

    def test_invalid_registration_and_unrelated_device_are_ignored(self) -> None:
        created_at = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
        adapter = FeishuOperationAdapter(
            source=_Source(
                (
                    FeishuRawRecord(
                        record_id="invalid",
                        fields={
                            "device": "TH-03",
                            "area": "精密装配间",
                            "action": "开始作业",
                            "validity": "组合错误",
                        },
                        created_at=created_at,
                    ),
                    FeishuRawRecord(
                        record_id="unrelated",
                        fields={
                            "device": "TH-10",
                            "area": "PE仓库",
                            "action": "开始作业",
                            "validity": "有效",
                        },
                        created_at=created_at,
                    ),
                    FeishuRawRecord(
                        record_id="valid",
                        fields={
                            "device": "TH-03",
                            "area": "精密装配间",
                            "action": "开始作业",
                            "validity": "有效",
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
                validation="validity",
                allowed_device_ids=frozenset({"TH-03", "TH-04", "TH-05", "TH-07"}),
            ),
        )

        observations = adapter.fetch_observations(observed_at=created_at)

        self.assertEqual([item.source_record_id for item in observations], ["valid"])


class FeishuObservationAdapterTests(unittest.TestCase):
    def _observe_count(self, value, *, field_name="active_count"):
        class _ObservationSource:
            def read(self, device_id: str):
                return {
                    "alarm": "未触发",
                    "operation": "N/A",
                    "event": False,
                    field_name: value,
                }

        return FeishuObservationAdapter(
            source=_ObservationSource(),
            fields=FeishuObservationFieldMap(
                alarm_state="alarm",
                operation_state="operation",
                event_exists="event",
                active_event_count=(
                    field_name if field_name == "active_count" else None
                ),
                pending_closure_count=(
                    field_name if field_name == "pending_count" else None
                ),
            ),
        ).observe("TH-10")

    def test_integer_field_accepts_supported_feishu_shapes(self) -> None:
        supported = (
            (0, 0),
            (1, 1),
            ("0", 0),
            ("1", 1),
            (0.0, 0),
            (None, None),
            ("", None),
            ([0], 0),
            (["0"], 0),
            ({"value": 0}, 0),
            ([{"value": 0}], 0),
        )
        for raw_value, expected in supported:
            with self.subTest(raw_value=raw_value):
                observed = self._observe_count(raw_value)
                self.assertEqual(observed.active_event_count, expected)

    def test_integer_field_rejects_ambiguous_or_non_integral_shapes(self) -> None:
        for raw_value in (1.5, "abc", [0, 1], {"unexpected": 0}):
            with self.subTest(raw_value=raw_value):
                with self.assertRaisesRegex(
                    ValueError,
                    "Feishu observation field is not an integer: active_count",
                ):
                    self._observe_count(raw_value)

    def test_pending_closure_count_uses_same_integer_normalization(self) -> None:
        observed = self._observe_count(
            [{"value": "1"}], field_name="pending_count"
        )
        self.assertEqual(observed.pending_closure_count, 1)

    def test_raw_feishu_record_envelope_reaches_observation_adapter(self) -> None:
        """Exercise API record envelope -> record DTO -> composed observation."""
        timestamp_ms = 1_788_228_800_000

        def fetch_records(table_id: str):
            if table_id == "devices":
                return (
                    {
                        "record_id": "device-10",
                        "fields": {
                            "设备编号": [{"text": "TH-10"}],
                            "警报状态": [{"text": "未触发"}],
                            "当前作业状态": {"value": "N/A"},
                        },
                        "created_time": timestamp_ms,
                        "last_modified_time": timestamp_ms,
                    },
                )
            return (
                {
                    "record_id": "event-1",
                    "fields": {
                        "监测点": [{"text": "TH-10"}],
                        "处理状态": {"value": "处理中"},
                        "恢复时间": None,
                    },
                },
            )

        record_source = FeishuBitableRecordSource(fetch_records=fetch_records)
        observation_source = FeishuBitableObservationSource(
            source=record_source,
            device_table_id="devices",
            event_table_id="events",
        )
        observed = FeishuObservationAdapter(
            source=observation_source,
            fields=FeishuObservationFieldMap(
                alarm_state="警报状态",
                operation_state="当前作业状态",
                event_exists="__event_exists",
                active_event_count="__active_event_count",
                pending_closure_count="__pending_closure_count",
            ),
        ).observe("TH-10")

        self.assertEqual(observed.alarm_state, "NORMAL")
        self.assertEqual(observed.operation_state, "NOT_APPLICABLE")
        self.assertTrue(observed.event_exists)
        self.assertEqual(observed.active_event_count, 1)
        self.assertEqual(observed.pending_closure_count, 0)

    def test_business_closure_formula_excludes_closed_events(self) -> None:
        timestamp = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

        class _ObservationSource:
            def read_records(self, table_id: str):
                if table_id == "devices":
                    return (
                        FeishuRawRecord(
                            record_id="device-10",
                            fields={
                                "设备编号": "TH-10",
                                "警报状态": "未触发",
                                "当前作业状态": "N/A",
                                "当前判定状态": "正常",
                                "在线状态": "在线",
                            },
                            updated_at=timestamp,
                        ),
                    )
                return (
                    FeishuRawRecord(
                        record_id="closed-event",
                        fields={
                            "监测点": "TH-10",
                            "处理状态": "关闭",
                            "闭环状态": "已闭环",
                        },
                    ),
                    FeishuRawRecord(
                        record_id="open-event",
                        fields={
                            "监测点": "TH-10",
                            "处理状态": "待处理",
                            "闭环状态": "未关闭",
                        },
                    ),
                )

        source = FeishuBitableObservationSource(
            source=_ObservationSource(),
            device_table_id="devices",
            event_table_id="events",
        )
        observed = FeishuObservationAdapter(
            source=source,
            fields=FeishuObservationFieldMap(
                alarm_state="警报状态",
                operation_state="当前作业状态",
                overall_status="当前判定状态",
                event_exists="__event_exists",
                active_event_count="__active_event_count",
            ),
        ).observe("TH-10")

        self.assertTrue(observed.event_exists)
        self.assertEqual(observed.active_event_count, 1)

    def test_business_closed_uses_formula_only(self) -> None:
        self.assertFalse(business_closed({"闭环状态": "未关闭"}))
        self.assertTrue(business_closed({"闭环状态": "已闭环"}))
        self.assertFalse(
            business_closed(
                {
                    "闭环状态": "未关闭",
                    "处理状态": "关闭",
                    "恢复时间": 1_756_700_000_000,
                }
            )
        )

    def test_no_standard_observation_maps_to_unknown_overall(self) -> None:
        class _Source:
            def read(self, device_id: str):
                return {
                    "overall": "待工艺标准",
                    "operation": "作业中",
                    "event": False,
                }

        observed = FeishuObservationAdapter(
            source=_Source(),
            fields=FeishuObservationFieldMap(
                alarm_state="alarm",
                operation_state="operation",
                overall_status="overall",
                event_exists="event",
            ),
        ).observe("TH-03")

        self.assertEqual(observed.overall_status, "UNKNOWN")


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
