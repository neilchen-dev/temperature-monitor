"""Feishu projection state machine for the /temperature reliability split.

Production invariant this module enforces: a validated sample that reached
the server is durably persisted locally *independently* of Feishu. Feishu
is an external projection; when it is unavailable the local sample stays,
the failure is recorded as durable state, a bounded retry is scheduled via
the existing ``automation_tasks`` scheduler, and the Runtime/Shadow
dispatch is deferred until the projection actually succeeds (so a frozen
Feishu table can never produce guaranteed-wrong SHADOW_COMPARE results).

Dispatch ownership (SINGLE OWNER — 2026-09-02 production deadlock fix):

The Shadow scheduler thread is the **only** caller of
``dispatch_projected_sample`` (via ``recover_pending_dispatches`` on every
tick). HTTP threads never dispatch: they only persist, project, and
advance the projected watermark. Previously the /temperature success path
dispatched inline, which acquired ``_dispatch_lock`` and then (through the
sample listener) ``ShadowRuntime._execution_lock``, while the scheduler
thread held ``_execution_lock`` and reached for ``_dispatch_lock`` inside
``recover_pending_dispatches`` / the FEISHU_PROJECTION handler — a classic
AB-BA deadlock (production: scheduler stalled, SYNC_OPERATIONS stuck
PENDING while Waitress threads kept projecting).

Lock-order invariant: HTTP paths acquire **no** runtime locks at all. The
scheduler thread acquires ``_execution_lock`` (RLock, same-thread
reentrant) and nothing else; ``_dispatch_lock`` was removed entirely, so
no two locks can ever be acquired in opposite orders. Cost: dispatch may
lag projection by at most one scheduler poll interval.

State machine per device (``sample_projection_state``):

- ``ok``      — latest local sample is projected; dispatch is up to date.
- ``pending`` — projection failed; scheduler retries with exponential
                backoff (``FEISHU_PROJECTION_MAX_RETRIES`` attempts).
- ``failed``  — retries exhausted. Visible for inspection; the next
                successful inline projection on /temperature recovers it.

Watermarks (all monotonic, targeted SQL updates — never full-row replaces):

- ``last_sample_time_ms``           — latest durably persisted sample.
- ``last_projected_sample_time_ms`` — latest sample whose Feishu write
  succeeded. Advanced *before* status flips to ok.
- ``last_dispatched_sample_time_ms`` — latest sample handed to the
  Runtime/Shadow pipeline.

Crash-consistency invariant (recovery scan, every scheduler tick):

    last_projected > last_dispatched  ⇒  unfinished dispatch work,
    regardless of projection_status.

Delivery semantics for dispatch are **at-least-once**, not exactly-once:
a crash between the listener call and the watermark write can re-deliver
the same sample once. This is safe because every downstream projection
dedupes (SHADOW_COMPARE by device+sample_time, VERIFY_ALARM dedupe keys,
event keys, latest-sample upsert) — re-delivery never duplicates business
effects. A transactional outbox (dispatch + watermark in one SQLite
transaction spanning the listener call) was rejected because listeners run
in-process synchronously and cannot join the mirror-DB transaction.

Scheduler-blocking contract: the scheduler thread is serial, so the
FEISHU_PROJECTION handler performs SINGLE bounded network attempts
(attempts=1, ``FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS`` each; worst
handler ≈ token + resolve + update ≈ 3 × timeout, typically ≤ 2 ×) and
returns failure immediately — the retry cadence is owned entirely by
``automation_tasks`` + exponential backoff, with per-device task staggering
so 11 devices cannot starve SHADOW_COMPARE / SYNC tasks.

Request-level dedupe boundary (weak idempotency, documented): HA payloads
carry no source timestamp, so ``persist_sample`` collapses only identical
content within ``TEMPERATURE_DEDUPE_WINDOW_MS``. Two *genuine* readings
with identical values inside the window would be merged into one sample —
accepted trade-off; upgrade path is a source-provided
``sample_time_ms``/``source_event_id`` in the HA payload for strong
idempotency (not implemented in this round).

All writes go through the ``services.db`` mirror connection (best-effort,
never raise); when SQLite is disabled every helper degrades to a no-op and
the caller falls back to legacy semantics.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import requests

import config
from domain.models import MonitorSample
from services import db, devices
from services.feishu import resolve_record_id, update_feishu_fields


logger = logging.getLogger("temperature_monitor")

PROJECTION_OK = "ok"
PROJECTION_PENDING = "pending"
PROJECTION_FAILED = "failed"

# Dispatch ownership contract (see module docstring): only the scheduler
# thread calls dispatch_projected_sample, via recover_pending_dispatches.
# There is deliberately NO process lock here — the old _dispatch_lock was
# half of the 2026-09-02 AB-BA deadlock. Cross-thread/crash idempotency is
# guaranteed by the durable watermark (last_dispatched_sample_time_ms) plus
# at-least-once delivery with downstream dedupe, not by locking.

# 同 tick 到期的 FEISHU_PROJECTION 任务按设备错峰的间隔（秒）。Scheduler
# 串行执行：错峰 + 1s poll ⇒ 每 tick 至多 1-2 个有界投影尝试（各 ≤
# FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS），其他任务类型不会被饿死。
_TASK_STAGGER_SECONDS = 2.0

# Truncation for persisted error strings — they end up in SQLite/logs/API.
_MAX_ERROR_LENGTH = 300


def _now_datetime(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now().astimezone()


def _now_iso(now: datetime | None = None) -> str:
    return _now_datetime(now).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _truncate_error(error: Any) -> str:
    text = str(error) if error is not None else ""
    if len(text) > _MAX_ERROR_LENGTH:
        text = text[: _MAX_ERROR_LENGTH - 3] + "..."
    return text


def build_projection_fields(
    temperature: Any,
    humidity: Any,
    *,
    offline: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Feishu realtime fields for one sample — shared by route and retry.

    Mirrors the legacy /temperature semantics exactly: offline only flips
    the online-status field (last temperature/humidity are preserved in the
    Feishu row); online writes temperature + humidity + status + timestamp.
    """
    if offline:
        return {"在线状态": "离线"}
    return {
        "当前温度": temperature,
        "当前湿度": humidity,
        "在线状态": "在线",
        "更新时间": int(now_ms if now_ms is not None else time.time() * 1000),
    }


# ---------------------------------------------------------------------------
# State helpers (all best-effort; no-op when the mirror is unavailable)
# ---------------------------------------------------------------------------


def _fetch_state(device: str) -> dict[str, Any]:
    state = db.fetch_projection_state(device)
    if state is None:
        return {
            "device": device,
            "last_sample_time_ms": None,
            "last_dispatched_sample_time_ms": None,
            "projection_status": PROJECTION_OK,
            "retry_count": 0,
            "last_error": None,
            "last_attempt_at": None,
            "projected_at": None,
        }
    return dict(state)


def note_sample_persisted(device: str, sample_time_ms: int) -> None:
    """Record that a local durable sample exists (does not touch status)."""
    db.note_projection_sample(device, int(sample_time_ms))


def mark_projection_success(
    device: str,
    sample_time_ms: int | None = None,
    now: datetime | None = None,
) -> None:
    """Feishu realtime projection succeeded for ``sample_time_ms``.

    Order is crash-consistency critical: the projected watermark advances
    FIRST (targeted monotonic update, committed before returning), then the
    status clears. A crash between them leaves ``pending`` with the
    watermark set — the retry handler re-projects (idempotent) and
    dispatches. A crash *after* this function but before dispatch leaves
    ``ok`` with ``projected > dispatched`` — ``recover_pending_dispatches``
    finishes it on the next scheduler tick.
    """
    if sample_time_ms is not None:
        db.mark_projection_projected(device, int(sample_time_ms))
    previous = _fetch_state(device)
    db.update_projection_status(
        device,
        projection_status=PROJECTION_OK,
        retry_count=0,
        last_error=None,
        last_attempt_at=previous.get("last_attempt_at"),
        projected_at=_now_iso(now),
    )
    logger.info(
        "feishu_projection_ok | device=%s | sample_time_ms=%s"
        " | retry_count_reset=1",
        device,
        sample_time_ms,
    )


def mark_projection_failure(
    device: str, error: Any, now: datetime | None = None
) -> None:
    """Inline projection failed on /temperature.

    Transitions ``ok -> pending`` (new failure episode, retry counter
    reset). An already-``pending`` device keeps its retry counter (the
    scheduler owns backoff growth); a terminal ``failed`` device stays
    ``failed`` — inline attempts must never re-arm an exhausted retry
    loop, or a long outage would produce an infinite retry storm.
    """
    state = _fetch_state(device)
    status = str(state.get("projection_status") or PROJECTION_OK)
    if status == PROJECTION_OK:
        status = PROJECTION_PENDING
        retry_count = 0
    else:
        retry_count = int(state.get("retry_count") or 0)
    error_text = _truncate_error(error)
    db.update_projection_status(
        device,
        projection_status=status,
        retry_count=retry_count,
        last_error=error_text,
        last_attempt_at=_now_iso(now),
        projected_at=state.get("projected_at"),
    )
    logger.warning(
        "feishu_projection_deferred | device=%s | projection_status=%s"
        " | retry_count=%s | error=%s",
        device,
        status,
        retry_count,
        error_text,
    )


def mark_retry_attempt_failed(
    device: str, error: Any, now: datetime | None = None
) -> bool:
    """A scheduled FEISHU_PROJECTION retry attempt failed.

    Returns True while retries remain (state stays ``pending``); False when
    the cap is exhausted and the state becomes terminal ``failed``.
    """
    state = _fetch_state(device)
    retry_count = int(state.get("retry_count") or 0) + 1
    error_text = _truncate_error(error)
    status = (
        PROJECTION_FAILED
        if retry_count >= config.FEISHU_PROJECTION_MAX_RETRIES
        else PROJECTION_PENDING
    )
    db.update_projection_status(
        device,
        projection_status=status,
        retry_count=retry_count,
        last_error=error_text,
        last_attempt_at=_now_iso(now),
        projected_at=state.get("projected_at"),
    )
    if status == PROJECTION_FAILED:
        logger.error(
            "feishu_projection_failed_final | device=%s | retries=%s | error=%s",
            device,
            retry_count,
            error_text,
        )
        return False
    logger.warning(
        "feishu_projection_retry_failed | device=%s | attempt=%s/%s"
        " | next_retry_in=%ss | error=%s",
        device,
        retry_count,
        config.FEISHU_PROJECTION_MAX_RETRIES,
        retry_backoff_seconds(retry_count),
        error_text,
    )
    return True


def should_suppress_inline_attempt(
    device: str, now: datetime | None = None
) -> bool:
    """True when a recent failed attempt makes a synchronous retry pointless.

    During a Feishu outage each inline attempt costs REQUEST_RETRY_TIMES ×
    timeout in a Waitress thread. After a failure, requests arriving inside
    the suppression window are accepted-and-deferred immediately instead of
    blocking; the scheduler retry (or the next request after the window)
    converges the Feishu state to the latest sample.
    """
    state = db.fetch_projection_state(device)
    if state is None:
        return False
    status = str(state.get("projection_status") or PROJECTION_OK)
    if status not in (PROJECTION_PENDING, PROJECTION_FAILED):
        return False
    last_attempt = _parse_iso(state.get("last_attempt_at"))
    if last_attempt is None:
        return False
    window = config.FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS
    if window <= 0:
        return False
    return (_now_datetime(now) - last_attempt).total_seconds() < window


def retry_backoff_seconds(retry_count: int) -> float:
    """Exponential backoff for scheduled retries: base * 2^n, capped 600s."""
    return min(
        config.FEISHU_PROJECTION_BACKOFF_SECONDS * (2 ** max(0, retry_count)),
        600.0,
    )


def list_due_projection_retries(
    now: datetime | None = None,
) -> list[tuple[str, datetime]]:
    """Devices with a pending projection whose backoff has elapsed.

    The runtime scheduler scans this each tick and (re)creates the durable
    ``FEISHU_PROJECTION`` task via ``create_or_get_unfinished`` (per-device
    dedupe), so restart recovery is automatic.
    """
    states = db.fetch_projection_states(status=PROJECTION_PENDING)
    current = _now_datetime(now)
    due: list[tuple[str, datetime]] = []
    for state in states:
        last_attempt = _parse_iso(state.get("last_attempt_at"))
        if last_attempt is None:
            # Never attempted (state written before any attempt): due now.
            due.append((state["device"], current))
            continue
        backoff = retry_backoff_seconds(int(state.get("retry_count") or 0))
        # timedelta 运算保持在 aware datetime 上，避免 naive/aware 比较。
        ready_at = last_attempt + timedelta(seconds=backoff)
        if current >= ready_at:
            due.append((state["device"], ready_at))
    return due


def ensure_projection_tasks(task_repository: Any, now: datetime | None = None) -> None:
    """(Re)create durable FEISHU_PROJECTION retry tasks for pending devices.

    Called by the runtime scheduler each tick. The per-device dedupe key
    guarantees at most one unfinished retry task per device; after a
    restart the scanner recreates the task from the persisted pending
    state, and a leftover PENDING task is simply reused. Best-effort:
    failures are logged, never raised (the scheduler loop must survive).
    """
    current = _now_datetime(now)
    try:
        # 错峰：把同时到期的重试任务按设备摊开（默认 2s 间隔），配合
        # 1s scheduler poll，保证每个 tick 至多 1-2 个 FEISHU_PROJECTION
        # 尝试，SHADOW_COMPARE / SYNC 任务不会被成批的投影重试饿死。
        for index, (device, due_at) in enumerate(
            list_due_projection_retries(now=current)
        ):
            task_repository.create_or_get_unfinished(
                task_type="FEISHU_PROJECTION",
                entity_type="DEVICE",
                entity_id=device,
                due_at=due_at + timedelta(seconds=index * _TASK_STAGGER_SECONDS),
                payload={"device": device},
                dedupe_key=f"FEISHU_PROJECTION:{device}",
                created_at=current,
            )
    except Exception:  # noqa: BLE001 - scanner must not kill the scheduler loop
        logger.exception("FEISHU_PROJECTION 任务扫描失败；下一 tick 重试")


def recover_pending_dispatches(now: datetime | None = None) -> None:
    """Dispatch projected samples that are not dispatched yet (every tick).

    This is the **single dispatch owner** for the HA projection → Runtime
    pipeline: the scheduler thread runs it on every tick, both for the
    normal flow (HTTP succeeded, projected watermark advanced) and for
    crash recovery. Invariant: ``last_projected_sample_time_ms >
    last_dispatched_sample_time_ms`` ⇒ unfinished dispatch work —
    independent of ``projection_status`` (the crash can happen after the
    status already flipped to ``ok``). Never raises.

    Delivery semantics are **at-least-once**, not exactly-once: a crash
    between the listener call and the watermark write can re-deliver the
    same sample once. That is safe because every downstream projection is
    idempotent (SHADOW_COMPARE dedupe by device+sample_time, VERIFY_ALARM
    dedupe keys, event keys, latest-sample upsert) — re-delivery never
    duplicates business effects.
    """
    try:
        states = db.fetch_undispatched_projection_states()
        for state in states:
            device = str(state["device"])
            projected_ms = int(state["last_projected_sample_time_ms"])
            try:
                row = db.fetch_device_sample_at(
                    device, devices.SOURCE_HOME_ASSISTANT, projected_ms
                )
            except Exception:  # noqa: BLE001 - transient read error: retry next tick
                logger.exception(
                    "dispatch_recover_read_failed | device=%s"
                    " | sample_time_ms=%s",
                    device,
                    projected_ms,
                )
                continue
            if row is None:
                # projected ⇒ persisted must hold; a missing row means a
                # corrupted watermark. Advance the dispatched watermark so
                # recovery cannot wedge, and log loudly for forensics.
                logger.error(
                    "dispatch_recover_missing_sample | device=%s"
                    " | sample_time_ms=%s",
                    device,
                    projected_ms,
                )
                db.mark_projection_dispatched(device, projected_ms)
                continue
            dispatched = dispatch_projected_sample(
                device,
                devices.sample_from_row(device, row, projected_ms),
                projected_ms,
            )
            logger.info(
                "dispatch_recovered | device=%s | sample_time_ms=%s"
                " | dispatched=%s",
                device,
                projected_ms,
                dispatched,
            )
    except Exception:  # noqa: BLE001 - recovery must not kill the scheduler loop
        logger.exception("投影派发恢复扫描失败；下一 tick 重试")


# ---------------------------------------------------------------------------
# Dispatch (phase B) with durable once-only guard
# ---------------------------------------------------------------------------


def is_sample_dispatched(device: str, sample_time_ms: int) -> bool:
    """True when this sample already reached the Runtime/Shadow pipeline."""
    state = db.fetch_projection_state(device)
    if state is None:
        return False
    dispatched = state.get("last_dispatched_sample_time_ms")
    return dispatched is not None and int(dispatched) >= int(sample_time_ms)


def is_sample_projected(device: str, sample_time_ms: int) -> bool:
    """True when this sample already succeeded on the Feishu projection.

    Used by the /temperature duplicate-request short-circuit: with dispatch
    deferred to the scheduler, a duplicate request whose sample is already
    projected (same content, watermark covers it) can replay the success
    semantics without touching Feishu again. Dispatch follows on the next
    scheduler tick via ``recover_pending_dispatches``.
    """
    state = db.fetch_projection_state(device)
    if state is None:
        return False
    projected = state.get("last_projected_sample_time_ms")
    return projected is not None and int(projected) >= int(sample_time_ms)


def dispatch_projected_sample(
    device: str,
    sample: MonitorSample,
    sample_time_ms: int | None = None,
) -> bool:
    """Notify Runtime/Shadow listeners exactly once per business sample.

    Ownership contract: the **scheduler thread only** (via
    ``recover_pending_dispatches``). HTTP threads must never call this —
    an inline dispatch from a Waitress thread was one half of the
    2026-09-02 AB-BA deadlock (HTTP held the dispatch lock waiting for
    ``_execution_lock`` while the scheduler held ``_execution_lock``
    waiting for the dispatch lock). There is no process lock in here by
    design: with a single owner there is nothing to serialize, and the
    durable watermark (``last_dispatched_sample_time_ms``) keeps the
    check -> notify -> mark sequence idempotent across restarts.

    Returns False when the sample was already dispatched or superseded by
    a newer one.
    """
    if sample_time_ms is None:
        sample_time_ms = round(sample.sample_time.timestamp() * 1000)
    sample_time_ms = int(sample_time_ms)

    if not db.is_mirror_available():
        # SQLITE_ENABLED=false：没有水位可写，退化为旧版 record_sample
        # 语义（直接通知），不做 once-only 去重。
        devices.dispatch_sample(sample)
        return True

    state = db.fetch_projection_state(device)
    dispatched_ms = (
        int(state.get("last_dispatched_sample_time_ms"))
        if state is not None and state.get("last_dispatched_sample_time_ms") is not None
        else None
    )
    if dispatched_ms is not None and dispatched_ms >= sample_time_ms:
        logger.info(
            "sample_dispatch_skipped | device=%s | sample_time_ms=%s"
            " | last_dispatched_sample_time_ms=%s | reason=already_dispatched",
            device,
            sample_time_ms,
            dispatched_ms,
        )
        return False

    devices.dispatch_sample(sample)
    advanced = db.mark_projection_dispatched(device, sample_time_ms)
    if not advanced:
        # A previous crash between notify and mark could leave a higher
        # watermark — keep it, log for forensics.
        logger.warning(
            "sample_dispatch_watermark_conflict | device=%s"
            " | sample_time_ms=%s",
            device,
            sample_time_ms,
        )
        return False
    logger.info(
        "sample_dispatched | device=%s | sample_time_ms=%s",
        device,
        sample_time_ms,
    )
    return True


# ---------------------------------------------------------------------------
# Scheduled retry execution (FEISHU_PROJECTION task handler)
# ---------------------------------------------------------------------------


def retry_device_projection(
    device: str, now: datetime | None = None
) -> dict[str, Any]:
    """Execute one scheduled projection retry for a device.

    Scheduler-blocking contract: every Feishu call in here is a SINGLE
    bounded attempt (``attempts=1``, timeout
    ``FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS``) — no internal retry
    loops. Worst handler duration = token + resolve + update ≈ 3 ×
    timeout (token cache hit: ≈ 2 ×). Failure returns immediately; the
    next attempt is rescheduled by the scanner via exponential backoff.

    Projects the *latest* persisted sample (Feishu is a current-state
    projection — replaying superseded values would regress it). On success
    it only advances the projected watermark; the **dispatch belongs to
    ``recover_pending_dispatches``** in the same tick's maintenance hook —
    keeping a single dispatch owner is precisely what removed the AB-BA
    deadlock. Idempotent: a device whose state is no longer ``pending`` is
    a no-op (the inline path already converged it), which makes duplicate
    tasks after restarts harmless.

    Raises RuntimeError when the retry fails so the scheduler marks the
    task FAILED (visible evidence); the scanner re-creates the next task
    from the pending state until the retry cap is exhausted.
    """
    state = db.fetch_projection_state(device)
    if state is None or str(state.get("projection_status")) != PROJECTION_PENDING:
        return {"device": device, "result": "skipped"}

    latest = db.fetch_previous_device_sample(
        device, source=devices.SOURCE_HOME_ASSISTANT
    )
    if latest is None:
        # Nothing locally to project; clear pending so the scanner does not
        # spin on an empty device forever.
        mark_projection_success(device, now=now)
        return {"device": device, "result": "no_sample"}

    offline = str(latest.get("status") or "").lower() == devices.STATUS_OFFLINE
    fields = build_projection_fields(
        latest.get("temperature"), latest.get("humidity"), offline=offline
    )
    attempt_timeout = config.FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS
    try:
        record_id = resolve_record_id(
            device,
            config.DEVICES.get(device, {}).get("record_id"),
            attempt_timeout=attempt_timeout,
        )
        result = update_feishu_fields(
            record_id, fields, attempt_timeout=attempt_timeout
        )
        code = int(result.get("code", -1))
        if code != 0:
            raise RuntimeError(
                f"feishu returned code={code} msg={result.get('msg', '')!r}"
            )
    except (requests.exceptions.RequestException, RuntimeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        still_pending = mark_retry_attempt_failed(device, error, now=now)
        raise RuntimeError(
            f"feishu projection retry failed | device={device}"
            f" | still_pending={still_pending} | {error}"
        ) from exc

    mark_projection_success(
        device, int(latest["sample_time_ms"]), now=now
    )
    # Dispatch ownership: scheduler-only. recover_pending_dispatches (the
    # maintenance hook that runs right after this handler in the same tick)
    # sees projected > dispatched and dispatches — the handler must NOT
    # dispatch itself.
    logger.info(
        "feishu_projection_retry_ok | device=%s | sample_time_ms=%s"
        " | dispatch=scheduler_recovery",
        device,
        latest.get("sample_time_ms"),
    )
    return {
        "device": device,
        "result": "projected",
        "dispatch": "scheduler_recovery",
    }


# ---------------------------------------------------------------------------
# Observability (/api/system/status)
# ---------------------------------------------------------------------------


def projection_health_summary() -> dict[str, Any]:
    """Counts + device lists for the feishu_projection status section."""
    states = db.fetch_projection_states()
    by_status = {"ok": 0, "pending": 0, "failed": 0}
    pending_devices: list[str] = []
    failed: list[dict[str, Any]] = []
    for state in states:
        status = str(state.get("projection_status") or PROJECTION_OK)
        by_status[status] = by_status.get(status, 0) + 1
        if status == PROJECTION_PENDING:
            pending_devices.append(str(state.get("device")))
        elif status == PROJECTION_FAILED:
            failed.append(
                {
                    "device": state.get("device"),
                    "retry_count": state.get("retry_count"),
                    "last_error": state.get("last_error"),
                    "last_attempt_at": state.get("last_attempt_at"),
                }
            )
    return {
        "tracked_devices": len(states),
        "by_status": by_status,
        "pending_devices": pending_devices,
        "failed_devices": failed,
        "undispatched_projected": len(db.fetch_undispatched_projection_states()),
        "max_retries": config.FEISHU_PROJECTION_MAX_RETRIES,
        "attempt_timeout_seconds": config.FEISHU_PROJECTION_ATTEMPT_TIMEOUT_SECONDS,
    }
