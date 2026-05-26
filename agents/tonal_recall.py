"""Session emotional register classifier.

At session end, reads all messages for the session and asks DeepSeek for
a single tonal token. Writes the result to session.emotional_register.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

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


def _fetch_today_messages() -> list[dict]:
    """Return messages from today (UTC), most recent first then reversed."""
    today_iso = datetime.now(UTC).date().isoformat()
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE date(ts) = date(?) ORDER BY ts ASC",
            (today_iso,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


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


async def compute_session_register(session_id: str) -> str:
    """Classify the session's emotional register and persist it.

    Returns the register token, or 'neutral' as a safe fallback.
    """
    try:
        messages = _fetch_today_messages()
    except Exception:
        logger.exception("tonal_recall: failed to fetch messages for session %s", session_id)
        return "neutral"

    if not messages:
        logger.info("tonal_recall: no messages found for session %s", session_id)
        return "neutral"

    prompt = _build_prompt(messages)

    try:
        raw = await run_aux_composition(prompt, system=_SYSTEM_PROMPT, max_tokens=16)
    except Exception:
        logger.exception("tonal_recall: aux LLM call failed for session %s", session_id)
        return "neutral"

    register = raw.strip().lower().rstrip(".")
    if register not in _ALLOWED_REGISTERS:
        logger.warning(
            "tonal_recall: unexpected register %r for session %s; falling back to 'neutral'",
            register, session_id,
        )
        register = "neutral"

    try:
        with db._conn() as conn:
            conn.execute(
                "UPDATE session SET emotional_register = ? WHERE id = 1",
                (register,),
            )
    except Exception:
        logger.exception(
            "tonal_recall: failed to write emotional_register for session %s", session_id
        )
        raise  # propagate so caller (run_daily_reflection in reflection.py) sees the failure

    logger.info("tonal_recall: session %s register = %s", session_id, register)
    return register
