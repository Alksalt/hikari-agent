"""task_extractor — extract independent executable tasks from a compound message.

should_extract() is a fast heuristic gate: only calls the LLM when connective
keywords suggest multiple tasks AND the message is long enough to be compound.

extract_tasks() calls the cheap aux_model and returns a list of task dicts:
  [{"task": str, "depends_on": list[int]}]  # depends_on indexes into the list

Single-task messages return [{"task": <verbatim>}] with no depends_on.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Connective keywords that hint at multiple tasks (EN/UK/RU).
_COMPOUND_RE = re.compile(
    r"\b(and also|and then|and check|and look|and find|"
    r"also|then|plus|"
    r"потім|та|після|і ще|також|"
    r"и ещё|и потом|плюс|а также)\b",
    re.IGNORECASE,
)
_MIN_WORDS = 8

_EXTRACT_SYSTEM = (
    "You are a task extraction assistant. "
    "Extract independent executable tasks from the user message. "
    "Return a JSON array of objects with keys: "
    '  "task" (string), "depends_on" (array of zero-based integer indexes; empty = independent). '
    "If the message has only one task, return "
    '[{"task": "<verbatim message>"}]. '
    "Output ONLY valid JSON — no prose, no markdown fences, no explanations."
)


def should_extract(message: str) -> bool:
    """Return True only when the message looks like a compound request."""
    if len(message.split()) < _MIN_WORDS:
        return False
    return bool(_COMPOUND_RE.search(message))


async def extract_tasks(message: str) -> list[dict]:
    """Call the cheap aux model to split message into tasks.

    On any failure returns [{"task": message, "depends_on": []}] so
    the caller falls back to the single-turn path.
    """
    from agents.runtime import _call_aux_llm
    try:
        raw = await _call_aux_llm(message, system=_EXTRACT_SYSTEM)
        raw = raw.strip()
        # Strip markdown fences if model disobeyed.
        if raw.startswith("```"):
            raw = re.sub(r"^```[^\n]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("empty or non-list response")
        result: list[dict] = []
        for item in parsed:
            if not isinstance(item, dict) or "task" not in item:
                continue
            result.append({
                "task": str(item["task"]),
                "depends_on": [int(i) for i in item.get("depends_on", [])],
            })
        if not result:
            raise ValueError("no valid task objects")
        return result
    except Exception as exc:
        logger.warning("task_extractor: failed (%s) — single-task fallback", exc)
        return [{"task": message, "depends_on": []}]
