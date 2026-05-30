"""task_extractor — extract independent executable tasks from a compound message.

Two surfaces:

1. **Legacy** (kept for back-compat): ``extract_tasks(message) -> list[dict]``
   returns the original ``[{"task": str, "depends_on": [int]}]`` shape used by
   the existing compound-turn dispatcher. Falls back to a single-task list on
   any parse error.

2. **Typed** (Sprint A Wave 3): ``extract_typed_nodes(message) -> list[CompoundTaskNode]``
   returns the new ``CompoundTaskNode`` shape with intent_type / utterance_span
   / entities / time_refs / risk_class / approval_policy / confidence /
   voice_uncertainty. The new compound-turn planner (``agents.compound_turn``)
   calls this and runs ``validate_nodes`` before execution.

``should_extract()`` is a fast heuristic gate: only calls the LLM when
connective keywords suggest multiple tasks AND the message is long enough.
"""
from __future__ import annotations

import json
import logging
import re

from agents.work_packet import CompoundTaskNode

logger = logging.getLogger(__name__)

# Connective keywords that hint at MULTIPLE tasks (EN/UK/RU).
#
# Only multi-word enumerators belong here. Bare conjunctions — Ukrainian "та"
# / "і" ("and"), "також" ("also"), "потім" ("then"), English "also"/"then"/
# "plus" — appear in ordinary single-clause sentences. The user converses in
# Ukrainian, so a bare "та" matched almost every message ≥8 words and misrouted
# normal chat onto the stateless, memory-less compound path → Hikari replied
# without conversation context and "felt dumb". Under-triggering is cheap (the
# message just takes the normal stateful turn, which already does multi-tool
# work in one turn); over-triggering is what hurt. So require an explicit
# "and X" / "а також" / "і ще"-style enumerator, never a lone "and"/"та".
_COMPOUND_RE = re.compile(
    r"\b(and also|and then|and check|and look|and find|and send|and schedule|"
    r"а також|а потім|і ще|і також|і потім|"
    r"и также|и потом|и ещё|а потом)\b",
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

# Sprint A Wave 3: typed extractor prompt. The model must output a JSON
# array of CompoundTaskNode-shaped objects. We document the enum vocab
# inline so DeepSeek doesn't invent fields.
_TYPED_EXTRACT_SYSTEM = (
    "You are a typed task-extraction planner. Given a user message, "
    "split it into atomic tasks and emit a JSON array. Each element MUST be "
    'an object with these fields:\n'
    '  "task":             string  (the task body, paraphrased if needed)\n'
    '  "intent_type":      one of "read" | "write" | "search" | "compose" | "dispatch" | "calc"\n'
    '  "utterance_span":   [start_offset, end_offset] (char offsets into the original message)\n'
    '  "entities":         array of short strings (names, places, files, tools, topics)\n'
    '  "time_refs":        array of time phrases lifted verbatim from the message ("in 1h", "tomorrow 9am", "за 30 хвилин"); [] if none\n'
    '  "risk_class":       one of "safe" | "approve_required" | "blocked"\n'
    '  "approval_policy":  one of "auto" | "confirm_send" | "block"\n'
    '  "confidence":       float in [0, 1] (your confidence in this decomposition)\n'
    '  "voice_uncertainty": bool (true if input looked transcribed / phonetic, false otherwise)\n'
    "\n"
    "Rules:\n"
    "- READ intents (lookup, recall, check status) are always 'safe' / 'auto'.\n"
    "- WRITE intents that *send* (email, message, post, push, publish) are 'approve_required' / 'confirm_send'.\n"
    "- DESTRUCTIVE intents (delete, drop, wipe, cancel-irrevocable) are 'blocked' / 'block'.\n"
    "- Spans must not overlap. Touching is OK.\n"
    "- Output ONLY valid JSON. No prose, no markdown fences, no commentary."
)


def should_extract(message: str) -> bool:
    """Return True only when the message looks like a compound request."""
    if len(message.split()) < _MIN_WORDS:
        return False
    return bool(_COMPOUND_RE.search(message))


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
    return raw


async def extract_tasks(message: str) -> list[dict]:
    """Legacy extractor: returns ``[{"task": str, "depends_on": [int]}]``.

    Kept so the existing compound-turn dispatcher (dict-based) keeps working
    while the typed pipeline rolls out. On any failure returns
    ``[{"task": message, "depends_on": []}]`` so the caller falls back to the
    single-turn path.
    """
    from agents.runtime import _call_aux_llm
    try:
        raw = await _call_aux_llm(message, system=_EXTRACT_SYSTEM)
        raw = _strip_fences(raw)
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
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("task_extractor: failed (%s) — single-task fallback", exc)
        return [{"task": message, "depends_on": []}]
    except Exception as exc:  # transport / runtime errors from aux LLM
        logger.warning("task_extractor: aux-llm error (%s) — single-task fallback", exc)
        return [{"task": message, "depends_on": []}]


async def extract_typed_nodes(message: str) -> list[CompoundTaskNode]:
    """Sprint A Wave 3 typed extractor.

    Calls the cheap aux model (DeepSeek via OpenRouter) with a typed system
    prompt and parses the JSON output into a ``list[CompoundTaskNode]``.

    Raises ``ValueError`` on hard parse failure so the caller (`compound_turn`)
    can escalate to the single-LLM fallback per Wave 3 scope. Transport-level
    aux-LLM errors are also raised (not silently swallowed) — single-LLM
    fallback is the right thing to do, but the caller decides.
    """
    from agents.runtime import run_aux_composition

    raw = await run_aux_composition(message, system=_TYPED_EXTRACT_SYSTEM, max_tokens=1024)
    raw = _strip_fences(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("typed_extractor: JSON parse failed (%s) — body=%r", exc, raw[:200])
        raise ValueError(f"typed extractor JSON parse failed: {exc}") from exc

    if not isinstance(parsed, list) or not parsed:
        raise ValueError("typed extractor returned empty or non-list response")

    nodes: list[CompoundTaskNode] = []
    for i, item in enumerate(parsed):
        try:
            node = CompoundTaskNode.from_raw_dict(item, full_text=message)
        except ValueError as exc:
            logger.warning("typed_extractor: skipping malformed item[%d]: %s", i, exc)
            continue
        nodes.append(node)

    if not nodes:
        raise ValueError("typed extractor produced no valid nodes")
    return nodes
