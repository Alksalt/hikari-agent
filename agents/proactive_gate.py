"""Global reservation + final-gate for every proactive send.

asyncio.Lock serializes producers; DB-backed audit row (status reserved → sent
or aborted) makes suppressions observable. Centralizes silence-window /
quiet-hours / dedup checks that previously lived scattered across producers.

Wave 3: reason-contract columns (anchor / why_now / suggested_action /
confidence / controls_json / data_checked_json) are populated at send time
from the candidate context passed via ``reserve_and_send``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)
_PROACTIVE_LOCK = asyncio.Lock()

ReservationStatus = Literal["sent", "aborted"]
AbortReason = Literal["silence_window", "quiet_hours", "dedup", "send_failed", "empty_text", "proactive_disabled", "snooze"]


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


def _snooze_active(db, producer_id: str) -> bool:
    """Return True when a snooze is active for *producer_id* or for all sources.

    Reads ``proactive_snooze_until`` from runtime_state — a JSON map of
    {source_id: iso_timestamp}.  Two keys are checked:

    - ``"all"`` — global "snooze all" written by /proactive snooze all <dur>
    - ``producer_id`` — per-source snooze written by /proactive snooze <src> <dur>

    An entry is only considered active when its timestamp is strictly in the
    future.  Unparseable / absent entries are treated as inactive (fails open so
    a bad snooze entry never permanently mutes a source).
    """
    try:
        raw = db.runtime_get("proactive_snooze_until")
        if not raw:
            return False
        snooze_map: dict[str, str] = json.loads(raw)
        now = datetime.now(UTC)
        for key in ("all", producer_id):
            iso = snooze_map.get(key)
            if iso:
                try:
                    until = datetime.fromisoformat(iso)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=UTC)
                    if now < until:
                        return True
                except (ValueError, TypeError):
                    continue
        return False
    except Exception:
        logger.exception(
            "proactive_gate: error reading snooze map for producer %r — "
            "treating as inactive (fails open)",
            producer_id,
        )
        return False


def _proactive_globally_disabled(db) -> bool:
    """Return True only when the user has explicitly set proactive.enabled=false.

    The signal is ``proactive_enabled_sources_override == "[]"`` (the empty JSON
    list) written by cockpit._write_proactive_enabled when the user turns off the
    global toggle.  A NULL / absent override means "use defaults = ON".  A
    non-empty list means specific sources are ON.  Only the empty-list value means
    "everything off".

    Fails CLOSED (treat as disabled) if the runtime read raises: for a privacy
    off-switch, a transient DB error must never let proactive content leak.
    """
    try:
        raw = db.runtime_get("proactive_enabled_sources_override")
    except Exception:
        logger.error(
            "proactive_gate: runtime_get failed reading proactive override — "
            "failing CLOSED (suppressing proactive)"
        )
        return True
    return raw is not None and raw.strip() == "[]"


def _is_reminder_producer(producer_id: str) -> bool:
    """Return True when the producer is a user-created reminder.

    Both reminder call sites in agents/proactive.py use producer_id="reminder".
    This is the sole exemption from the proactive_disabled gate.
    """
    return producer_id == "reminder"


def _extract_reason_contract(candidate: Any) -> dict[str, Any]:
    """Extract reason-contract fields from a TriggerCandidate (or duck-typed dict).

    All fields are optional — missing attrs / keys produce None, never raise.
    Returns a dict with keys: anchor, why_now, suggested_action, confidence,
    controls_json, data_checked_json (JSON-serialised strings where applicable).
    """
    if candidate is None:
        return {}

    def _get(key: str):
        if hasattr(candidate, key):
            return getattr(candidate, key)
        if isinstance(candidate, dict):
            return candidate.get(key)
        return None

    # confidence is already a float on TriggerCandidate; keep it as-is
    raw_confidence = _get("confidence")
    try:
        confidence = float(raw_confidence) if raw_confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    # anchor: prefer explicit attr; fall back to payload["id"] / payload["event_id"]
    anchor = _get("anchor")
    if anchor is None:
        payload = _get("payload") or {}
        if isinstance(payload, dict):
            anchor = (
                payload.get("anchor")
                or payload.get("thread_id")
                or payload.get("event_id")
                or payload.get("file_id")
                or payload.get("id")
            )
    anchor = str(anchor) if anchor is not None else None

    why_now = _get("why_now")
    if why_now is None:
        # derive a minimal why_now from pool / source if not supplied
        source = _get("source") or ""
        pool = _get("pool") or ""
        if source or pool:
            why_now = f"source={source} pool={pool}"
    why_now = str(why_now) if why_now is not None else None

    suggested_action = _get("suggested_action")
    suggested_action = str(suggested_action) if suggested_action is not None else None

    # controls: JSON with standard snooze/mute knobs + any candidate-supplied overrides
    controls_raw = _get("controls")
    if controls_raw is None:
        source = _get("source") or ""
        controls_raw = {"snooze_hours": [1, 4, 24], "mute_source": source}
    try:
        controls_json: str | None = json.dumps(controls_raw, default=str)
    except (TypeError, ValueError):
        controls_json = None

    # data_checked: what data sources were consulted
    data_checked_raw = _get("data_checked")
    if data_checked_raw is None:
        # try to infer from source name
        source = _get("source") or ""
        inferred: list[str] = []
        if "gmail" in source:
            inferred.append("gmail")
        if "calendar" in source or "event" in source:
            inferred.append("calendar")
        if "decision" in source:
            inferred.append("decision_log")
        if "wiki" in source or "drive" in source:
            inferred.append("drive")
        if "reminder" in source:
            inferred.append("reminders")
        data_checked_raw = inferred or None
    try:
        data_checked_json: str | None = (
            json.dumps(data_checked_raw, default=str) if data_checked_raw is not None else None
        )
    except (TypeError, ValueError):
        data_checked_json = None

    return {
        "anchor": anchor,
        "why_now": why_now,
        "suggested_action": suggested_action,
        "confidence": confidence,
        "controls_json": controls_json,
        "data_checked_json": data_checked_json,
    }


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
    candidate: Any = None,
) -> ReservationResult:
    """Acquire the global proactive lock, reserve an audit row, run the final
    gate (silence / quiet-hours / dedup), then either send (status=sent) or
    suppress (status=aborted, reason=...).

    ``candidate`` is an optional TriggerCandidate (or any duck-typed object)
    whose reason-contract fields (anchor, why_now, suggested_action, confidence,
    controls, data_checked) are written to the proactive_events row at send time.
    """
    if db is None:
        from storage import db as _db_mod
        db = _db_mod

    if not text:
        return ReservationResult("aborted", "empty_text", None, 0, "")

    # Extract reason-contract fields once (before acquiring the lock — pure CPU).
    reason = _extract_reason_contract(candidate)

    async with _PROACTIVE_LOCK:
        # 1. Reserve a row (with reason-contract baked in at reservation time).
        event_id = db.proactive_event_insert(
            source=producer_id,
            pattern=pattern,
            payload_json="{}",       # minimal — PII goes on the sent-terminal update only
            chat_id=chat_id,
            status="reserved",
            dedup_key=dedup_key,
            anchor=reason.get("anchor"),
            why_now=reason.get("why_now"),
            suggested_action=reason.get("suggested_action"),
            confidence=reason.get("confidence"),
            controls_json=reason.get("controls_json"),
            data_checked_json=reason.get("data_checked_json"),
        )

        # 2. Final gate — proactive_disabled checked FIRST.
        #    Reminder producers (producer_id="reminder") are exempt so user-created
        #    reminders always fire regardless of the global toggle.
        abort_reason: AbortReason | None = None
        if _proactive_globally_disabled(db) and not _is_reminder_producer(producer_id):
            abort_reason = "proactive_disabled"
        elif _silence_active(db):
            abort_reason = "silence_window"
        elif _is_quiet_now(db):
            abort_reason = "quiet_hours"
        elif _snooze_active(db, producer_id):
            abort_reason = "snooze"
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

        # 4. Commit terminal (full payload + reason-contract confirmation).
        db.proactive_event_update_terminal(
            event_id, status="sent", telegram_message_id=tg_id,
            payload_json=payload_json,   # commit full payload only on success
            anchor=reason.get("anchor"),
            why_now=reason.get("why_now"),
            suggested_action=reason.get("suggested_action"),
            confidence=reason.get("confidence"),
            controls_json=reason.get("controls_json"),
            data_checked_json=reason.get("data_checked_json"),
        )

        # 5. Post-send hooks keyed on dedup_key prefix.
        if dedup_key and dedup_key.startswith("decision_resolve_due:"):
            try:
                decision_id = int(dedup_key.split(":", 1)[1])
                db.decision_mark_asked(decision_id)
            except Exception:
                logger.exception("decision_mark_asked failed for %r", dedup_key)

        return ReservationResult("sent", None, tg_id, event_id, final_text)
