"""Global reservation + final-gate for every proactive send.

asyncio.Lock serializes producers; DB-backed audit row (status reserved → sent
or aborted) makes suppressions observable. Centralizes silence-window /
quiet-hours / dedup checks that previously lived scattered across producers.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)
_PROACTIVE_LOCK = asyncio.Lock()

ReservationStatus = Literal["sent", "aborted"]
AbortReason = Literal["silence_window", "quiet_hours", "dedup", "send_failed", "empty_text"]


@dataclass(frozen=True)
class ReservationResult:
    status: ReservationStatus
    reason: AbortReason | None
    telegram_message_id: int | None
    event_id: int          # 0 when status is aborted pre-reservation (empty text)
    final_text: str


SendTextFn = Callable[[str], Awaitable[tuple[str, int | None, bool]]]


def _is_quiet_now(_db=None) -> bool:
    """Delegate to agents.proactive._is_quiet_now — single source of truth."""
    from agents.proactive import _is_quiet_now as _proactive_is_quiet_now
    return _proactive_is_quiet_now()


def _silence_active(db) -> bool:
    iso = db.runtime_get("silence_until")
    if not iso:
        return False
    try:
        until = datetime.fromisoformat(iso)
    except Exception:
        logger.error(
            "proactive_gate: silence_until is unparseable (%r) — "
            "failing CLOSED (treating as active silence) to honor user intent",
            iso,
        )
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return datetime.now(UTC) < until


async def reserve_and_send(
    *,
    send_text_fn: SendTextFn,
    producer_id: str,
    pattern: str,
    text: str,
    payload_json: str = "{}",
    dedup_key: str | None = None,
    dedup_window_minutes: int = 60,
    chat_id: int | None = None,
    db=None,
) -> ReservationResult:
    """Acquire the global proactive lock, reserve an audit row, run the final
    gate (silence / quiet-hours / dedup), then either send (status=sent) or
    suppress (status=aborted, reason=...).
    """
    if db is None:
        from storage import db as _db_mod
        db = _db_mod

    if not text:
        return ReservationResult("aborted", "empty_text", None, 0, "")

    async with _PROACTIVE_LOCK:
        # 1. Reserve a row.
        event_id = db.proactive_event_insert(
            source=producer_id,
            pattern=pattern,
            payload_json="{}",       # minimal — PII goes on the sent-terminal update only
            chat_id=chat_id,
            status="reserved",
            dedup_key=dedup_key,
        )

        # 2. Final gate.
        abort_reason: AbortReason | None = None
        if _silence_active(db):
            abort_reason = "silence_window"
        elif _is_quiet_now(db):
            abort_reason = "quiet_hours"
        elif dedup_key and db.proactive_event_dedup_hit(
            producer_id, dedup_key, dedup_window_minutes
        ):
            abort_reason = "dedup"

        if abort_reason:
            db.proactive_event_update_terminal(
                event_id, status="aborted", aborted_reason=abort_reason
            )
            return ReservationResult("aborted", abort_reason, None, event_id, "")

        # 3. Send.
        try:
            final_text, tg_id, ok = await send_text_fn(text)
        except Exception:
            logger.exception("reserve_and_send: send_text_fn raised")
            final_text, tg_id, ok = (text, None, False)

        if not ok:
            db.proactive_event_update_terminal(
                event_id, status="aborted", aborted_reason="send_failed"
            )
            return ReservationResult("aborted", "send_failed", None, event_id, final_text)

        # 4. Commit terminal.
        db.proactive_event_update_terminal(
            event_id, status="sent", telegram_message_id=tg_id,
            payload_json=payload_json,   # commit full payload only on success
        )

        # 5. Post-send hooks keyed on dedup_key prefix.
        if dedup_key and dedup_key.startswith("decision_resolve_due:"):
            try:
                decision_id = int(dedup_key.split(":", 1)[1])
                db.decision_mark_asked(decision_id)
            except Exception:
                logger.exception("decision_mark_asked failed for %r", dedup_key)

        return ReservationResult("sent", None, tg_id, event_id, final_text)
