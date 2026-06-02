"""Post-turn extractor: non-explicit insights about the user.

Reads a window of recent messages and asks DeepSeek for 0-3 latent
observations (e.g. "tends to deflect about work", "brought up his father
twice this week"). Results land in peer_insights.
"""

from __future__ import annotations

import asyncio
import json
import logging

from agents.runtime import run_aux_composition
from storage import db

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an analyst observing a conversation between a user and an AI companion. "
    "Your job is to extract 0-3 NON-EXPLICIT insights about the USER — latent patterns, "
    "recurring themes, or emotional subtext that were not stated outright. "
    "Focus on the user's behavior, not the AI's responses. "
    "Examples: 'tends to deflect about work', 'brought up his father twice this week', "
    "'consistently frames problems as other people's fault'. "
    "Output ONLY a JSON array of strings. If there are no meaningful insights, output []. "
    "Never output more than 3 items. Never include trivial or obvious observations."
)

_MAX_WINDOW = 12  # messages to include in the prompt


def _format_window(message_window: list[dict]) -> str:
    lines: list[str] = []
    for msg in message_window[-_MAX_WINDOW:]:
        role = str(msg.get("role", "unknown"))
        content = str(msg.get("content", ""))[:500]
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


async def extract_post_turn(message_window: list[dict]) -> int:
    """Extract non-explicit insights from the last N messages.

    Returns the number of insights inserted.
    """
    if not message_window:
        return 0

    conversation_text = _format_window(message_window)
    prompt = (
        f"Conversation window:\n{conversation_text}\n\n"
        "Extract 0-3 non-explicit insights about the USER. Output as JSON array of strings."
    )

    try:
        raw = await run_aux_composition(prompt, system=_SYSTEM_PROMPT, max_tokens=256)
    except Exception:
        logger.exception("dialectic: aux LLM call failed")
        return 0

    raw = raw.strip()
    # Strip markdown fences if present (splitlines-based, mirrors drift_judge/_strip_fences).
    # Using split("```") is fragile: a stray backtick inside a JSON string value
    # splits the payload and discards everything after it.
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    raw = raw.strip()

    # Cheap model sometimes returns a bare "None"/"null" instead of the "[]"
    # the prompt asks for — treat as "no insights", not a noisy parse failure.
    if raw.lower() in {"none", "null", "n/a", "(none)", ""}:
        return 0

    try:
        insights = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("dialectic: JSON parse failed; raw=%r", raw[:200])
        return 0

    if not isinstance(insights, list):
        logger.warning("dialectic: expected list, got %s", type(insights).__name__)
        return 0

    from agents.reflection_sanitize import MemoryInstructionShape, sanitize

    count = 0
    for item in insights[:3]:
        if not isinstance(item, str):
            logger.warning("dialectic: non-string item skipped: %r", item)
            continue
        item = item.strip()
        if not item:
            continue
        try:
            item = sanitize(item, kind="observation")
        except MemoryInstructionShape as exc:
            logger.warning("dialectic: dropped injection-shaped insight — matched %r", str(exc))
            continue
        try:
            db.peer_insight_insert(item, surface_score=0.5, source="dialectic")
            count += 1
        except Exception:
            logger.exception("dialectic: peer_insight_insert failed for %r", item)

    if count:
        logger.info("dialectic: inserted %d insights", count)
    return count


if __name__ == "__main__":
    sample_window = [
        {"role": "user", "content": "i don't want to talk about my job right now"},
        {"role": "assistant", "content": "okay. what else is going on?"},
        {"role": "user", "content": "just tired. my dad used to say sleep fixes everything"},
        {"role": "assistant", "content": "...that's oddly optimistic for him, based on what you've said."},
        {"role": "user", "content": "yeah well. anyway. what did you say earlier about that library?"},
    ]

    async def _smoke() -> None:
        n = await extract_post_turn(sample_window)
        print(f"inserted {n} insight(s)")

    asyncio.run(_smoke())
