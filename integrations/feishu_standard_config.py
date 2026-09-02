"""Frozen wiring for the real read-only Feishu environment-standard table."""

from __future__ import annotations

from .feishu_standard import FeishuStandardAdapter, FeishuStandardFieldMap
from .feishu_records import FeishuBitableRecordSource


# This is a table identifier, not a credential.  Credentials remain in the
# runtime environment and are never committed to the repository.
FEISHU_STANDARD_TABLE_ID = "tbl4S6Q0VOYjK92t"

FEISHU_STANDARD_FIELD_MAP = FeishuStandardFieldMap(
    standard_id="标准编号",
    revision="版本",
    area="适用区域",
    device_id="适用设备",
    operation_type="适用作业类型",
    control_type="控制类型",
    temperature_min="温度下限（°C）",
    temperature_max="温度上限（°C）",
    humidity_min="湿度下限（%RH）",
    humidity_max="湿度上限（%RH）",
    effective_from="生效时间",
    effective_to="失效时间",
    priority="优先级",
    enabled="是否启用",
    source_document="来源文件",
    clause="条款",
)


def build_feishu_standard_adapter() -> FeishuStandardAdapter:
    """Build the read-only adapter with the frozen production mapping."""
    return FeishuStandardAdapter(
        source=FeishuBitableRecordSource(),
        table_id=FEISHU_STANDARD_TABLE_ID,
        fields=FEISHU_STANDARD_FIELD_MAP,
    )
