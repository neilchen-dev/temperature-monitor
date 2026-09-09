"""Explicit Feishu IM notification effects for alarm lifecycle actions.

The event writer and this writer are deliberately separate.  A notification
never creates or updates a Bitable event; it only consumes the already-bound
local event and records its own durable external-effect marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import logging
from typing import Any, Protocol

import config
from domain.models import AlarmActionType
from integrations.feishu_records import FeishuRawRecord
from repositories.environment_events import SQLiteEnvironmentEventRepository
from services.feishu import FeishuIMError


logger = logging.getLogger("temperature_monitor")


class FeishuNotificationError(RuntimeError):
    """A notification failed with an explicit retry classification."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool,
        outcome_unknown: bool = False,
        recipient: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.recipient = recipient
        super().__init__(message)


class FeishuMessageSender(Protocol):
    def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Send one text message and return the official response."""


class FeishuNotificationWriter:
    """Write one independently auditable alarm/recovery message."""

    def __init__(
        self,
        *,
        sender: FeishuMessageSender,
        source: Any,
        event_table_id: str,
        device_table_id: str,
        event_repository: SQLiteEnvironmentEventRepository,
        event_owner_field: str = "责任人",
        device_owner_field: str = "默认异常责任人",
        event_table_url: str = "",
        attempt_timeout: float | None = None,
    ) -> None:
        self.sender = sender
        self.source = source
        self.event_table_id = str(event_table_id).strip()
        self.device_table_id = str(device_table_id).strip()
        self.event_repository = event_repository
        self.event_owner_field = event_owner_field
        self.device_owner_field = device_owner_field
        self.event_table_url = event_table_url.strip()
        self.attempt_timeout = attempt_timeout
        if not self.event_table_id:
            raise ValueError("event_table_id cannot be empty")
        if not self.device_table_id:
            raise ValueError("device_table_id cannot be empty")

    def handle_notification_action(
        self,
        action: Any,
        context: Mapping[str, Any],
    ) -> None:
        action_type = _action_type(action)
        if action_type not in {
            AlarmActionType.NOTIFY_ALARM.value,
            AlarmActionType.NOTIFY_RECOVERY.value,
        }:
            raise FeishuNotificationError(
                f"unsupported notification action: {action_type}",
                error_code="invalid_action_type",
                retryable=False,
            )
        event_id = str(
            getattr(action, "alarm_id", None)
            or context.get("event_id")
            or ""
        ).strip()
        if not event_id:
            self._set_context(context, result="FAILED", error_code="event_id_missing")
            raise FeishuNotificationError(
                "notification requires event_id",
                error_code="event_id_missing",
                retryable=False,
            )
        event = self.event_repository.get(event_id)
        if event is None:
            self._set_context(context, result="FAILED", error_code="event_missing")
            raise FeishuNotificationError(
                f"local environment event is missing: {event_id}",
                error_code="event_missing",
                retryable=False,
            )
        record_id = _text(event.payload.get("feishu_record_id"))
        if not record_id:
            self._set_context(
                context,
                result="FAILED",
                error_code="event_projection_pending",
                retryable=True,
            )
            raise FeishuNotificationError(
                "event projection is not bound; notification will retry after CREATE",
                error_code="event_projection_pending",
                retryable=True,
            )

        effect_key = _notification_effect_key(action_type, event_id, context)
        marker = self.event_repository.get_external_effect(event_id, effect_key)
        if marker is not None and marker.get("status") == "SUCCEEDED":
            self._set_context(
                context,
                result="ALREADY_SENT",
                recipient=_text(marker.get("recipient")),
                message_id=_text(marker.get("message_id")),
                sent_at=_text(marker.get("sent_at") or marker.get("completed_at")),
                error_code=None,
                retryable=False,
            )
            return

        try:
            recipient, receive_id_type = self._resolve_recipient(
                event=event,
                record_id=record_id,
            )
        except FeishuNotificationError as exc:
            self._mark_failed(
                event_id,
                effect_key,
                _attempt_time(context),
                exc,
                recipient=exc.recipient or "",
            )
            self._set_context(
                context,
                result="FAILED",
                error_code=exc.error_code,
                recipient=exc.recipient,
                retryable=exc.retryable,
                outcome_unknown=exc.outcome_unknown,
            )
            raise
        requested_at = _attempt_time(context)
        self.event_repository.mark_external_effect_pending(
            event_id,
            effect_key=effect_key,
            action_type=action_type,
            requested_at=requested_at,
            metadata={
                "dedupe_key": effect_key,
                "recipient": recipient,
                "receive_id_type": receive_id_type,
            },
        )
        message = self._message(
            action_type=action_type,
            event_id=event_id,
            record_id=record_id,
            context={**dict(context), "event_payload": dict(event.payload)},
        )
        try:
            response = self.sender.send_text(
                recipient,
                receive_id_type,
                message,
                idempotency_key=effect_key,
                max_attempts=1,
                timeout=self.attempt_timeout,
            )
            message_id = _message_id(response)
            if not message_id:
                raise FeishuNotificationError(
                    "Feishu success response is missing message_id",
                    error_code="message_id_missing",
                    retryable=False,
                    outcome_unknown=True,
                    recipient=recipient,
                )
        except FeishuNotificationError as exc:
            self._mark_failed(
                event_id,
                effect_key,
                requested_at,
                exc,
                recipient=recipient,
            )
            self._set_context(
                context,
                result="FAILED",
                error_code=exc.error_code,
                recipient=recipient,
                retryable=exc.retryable,
                outcome_unknown=exc.outcome_unknown,
            )
            raise
        except FeishuIMError as exc:
            wrapped = FeishuNotificationError(
                str(exc),
                error_code=exc.error_code,
                retryable=exc.retryable,
                outcome_unknown=exc.outcome_unknown,
                recipient=recipient,
            )
            self._mark_failed(
                event_id,
                effect_key,
                requested_at,
                wrapped,
                recipient=recipient,
            )
            self._set_context(
                context,
                result="FAILED",
                error_code=wrapped.error_code,
                recipient=recipient,
                retryable=wrapped.retryable,
                outcome_unknown=wrapped.outcome_unknown,
            )
            raise wrapped from exc
        except Exception as exc:  # noqa: BLE001 - injected transport adapter
            wrapped = FeishuNotificationError(
                str(exc),
                error_code="notification_error",
                retryable=True,
                outcome_unknown=True,
                recipient=recipient,
            )
            self._mark_failed(
                event_id,
                effect_key,
                requested_at,
                wrapped,
                recipient=recipient,
            )
            self._set_context(
                context,
                result="FAILED",
                error_code=wrapped.error_code,
                recipient=recipient,
                retryable=True,
                outcome_unknown=True,
            )
            raise wrapped from exc

        sent_at = datetime.now(timezone.utc)
        self.event_repository.mark_external_effect_succeeded(
            event_id,
            effect_key=effect_key,
            completed_at=sent_at,
            metadata={
                "dedupe_key": effect_key,
                "recipient": recipient,
                "receive_id_type": receive_id_type,
                "message_id": message_id,
                "sent_at": sent_at.isoformat(),
                "result": "SUCCEEDED",
            },
        )
        self._set_context(
            context,
            result="SUCCEEDED",
            error_code=None,
            recipient=recipient,
            message_id=message_id,
            sent_at=sent_at.isoformat(),
            retryable=False,
        )
        logger.info(
            "feishu_notification_sent | action_type=%s event_id=%s recipient=%s "
            "message_id=%s dedupe_key=%s",
            action_type,
            event_id,
            recipient,
            message_id,
            effect_key,
        )

    def _resolve_recipient(self, *, event: Any, record_id: str) -> tuple[str, str]:
        event_record = self._read_record(self.event_table_id, record_id)
        candidate = (
            event_record.fields.get(self.event_owner_field)
            if event_record is not None
            else None
        )
        parsed = _parse_recipient(
            candidate,
            default_type=getattr(config, "FEISHU_NOTIFY_RECEIVE_ID_TYPE", "open_id"),
        )
        if parsed is None:
            device_record = self._read_device_record(event.device_id)
            candidate = (
                device_record.fields.get(self.device_owner_field)
                if device_record is not None
                else None
            )
            parsed = _parse_recipient(
                candidate,
                default_type=getattr(config, "FEISHU_NOTIFY_RECEIVE_ID_TYPE", "open_id"),
            )
        if parsed is not None:
            return parsed

        fallback = _text(getattr(config, "FEISHU_ALARM_CHAT_ID", ""))
        if fallback:
            return fallback, "chat_id"
        raise FeishuNotificationError(
            "no Feishu recipient could be resolved from owner or fallback chat",
            error_code="recipient_unresolved",
            retryable=False,
        )

    def _read_record(self, table_id: str, record_id: str) -> FeishuRawRecord | None:
        try:
            for record in self.source.read_records(table_id):
                if record.record_id == record_id:
                    return record
        except Exception as exc:  # noqa: BLE001 - source failure can recover later
            raise FeishuNotificationError(
                f"unable to resolve Feishu recipient record: {exc}",
                error_code="recipient_lookup_failed",
                retryable=True,
                outcome_unknown=False,
            ) from exc
        return None

    def _read_device_record(self, device_id: str) -> FeishuRawRecord | None:
        normalized = str(device_id).strip().upper()
        try:
            for record in self.source.read_records(self.device_table_id):
                value = record.fields.get(getattr(config, "DEVICE_ID_FIELD", "设备编号"))
                if _text(value).upper() == normalized:
                    return record
        except Exception as exc:  # noqa: BLE001 - source failure can recover later
            raise FeishuNotificationError(
                f"unable to resolve Feishu device owner: {exc}",
                error_code="recipient_lookup_failed",
                retryable=True,
                outcome_unknown=False,
            ) from exc
        return None

    def _message(
        self,
        *,
        action_type: str,
        event_id: str,
        record_id: str,
        context: Mapping[str, Any],
    ) -> str:
        sample = _mapping(context.get("sample"))
        result = _mapping(context.get("python_monitor_result"))
        transition = _mapping(context.get("python_alarm_transition"))
        operation = _mapping(context.get("operation_state"))
        standard = _mapping(context.get("standard"))
        event_payload = _mapping(context.get("event_payload"))
        area = _text(operation.get("area_id")) or _text(context.get("area")) or "未提供"
        if action_type == AlarmActionType.NOTIFY_RECOVERY.value:
            recovered_at = (
                _text(context.get("recovered_at"))
                or _text(context.get("created_at"))
                or _text(context.get("sample_time"))
                or "未提供"
            )
            started = _parse_time(
                transition.get("violation_started_at")
                or context.get("violation_started_at")
            )
            recovered = _parse_time(recovered_at)
            duration = _duration_text(started, recovered)
            peak_temperature = event_payload.get(
                "peak_temperature", sample.get("peak_temperature", sample.get("temperature"))
            )
            peak_humidity = event_payload.get(
                "peak_humidity", sample.get("peak_humidity", sample.get("humidity"))
            )
            return "\n".join(
                (
                    "[温湿度异常恢复]",
                    f"设备 ID：{_text(context.get('device_id')) or _text(sample.get('device_id'))}",
                    f"区域：{area}",
                    f"恢复时间：{recovered_at}",
                    f"峰值温度/湿度：{_number_text(peak_temperature)} / {_number_text(peak_humidity)}",
                    f"事件 ID：{event_id}",
                    f"持续时间：{duration}",
                )
            )

        reasons = result.get("reasons")
        if isinstance(reasons, (list, tuple)):
            exceeded = "、".join(_text(item) for item in reasons if _text(item)) or "见判定状态"
        else:
            exceeded = _text(reasons) or (
                f"温度={_text(result.get('temperature_status'))} "
                f"湿度={_text(result.get('humidity_status'))}"
            )
        standard_range = (
            f"温度 {_number_text(standard.get('temperature_min'))}~{_number_text(standard.get('temperature_max'))}；"
            f"湿度 {_number_text(standard.get('humidity_min'))}~{_number_text(standard.get('humidity_max'))}"
        )
        start_time = (
            _text(transition.get("violation_started_at"))
            or _text(transition.get("alarm_started_at"))
            or _text(context.get("sample_time"))
            or "未提供"
        )
        lines = (
            "[温湿度异常告警]",
            f"设备 ID：{_text(context.get('device_id')) or _text(sample.get('device_id'))}",
            f"区域：{area}",
            f"当前温度/湿度：{_number_text(sample.get('temperature'))} / {_number_text(sample.get('humidity'))}",
            f"超限项：{exceeded}",
            f"标准范围：{standard_range}",
            f"异常开始时间：{start_time}",
            f"事件 ID：{event_id}",
            f"标准：{_text(standard.get('standard_id')) or _text(result.get('standard_id')) or '未提供'} "
            f"revision={_text(standard.get('revision')) or _text(result.get('standard_revision')) or '未提供'} "
            f"source={_text(standard.get('standard_source')) or _text(result.get('standard_source')) or '未提供'}",
        )
        link = self._event_link(record_id)
        return "\n".join((*lines, f"异常事件记录：{link}") if link else lines)

    def _event_link(self, record_id: str) -> str | None:
        if not self.event_table_url:
            return None
        try:
            return self.event_table_url.format(
                record_id=record_id,
                table_id=self.event_table_id,
                event_id=record_id,
            )
        except (KeyError, ValueError):
            return self.event_table_url

    def _mark_failed(
        self,
        event_id: str,
        effect_key: str,
        at: datetime,
        error: FeishuNotificationError,
        *,
        recipient: str | None,
    ) -> None:
        status = "UNKNOWN" if error.outcome_unknown else "FAILED"
        self.event_repository.mark_external_effect_failed(
            event_id,
            effect_key=effect_key,
            failed_at=at,
            error=str(error),
            status=status,
            metadata={
                "dedupe_key": effect_key,
                "recipient": recipient,
                "error_code": error.error_code,
                "retryable": error.retryable,
            },
        )

    @staticmethod
    def _set_context(context: Mapping[str, Any], **values: Any) -> None:
        if isinstance(context, dict):
            context.update(values)


def _action_type(action: Any) -> str:
    return str(getattr(getattr(action, "action_type", None), "value", "")).strip()


def _notification_effect_key(
    action_type: str,
    event_id: str,
    context: Mapping[str, Any],
) -> str:
    if action_type == AlarmActionType.NOTIFY_ALARM.value:
        return f"NOTIFY_ALARM:{event_id}"
    transition = _mapping(context.get("python_alarm_transition"))
    recovery_started = (
        _text(context.get("recovery_started_at"))
        or _text(transition.get("recovery_started_at"))
        or _text(context.get("recovered_at"))
        or _text(context.get("created_at"))
    )
    if not recovery_started:
        raise FeishuNotificationError(
            "recovery notification is missing recovery_started_at",
            error_code="recovery_started_at_missing",
            retryable=False,
        )
    return f"NOTIFY_RECOVERY:{event_id}:{recovery_started}"


def _parse_recipient(
    value: Any,
    *,
    default_type: str = "open_id",
) -> tuple[str, str] | None:
    """Accept only an explicit Feishu id/email, never a display name."""
    if isinstance(value, list):
        for item in value:
            parsed = _parse_recipient(item, default_type=default_type)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, Mapping):
        nested = value.get("value")
        if nested is not None:
            parsed = _parse_recipient(nested, default_type=default_type)
            if parsed is not None:
                return parsed
        for key, receive_type in (
            ("open_id", "open_id"),
            ("user_id", "user_id"),
            ("union_id", "union_id"),
            ("email", "email"),
        ):
            candidate = _text(value.get(key))
            if candidate and _valid_recipient(candidate, receive_type):
                return candidate, receive_type
        candidate = _text(value.get("id"))
        if candidate:
            if _valid_recipient(candidate, default_type):
                return candidate, default_type
            inferred = _infer_receive_type(candidate)
            if inferred is not None:
                return candidate, inferred
        return None
    candidate = _text(value)
    if not candidate:
        return None
    receive_type = (
        default_type
        if _valid_recipient(candidate, default_type)
        else _infer_receive_type(candidate)
    )
    return (candidate, receive_type) if receive_type is not None else None


def _infer_receive_type(value: str) -> str | None:
    if "@" in value and " " not in value:
        return "email"
    if value.startswith("ou_"):
        return "open_id"
    if value.startswith("on_"):
        return "union_id"
    return None


def _valid_recipient(value: str, receive_type: str) -> bool:
    if receive_type == "email":
        return "@" in value and " " not in value
    if receive_type in {"open_id", "user_id"}:
        return value.startswith("ou_")
    if receive_type == "union_id":
        return value.startswith("on_")
    if receive_type == "chat_id":
        return value.startswith("oc_")
    return False


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        if "value" in value:
            return _text(value["value"])
        if "text" in value:
            return _text(value["text"])
        if "name" in value:
            return _text(value["name"])
        return ""
    if isinstance(value, list):
        return " ".join(item for item in (_text(part) for part in value) if item)
    return str(value).strip() if value is not None else ""


def _number_text(value: Any) -> str:
    return _text(value) if value is not None else "未提供"


def _message_id(response: Mapping[str, Any]) -> str | None:
    data = response.get("data")
    if isinstance(data, Mapping):
        value = data.get("message_id")
        if value:
            return _text(value)
    return _text(response.get("message_id")) or None


def _attempt_time(context: Mapping[str, Any]) -> datetime:
    return (
        _parse_time(context.get("notification_attempted_at"))
        or datetime.now(timezone.utc)
    )


def _parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _duration_text(started: datetime | None, ended: datetime | None) -> str:
    if started is None or ended is None:
        return "未提供"
    seconds = max(0, int((ended - started).total_seconds()))
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分{remainder}秒"
    return f"{minutes}分{remainder}秒"
