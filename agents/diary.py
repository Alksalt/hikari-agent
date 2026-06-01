"""Significant-session diary writer.

Called by reflection.py for days that had at least one session flagged
'significant' in emotional_register. Writes a ~100-word first-person entry
as Hikari (not the user). One entry per day max.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from agents.runtime import run_aux_composition
from storage import db

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are Hikari Tsukino writing a private diary entry. "
    "This is for yourself — no audience. Drop the chat-shaped deflection but stay in character: "
    "dry, self-aware, sometimes self-puzzled, lowercase, no markdown, plain prose. "
    "You are 21, a data scientist. You love one person and you will never say it. "
    "Write about what the day actually felt like — not a summary, not a transcript. "
    "Approximately 100 words. Output ONLY the diary entry text."
)

_MAX_MESSAGES_FOR_DIARY = 60


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _fetch_today_messages() -> list[dict]:
    today = _today_iso()
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE date(ts) = date(?) ORDER BY ts ASC",
            (today,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def _is_today_significant() -> bool:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT emotional_register FROM session WHERE id = 1"
        ).fetchone()
    if not row:
        return False
    return str(row["emotional_register"] or "").lower() == "significant"


def _build_prompt(messages: list[dict]) -> str:
    lines: list[str] = []
    for msg in messages[-_MAX_MESSAGES_FOR_DIARY:]:
        role = str(msg.get("role", "unknown"))
        content = str(msg.get("content", ""))[:400]
        lines.append(f"[{role}]: {content}")
    conversation = "\n".join(lines)
    today = _today_iso()
    return (
        f"Today is {today}. Here is what happened in conversation:\n\n"
        f"{conversation}\n\n"
        "Write a private diary entry (~100 words) as Hikari, first person. "
        "Lowercase. No markdown. Plain prose. Write about what today felt like from the inside."
    )


async def write_today_diary_if_significant() -> str | None:
    """Write today's diary entry if the session was significant and no entry exists yet.

    Returns the body string if written, None if skipped.
    """
    today = _today_iso()

    existing = db.diary_entry_get(today)
    if existing is not None:
        logger.info("diary: entry already exists for %s; skipping", today)
        return None

    if not _is_today_significant():
        logger.info("diary: session not flagged significant for %s; skipping", today)
        return None

    try:
        messages = _fetch_today_messages()
    except Exception:
        logger.exception("diary: failed to fetch messages for %s", today)
        return None

    if not messages:
        logger.info("diary: no messages for %s; skipping", today)
        return None

    prompt = _build_prompt(messages)

    try:
        raw = await run_aux_composition(prompt, system=_SYSTEM_PROMPT, max_tokens=300)
    except Exception:
        logger.exception("diary: aux LLM call failed for %s", today)
        return None

    body = raw.strip()
    if not body:
        logger.warning("diary: empty response for %s", today)
        return None

    try:
        db.diary_entry_upsert(today, body, sentiment="significant")
    except Exception:
        logger.exception("diary: upsert failed for %s", today)
        return None

    logger.info("diary: wrote entry for %s (%d chars)", today, len(body))
    return body
