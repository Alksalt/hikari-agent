"""Immediate delivery loop for priority-0 job-search mail actions.

The job-search repository (``mail_actions_cli.py`` / ``mail_state.py``) emits
``kind='ask-user'`` mail actions carrying enumerated options when mail_triage
cannot deterministically resolve an inbound email on its own. This module is
the Hikari-side half of that loop:

  - ``poll_and_ask`` (scheduler job, every ``mail_decisions.poll_interval_minutes``)
    pushes every not-yet-surfaced priority-0 action immediately through
    ``agents.proactive_gate.reserve_and_send``. This includes interviews,
    offers, assessments, concrete questions/invites, and urgent ask-user
    actions. Lower-priority mail remains a silent operational record.
  - ``fetch_current_row`` re-fetches the live owner-CLI payload for the chat
    tool (``tools/mail_actions/decide.py``) so option-number -> option-id
    mapping always comes from the CLI's current state, never from whatever
    the model happens to remember from an earlier turn.

Like ``agents/mail_handoff.py``, this module never opens ``job_search.db``
directly — every read/write crosses the process boundary through
``mail_actions_cli.py`` (via ``mail_handoff._run_cli`` / ``mail_handoff.decide``).

Delivery tracking is receipt-based: ``reserve_and_send`` atomically commits a
durable Hikari receipt (event id + Telegram message id) before this module calls
the owner CLI's ``mark-delivered``.  The owner can therefore distinguish queued
from delivered and recover a callback failure without a duplicate Telegram
send.  Urgent/important actions keep repeating in the owner's
``list`` output until acknowledged (that's the owner's own semantics — see
``mail_state.pending_actions``), so we use ``surface_count == 0`` as our own
"not yet asked" gate rather than relying on the row disappearing from the
list.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from typing import Any

from agents import config as cfg
from agents import mail_handoff
from agents.injection_guard import escape_untrusted_delimiters_for_display

logger = logging.getLogger(__name__)

_PRODUCER_ID = "mail_decisions"
_PATTERN = "urgent_mail_action"
_PRIORITY_URGENT = 0
_ATTENTION_PUSH_NOW = "push_now"
_ATTENTION_CLASSES = {_ATTENTION_PUSH_NOW, "silent_hold", "silent_file"}
_DISPLAY_SOURCE_LABEL = "Jobbpost"
_HEADLINE_MAX_CHARS = 240
_DETAIL_MAX_CHARS = 320
_OPTION_MAX_CHARS = 180


def _sanitize_display_field(value: Any, *, max_chars: int) -> str:
    """Return a bounded single-line string safe for direct chat rendering.

    Mail action fields originate outside Hikari.  Replace whitespace and all
    Unicode control/format characters with plain spaces, neutralize forged
    prompt-envelope delimiters, and cap the result without invoking an LLM.
    """
    raw = escape_untrusted_delimiters_for_display(str(value or ""))
    visible = "".join(
        " " if char.isspace() or unicodedata.category(char).startswith("C") else char
        for char in raw
    )
    visible = re.sub(r" +", " ", visible).strip()
    if max_chars <= 0:
        return ""
    if len(visible) <= max_chars:
        return visible
    if max_chars == 1:
        return "…"
    return visible[: max_chars - 1].rstrip() + "…"


def _display_action_id(row: dict) -> str:
    """Return only the numeric owner id used by the decision tool."""
    raw = row.get("id")
    if isinstance(raw, bool):
        return "?"
    try:
        return str(int(raw))
    except (TypeError, ValueError, OverflowError):
        return "?"


def _low_priority_cap() -> int:
    return max(0, int(cfg.get("mail_actions.low_priority_cap", 5)))


def _list_payload(low_priority_cap: int | None = None) -> list[dict] | None:
    """Raw owner-CLI ``list`` rows (the ``mail_actions_cli._dump`` shape:
    ``id``, ``kind``, ``options`` (list of ``{"id","label"}``), ``decision``,
    ``priority``, ``surface_count``, ``headline``, ``details``, ...).

    Deliberately does NOT go through ``mail_handoff._structured_actions()`` —
    that normalizer (consumed by ``mail_handoff.pull_unprocessed()``) keeps
    the ``options``/``decision`` fields too (Task 6 extended it for the daily
    brief's composer), but this module talks to the raw CLI directly so it
    is never coupled to that normalizer's key names (``action_id`` vs ``id``).
    Returns ``None`` when the owner CLI process boundary is unavailable —
    callers must treat that as "nothing to do this poll", never as "no
    pending questions" (the legacy Markdown handoff never carries
    ``kind='ask-user'`` rows, so there is no fallback to read here).
    """
    cap = _low_priority_cap() if low_priority_cap is None else max(0, int(low_priority_cap))
    payload = mail_handoff._run_cli(
        "list", "--low-priority-cap", str(cap), expect_json=True
    )
    if payload is None:
        return None
    if not isinstance(payload, list):
        logger.error("mail_decisions: owner CLI JSON was not a list")
        return None
    return [row for row in payload if isinstance(row, dict)]


def _is_ask_user(row: dict) -> bool:
    return str(row.get("kind") or "") == "ask-user"


def _surface_count(row: dict) -> int:
    try:
        return int(row.get("surface_count") or 0)
    except (TypeError, ValueError):
        return 0


def _priority(row: dict) -> int:
    try:
        return int(row.get("priority", 2))
    except (TypeError, ValueError):
        return 2


def _attention_class(row: dict) -> str | None:
    """Return the explicit attention contract, with legacy compatibility.

    New owner rows are authoritative: only an exact ``push_now`` value may
    enter the immediate-delivery path. Rows created before ``attention_class``
    existed retain the old priority-0 behavior so an in-flight interview is
    not lost during rollout. Unknown explicit values fail closed.
    """
    if "attention_class" not in row or row.get("attention_class") in (None, ""):
        return _ATTENTION_PUSH_NOW if _priority(row) == _PRIORITY_URGENT else None
    value = str(row.get("attention_class") or "").strip()
    return value if value in _ATTENTION_CLASSES else None


def _delivery_dedup_key(row: dict) -> str:
    """Build a stable, PII-minimal receipt key for one owner action.

    The owner ``dedup_key`` survives local action-id reuse and database
    reconstruction. Hashing avoids copying sender/message identifiers into
    Hikari's receipt table. Pre-schema rows fall back to their action id.
    """
    owner_key = str(row.get("dedup_key") or "").strip()
    if owner_key:
        digest = hashlib.sha256(owner_key.encode("utf-8")).hexdigest()[:32]
        return f"{_PRODUCER_ID}:owner:{digest}"
    return f"{_PRODUCER_ID}:legacy:{row.get('id')}"


def _owner_mark_delivered(
    row: dict,
    *,
    delivery_key: str,
    event_id: int,
    telegram_message_id: int | None,
) -> bool:
    action_id = row.get("id")
    if action_id is None:
        logger.error("mail_decisions: cannot receipt owner row without action id")
        return False
    return mail_handoff.mark_delivered(
        action_id=int(action_id),
        event_id=int(event_id),
        dedup_key=delivery_key,
        telegram_message_id=telegram_message_id,
    )


def unasked_ask_user_rows(rows: list[dict] | None = None) -> list[dict]:
    """Filter to ask-user rows that are undecided and not yet surfaced once.

    ``rows`` lets callers/tests pass an already-fetched payload; ``None``
    (the default) fetches a fresh one via ``_list_payload()``.
    """
    if rows is None:
        rows = _list_payload()
    if not rows:
        return []
    return [
        row for row in rows
        if _is_ask_user(row) and row.get("decision") is None and _surface_count(row) == 0
    ]


def unasked_priority_zero_rows(rows: list[dict] | None = None) -> list[dict]:
    """Return undelivered ``push_now`` priority-0 actions.

    Explicit attention classes are authoritative. Legacy rows without the new
    field remain compatible through priority 0. The owner CLI already excludes
    resolved/acknowledged rows; ask-user rows additionally require no decision.
    """
    if rows is None:
        rows = _list_payload()
    if not rows:
        return []
    return [
        row for row in rows
        if _attention_class(row) == _ATTENTION_PUSH_NOW
        and _priority(row) == _PRIORITY_URGENT
        and _surface_count(row) == 0
        and (not _is_ask_user(row) or row.get("decision") is None)
    ]


def _format_question(row: dict) -> str:
    """Render a source-attributed question without prompt-only envelopes."""
    action_id = _display_action_id(row)
    headline = _sanitize_display_field(
        row.get("headline"), max_chars=_HEADLINE_MAX_CHARS
    ) or "(uten overskrift)"
    lines = [_DISPLAY_SOURCE_LABEL, headline]
    for i, opt in enumerate(row.get("options") or [], start=1):
        if isinstance(opt, dict):
            label = opt.get("label") or opt.get("id") or ""
        else:
            label = opt
        safe_label = _sanitize_display_field(label, max_chars=_OPTION_MAX_CHARS)
        lines.append(f"{i}. {safe_label or '(alternativ uten tekst)'}")
    lines.append(f"[action #{action_id}]")
    return "\n".join(lines)


def _format_attention(row: dict) -> str:
    """Render an urgent action without asking an LLM to rewrite email data."""
    if _is_ask_user(row):
        return _format_question(row)
    action_id = _display_action_id(row)
    headline = _sanitize_display_field(
        row.get("headline"), max_chars=_HEADLINE_MAX_CHARS
    ) or "(uten overskrift)"
    lines = [_DISPLAY_SOURCE_LABEL, headline]
    for detail in (row.get("details") or [])[:4]:
        safe_detail = _sanitize_display_field(detail, max_chars=_DETAIL_MAX_CHARS)
        if safe_detail:
            lines.append(f"• {safe_detail}")
    lines.append(f"[action #{action_id}]")
    return "\n".join(lines)


async def poll_and_ask(send_text) -> int:
    """Push each not-yet-delivered priority-0 action through the proactive gate.

    Lower-priority rows remain silent in the owner log. Returns the number of
    Telegram messages actually sent during this poll.
    """
    if not bool(cfg.get("jobhunt.enabled", True)):
        return 0
    if not bool(cfg.get("mail_decisions.enabled", True)):
        return 0

    urgent = unasked_priority_zero_rows()
    if not urgent:
        return 0

    from agents.proactive_gate import reserve_and_send

    sent = 0
    for row in urgent:
        action_id = row.get("id")
        delivery_key = _delivery_dedup_key(row)
        text = _format_attention(row)
        ask_user = _is_ask_user(row)
        result = await reserve_and_send(
            send_text_fn=send_text,
            producer_id=_PRODUCER_ID,
            pattern=_PATTERN,
            text=text,
            payload_json=json.dumps({"action_id": action_id}),
            dedup_key=delivery_key,
            durable_dedup=True,
            candidate={
                "anchor": f"mail_action_{action_id}",
                "why_now": "priority-0 job-search mail action",
                "suggested_action": "choose an option" if ask_user else "open the mail thread",
                "confidence": 0.9,
                "controls": {},
                "data_checked": ["mail_actions"],
            },
        )
        if result.reason == "dedup":
            # A durable dedup hit proves an earlier Telegram send. Re-read the
            # original receipt and replay only the owner callback, never send.
            try:
                from storage import db
                receipt = db.proactive_delivery_receipt_get(
                    _PRODUCER_ID, delivery_key
                )
                if receipt is None:
                    logger.error(
                        "mail_decisions: dedup hit without durable receipt for "
                        "action_id=%s", action_id,
                    )
                    continue
                if not _owner_mark_delivered(
                    row,
                    delivery_key=delivery_key,
                    event_id=int(receipt["event_id"]),
                    telegram_message_id=receipt.get("telegram_message_id"),
                ):
                    logger.warning(
                        "mail_decisions: owner delivery recovery remains pending "
                        "for action_id=%s", action_id,
                    )
            except Exception:
                logger.exception(
                    "mail_decisions: delivery receipt recovery failed for action_id=%s",
                    action_id,
                )
            continue
        if result.status != "sent":
            logger.info(
                "mail_decisions: gate skipped action_id=%s (%s)",
                action_id, result.reason,
            )
            continue
        try:
            if not _owner_mark_delivered(
                row,
                delivery_key=delivery_key,
                event_id=result.event_id,
                telegram_message_id=result.telegram_message_id,
            ):
                logger.warning(
                    "mail_decisions: owner rejected delivery receipt for "
                    "action_id=%s — durable recovery will retry next poll",
                    action_id,
                )
        except Exception:
            logger.exception(
                "mail_decisions: owner delivery callback failed for action_id=%s "
                "— durable recovery will retry next poll", action_id,
            )
        sent += 1
    return sent


def fetch_current_row(action_id: int, low_priority_cap: int | None = None) -> dict[str, Any] | None:
    """Re-fetch the CURRENT owner-CLI ``list`` payload and return the row
    matching ``action_id``, or ``None`` if it isn't currently pending there
    (unknown id, already decided/resolved, or the CLI is unavailable).

    Used exclusively by the ``mail_action_decide`` chat tool so option-number
    -> option-id mapping always comes from a fresh read — never from
    whatever the model remembers saying in an earlier turn. Uses a generous
    cap (``mail_actions.decide_lookup_cap``) rather than the poll-time
    ``mail_actions.low_priority_cap`` so a specific already-known action id
    is not excluded purely by low-priority list truncation.
    """
    cap = (
        int(cfg.get("mail_actions.decide_lookup_cap", 1000))
        if low_priority_cap is None else max(0, int(low_priority_cap))
    )
    rows = _list_payload(low_priority_cap=cap)
    if not rows:
        return None
    target = int(action_id)
    for row in rows:
        try:
            if int(row.get("id")) == target:
                return row
        except (TypeError, ValueError):
            continue
    return None
