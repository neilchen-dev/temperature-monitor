"""Small source-record contracts shared by read-only Feishu adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable, Iterable, Mapping
from typing import Any


@dataclass(frozen=True)
class FeishuRawRecord:
    """A connector-neutral representation of one Feishu Base record.

    The domain still receives only this connector-neutral DTO.  The concrete
    HTTP source below supplies it from Feishu, while tests can inject records
    without importing Feishu SDK types into the domain.
    """

    record_id: str
    fields: Mapping[str, Any]
    created_at: datetime | str | int | float | None = None
    updated_at: datetime | str | int | float | None = None


class FeishuBitableRecordSource:
    """Read Base records through the existing Feishu HTTP service.

    ``fetch_records`` is injectable so adapter tests remain offline.  The
    default is imported lazily, keeping domain and fake-source imports free of
    an HTTP dependency until a real Feishu read is requested.
    """

    def __init__(
        self,
        *,
        fetch_records: Callable[[str], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._fetch_records = fetch_records

    def read_records(self, table_id: str) -> tuple[FeishuRawRecord, ...]:
        fetch_records = self._fetch_records
        if fetch_records is None:
            from services.feishu import list_bitable_records

            fetch_records = list_bitable_records

        records: list[FeishuRawRecord] = []
        for raw_record in fetch_records(table_id):
            if isinstance(raw_record, FeishuRawRecord):
                records.append(raw_record)
                continue
            record_id = str(raw_record.get("record_id", "")).strip()
            if not record_id:
                raise ValueError("Feishu Base record is missing record_id")
            fields = raw_record.get("fields", {})
            if not isinstance(fields, Mapping):
                raise ValueError(f"Feishu Base record fields are not an object: {record_id}")
            records.append(
                FeishuRawRecord(
                    record_id=record_id,
                    fields=fields,
                    created_at=raw_record.get("created_time"),
                    updated_at=raw_record.get("last_modified_time"),
                )
            )
        return tuple(records)
