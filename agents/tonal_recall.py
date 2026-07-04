"""Session emotional register classifier.

At session end, reads all messages for the session and asks DeepSeek for
a single tonal token. Writes the result to session.emotional_register.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents.runtime import run_aux_composition
from storage import db

logger = logging.getLogger(__name__)

_ALLOWED_REGISTERS = frozenset({"warm", "neutral", "tense", "frosty", "significant"})

_SYSTEM_PROMPT = (
    "You are classifying the emotional register of a conversation session. "
    "Output EXACTLY one token from this list: warm, neutral, tense, frosty, significant. "
    "Definitions: "
    "warm = affectionate or playful energy; "
    "neutral = ordinary, even-keeled; "
    "tense = friction, conflict, or unresolved strain; "
    "frosty = cold, withdrawn, or clipped exchange; "
    "significant = emotionally heavy moment — grief, confession, breakthrough, real vulnerability. "
    "Output only the single token. No punctuation, no explanation."
)

_MAX_MESSAGES = 40
_WINDOW_HOURS = 24   # caller is the daily 09:00 reflection — "session" = last day
_MIN_MESSAGES = 4    # thinner than this = no real session; don't classify


def _fetch_recent_messages() -> list[dict]:
    """Messages from the last _WINDOW_HOURS, oldest first, capped at _MAX_MESSAGES.

    A UTC calendar-date filter (``date(ts) = date('now')``) doesn't work here:
    ``ts`` is stored in UTC but the only caller is the 09:00-LOCAL daily
    reflection, so an evening session in a timezone behind UTC lands on UTC
    date D-1 and would return zero rows. A rolling 24h window sidesteps the
    boundary without needing a calendar-date match.

    A fixed row-count window with no time bound classified 9 days of history
    as one "session" at real traffic volume (2026-07-04: register stuck on
    'tense' from week-old friction). Time-bound the window; the row cap only
    protects the prompt size within it.
    """
    since = (datetime.now(UTC) - timedelta(hours=_WINDOW_HOURS)).isoformat()
    return db.messages_since(since, exclude_ephemeral=True, limit=_MAX_MESSAGES)


def _build_prompt(messages: list[dict]) -> str:
    lines: list[str] = []
    for msg in messages[-_MAX_MESSAGES:]:
        role = str(msg.get("role", "unknown"))
        content = str(msg.get("content", ""))[:400]
        lines.append(f"[{role}]: {content}")
    conversation = "\n".join(lines)
    return (
        f"Conversation:\n{conversation}\n\n"
        "What is the emotional register? Output one token only: warm / neutral / tense / frosty / significant"
    )


def _persist_register(register: str, session_id: str) -> None:
    """Write `register` to the single session row (id=1).

    Extracted (Task 6) so both the thin-window gate and the normal
    end-of-classification path share one write path. Behavior is unchanged
    from the original inline block: a missing session row (rowcount == 0)
    logs a warning but does not raise; a DB failure during the write logs
    and re-raises so the caller decides whether to propagate or swallow it.
    """
    try:
        with db._conn() as conn:
            cur = conn.execute(
                "UPDATE session SET emotional_register = ? WHERE id = 1",
                (register,),
            )
            if cur.rowcount == 0:
                logger.warning(
                    "tonal_recall: session row id=1 missing — emotional_register not persisted "
                    "(session_id=%s, register=%s)",
                    session_id, register,
                )
    except Exception:
        logger.exception(
            "tonal_recall: failed to write emotional_register for session %s", session_id
        )
        raise


async def compute_session_register(session_id: str) -> str:
    """Classify the session's emotional register and persist it.

    Returns the register token, or 'neutral' as a safe fallback.
    """
    try:
        messages = _fetch_recent_messages()
    except Exception:
        logger.exception("tonal_recall: failed to fetch messages for session %s", session_id)
        return "neutral"

    if len(messages) < _MIN_MESSAGES:
        # A stale register (e.g. 'tense' from friction days ago) must not
        # survive a window this thin — persist 'neutral' directly rather
        # than leaving whatever value was already there. Also covers the
        # zero-message case the old code special-cased separately.
        logger.info(
            "tonal_recall: only %d messages in the last %dh for session %s — "
            "persisting neutral (stale register must not survive)",
            len(messages), _WINDOW_HOURS, session_id,
        )
        try:
            _persist_register("neutral", session_id)
        except Exception:
            logger.exception(
                "tonal_recall: failed to persist neutral for thin window (session %s)",
                session_id,
            )
        return "neutral"

    prompt = _build_prompt(messages)

    try:
        raw = await run_aux_composition(prompt, system=_SYSTEM_PROMPT, max_tokens=32)
    except Exception:
        logger.exception("tonal_recall: aux LLM call failed for session %s", session_id)
        return "neutral"

    register = raw.strip().lower().rstrip(".")
    if register not in _ALLOWED_REGISTERS:
        # Cheap model sometimes wraps the answer ("the register is tense") or
        # returns a non-register word ("none"). Scan for any allowed token
        # before giving up — the 5 registers aren't substrings of each other.
        found = next((r for r in _ALLOWED_REGISTERS if r in register), None)
        if found:
            register = found
        else:
            logger.warning(
                "tonal_recall: unexpected register %r for session %s; falling back to 'neutral'",
                register, session_id,
            )
            register = "neutral"

    _persist_register(register, session_id)

    logger.info("tonal_recall: session %s register = %s", session_id, register)
    return register
