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
    created_by: Mapping[str, Any] | None = None


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
                    created_by=(
                        raw_record.get("created_by")
                        if isinstance(raw_record.get("created_by"), Mapping)
                        else None
                    ),
                )
            )
        return tuple(records)

    def read_matching_records(
        self, table_id: str, *, field_name: str, value: str,
        field_names: list[str] | None = None,
        max_attempts: int | None = None, timeout: float | None = None,
    ) -> tuple[FeishuRawRecord, ...]:
        """Filter remotely while retaining the injected-source contract."""
        if self._fetch_records is not None:
            return self.read_records(table_id)
        from services.feishu import list_bitable_records

        def fetch(table):
            return list_bitable_records(
                table, field_names=field_names,
                record_filter={"conjunction": "and", "conditions": [
                    {"field_name": field_name, "operator": "is", "value": [value]},
                ]},
                max_attempts=max_attempts, timeout=timeout,
            )
        return FeishuBitableRecordSource(fetch_records=fetch).read_records(table_id)

    def read_device_events(
        self, table_id: str, device_id: str, *, device_field: str,
        max_attempts: int | None = None, timeout: float | None = None,
    ) -> tuple[FeishuRawRecord, ...]:
        """Read only the device and closure fields used by prewarning checks."""
        return self.read_matching_records(
            table_id, field_name=device_field, value=device_id,
            field_names=[device_field, "闭环状态"],
            max_attempts=max_attempts, timeout=timeout,
        )

    def read_field_names(self, table_id: str) -> tuple[str, ...]:
        """Read table metadata for lightweight integration schema checks."""
        from services.feishu import list_bitable_field_names

        return list_bitable_field_names(table_id)
