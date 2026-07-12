"""Ask-user question loop for job-search mail actions (Sprint 2, Task 6).

The job-search repository (``mail_actions_cli.py`` / ``mail_state.py``) emits
``kind='ask-user'`` mail actions carrying enumerated options when mail_triage
cannot deterministically resolve an inbound email on its own. This module is
the Hikari-side half of that loop:

  - ``poll_and_ask`` (scheduler job, every ``mail_decisions.poll_interval_minutes``)
    pushes each un-asked *urgent* (priority 0) ask-user action immediately
    through ``agents.proactive_gate.reserve_and_send``. Non-urgent ask-user
    actions are intentionally left alone here — they surface instead in the
    morning brief (``agents/daily_brief.py``'s composer renders them as
    numbered questions from the same owner-CLI ``list`` payload).
  - ``fetch_current_row`` re-fetches the live owner-CLI payload for the chat
    tool (``tools/mail_actions/decide.py``) so option-number -> option-id
    mapping always comes from the CLI's current state, never from whatever
    the model happens to remember from an earlier turn.

Like ``agents/mail_handoff.py``, this module never opens ``job_search.db``
directly — every read/write crosses the process boundary through
``mail_actions_cli.py`` (via ``mail_handoff._run_cli`` / ``mail_handoff.decide``).

"Asked" tracking mirrors how ``mail_handoff`` tracks surfacing: after a
successful proactive send we call the owner CLI's ``mark-surfaced`` (via
``mail_handoff.mark_surfaced``), which stamps ``surfaced_at`` and increments
``surface_count``. Urgent/important actions keep repeating in the owner's
``list`` output until acknowledged (that's the owner's own semantics — see
``mail_state.pending_actions``), so we use ``surface_count == 0`` as our own
"not yet asked" gate rather than relying on the row disappearing from the
list.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from agents import config as cfg
from agents import mail_handoff
from agents.injection_guard import wrap_untrusted

logger = logging.getLogger(__name__)

_SOURCE = "mail_handoff"
_PRODUCER_ID = "mail_decisions"
_PATTERN = "urgent_question"
_PRIORITY_URGENT = 0


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


def _format_question(row: dict) -> str:
    """headline + numbered option labels + ``[action #id]``, every external
    string wrapped as untrusted (headline/options originate from inbound
    email content the mail-triage pipeline could not fully resolve)."""
    action_id = row.get("id")
    headline = wrap_untrusted(_SOURCE, str(row.get("headline") or "").strip())
    lines = [headline]
    for i, opt in enumerate(row.get("options") or [], start=1):
        label = str(opt.get("label") or opt.get("id") or "")
        lines.append(f"{i}. {wrap_untrusted(_SOURCE, label)}")
    lines.append(f"[action #{action_id}]")
    return "\n".join(lines)


async def poll_and_ask(send_text) -> int:
    """Scheduler entry point. Pushes each un-asked URGENT ask-user action
    immediately via the proactive gate; returns the number actually sent.

    Non-urgent ask-user rows are intentionally not touched here — they wait
    for the morning brief instead, which surfaces (and marks-surfaced) them
    through the existing ``mail_handoff``/``daily_brief`` pipeline.
    """
    if not bool(cfg.get("jobhunt.enabled", True)):
        return 0
    if not bool(cfg.get("mail_decisions.enabled", True)):
        return 0

    rows = unasked_ask_user_rows()
    urgent = [row for row in rows if _priority(row) == _PRIORITY_URGENT]
    if not urgent:
        return 0

    from agents.proactive_gate import reserve_and_send

    sent = 0
    for row in urgent:
        action_id = row.get("id")
        text = _format_question(row)
        result = await reserve_and_send(
            send_text_fn=send_text,
            producer_id=_PRODUCER_ID,
            pattern=_PATTERN,
            text=text,
            payload_json=json.dumps({"action_id": action_id}),
            dedup_key=f"{_PRODUCER_ID}:{action_id}",
            candidate={
                "anchor": f"mail_action_{action_id}",
                "why_now": "urgent ask-user mail action awaiting a choice",
                "suggested_action": "choose an option",
                "confidence": 0.9,
                "controls": {},
                "data_checked": ["mail_actions"],
            },
        )
        if result.status != "sent":
            logger.info(
                "mail_decisions: gate skipped action_id=%s (%s)",
                action_id, result.reason,
            )
            continue
        try:
            if not mail_handoff.mark_surfaced([{"action_id": action_id}]):
                logger.warning(
                    "mail_decisions: owner rejected mark-surfaced for action_id=%s "
                    "— question may repeat next poll", action_id,
                )
        except Exception:
            logger.exception(
                "mail_decisions: mark-surfaced failed for action_id=%s "
                "— question may repeat next poll", action_id,
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
