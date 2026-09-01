"""External-system adapters."""

from .feishu_observation import (
    FeishuBitableObservationSource,
    FeishuObservationAdapter,
    FeishuObservationFieldMap,
    FeishuObservationTableFieldMap,
    FeishuObservationSource,
)
from .feishu_operation import (
    FeishuOperationAdapter,
    FeishuOperationFieldMap,
    FeishuOperationSource,
    OperationAction,
    OperationObservation,
)
from domain.operation import is_newer_operation
from .feishu_records import FeishuBitableRecordSource, FeishuRawRecord
from .feishu_standard import (
    FeishuStandardAdapter,
    FeishuStandardFieldMap,
    FeishuStandardSource,
    StandardSourceRecord,
)
from .feishu_writers import (
    FeishuBitableRecordWriter,
    FeishuEnvironmentEventWriter,
    FeishuEventWriteFieldMap,
    FeishuOperationRecordWriter,
    FeishuOperationWriteFieldMap,
    FeishuInspectionRecordWriter,
    FeishuInspectionWriteFieldMap,
    FeishuRecordWriter,
    FeishuWriteError,
)

__all__ = [
    "FeishuObservationAdapter",
    "FeishuObservationFieldMap",
    "FeishuObservationTableFieldMap",
    "FeishuBitableObservationSource",
    "FeishuObservationSource",
    "FeishuOperationAdapter",
    "FeishuOperationFieldMap",
    "FeishuOperationSource",
    "OperationAction",
    "OperationObservation",
    "is_newer_operation",
    "FeishuRawRecord",
    "FeishuBitableRecordSource",
    "FeishuStandardAdapter",
    "FeishuStandardFieldMap",
    "FeishuStandardSource",
    "StandardSourceRecord",
    "FeishuBitableRecordWriter",
    "FeishuEnvironmentEventWriter",
    "FeishuEventWriteFieldMap",
    "FeishuOperationRecordWriter",
    "FeishuOperationWriteFieldMap",
    "FeishuInspectionRecordWriter",
    "FeishuInspectionWriteFieldMap",
    "FeishuRecordWriter",
    "FeishuWriteError",
]
