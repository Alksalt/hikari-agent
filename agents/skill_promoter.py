"""skill_promoter — reflection-based automatic skill drafting.

Scans recent character_thoughts for repeated action patterns (same tool
call sequence appearing 3+ times in the last 14 days). When a pattern is
found, drafts a skill to session_scratch so Hikari can announce it on the
next turn and the user can approve with skill_approve.

Runs at the end of run_daily_reflection — non-fatal, at most once per week
(gated by the `skill_promoter.last_run` runtime_state key).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

_COOLDOWN_DAYS = 7
_THOUGHT_WINDOW_DAYS = 14

_SCAN_SYSTEM = (
    "You are a pattern-detection assistant. "
    "Given a list of diary entries from an AI assistant, identify any recurring "
    "action pattern that appears 3 or more times and would be worth automating as "
    "a reusable skill. A skill is a prompt template an assistant can invoke to "
    "perform a specific multi-step task consistently. "
    "If you find a clear repeating pattern, respond with a JSON object: "
    '{"found": true, "skill_id": "<kebab-case-name>", '
    '"description": "<one sentence>", "content": "<skill markdown>"}. '
    'If no pattern, respond with {"found": false}. '
    "Output ONLY valid JSON — no prose, no fences."
)


def _is_on_cooldown() -> bool:
    from storage import db as _db
    iso = _db.runtime_get("skill_promoter.last_run")
    if not iso:
        return False
    try:
        last = datetime.fromisoformat(iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last).days < _COOLDOWN_DAYS
    except (ValueError, TypeError):
        return False


def _recent_thoughts(days: int = _THOUGHT_WINDOW_DAYS) -> list[str]:
    from storage import db as _db
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        with _db._conn() as conn:
            rows = conn.execute(
                "SELECT thought FROM character_thoughts WHERE created_at >= ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        logger.exception("skill_promoter: failed to read character_thoughts")
        return []


async def maybe_promote_skill() -> None:
    """Check for repeating patterns and draft a skill if found. Non-fatal."""
    if _is_on_cooldown():
        logger.debug("skill_promoter: on cooldown — skip")
        return

    thoughts = _recent_thoughts()
    if len(thoughts) < 9:
        logger.debug("skill_promoter: too few thoughts (%d) — skip", len(thoughts))
        return

    from agents.runtime import _call_aux_llm
    from storage import db as _db

    def _set_cooldown() -> None:
        _db.runtime_set("skill_promoter.last_run", datetime.now(UTC).isoformat())

    sample = "\n---\n".join(thoughts[-40:])
    prompt = f"Diary entries (recent {_THOUGHT_WINDOW_DAYS} days):\n\n{sample}"
    try:
        raw = await _call_aux_llm(prompt, system=_SCAN_SYSTEM)
    except Exception:
        logger.exception("skill_promoter: run_reflection_call failed")
        _set_cooldown()
        return

    raw = raw.strip()
    if raw.startswith("```"):
        import re
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("skill_promoter: non-JSON response — applying cooldown")
        _set_cooldown()
        return

    if not result.get("found"):
        logger.debug("skill_promoter: no repeating pattern found")
        _set_cooldown()
        return

    skill_id = str(result.get("skill_id") or "").strip()
    description = str(result.get("description") or "").strip()
    content = str(result.get("content") or "").strip()
    if not skill_id or not content:
        logger.warning("skill_promoter: LLM returned found=true but incomplete fields")
        _set_cooldown()
        return

    session_id = _db.get_session_id() or "pending"
    payload = json.dumps({
        "skill_id": skill_id,
        "description": description,
        "content": content,
    }, ensure_ascii=False)
    try:
        with _db._conn() as conn:
            conn.execute(
                "INSERT INTO session_scratch (session_id, topic, payload_json) VALUES (?, ?, ?)",
                (session_id, f"staged_skill:{skill_id}", payload),
            )
        logger.info("skill_promoter: drafted skill %r → session_scratch", skill_id)
    except Exception:
        logger.exception("skill_promoter: failed to write staged skill")
        _set_cooldown()
        return

    _set_cooldown()
