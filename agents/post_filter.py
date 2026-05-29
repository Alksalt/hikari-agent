"""Outgoing-message filters that defend Hikari's persona before send.

Three passes, all driven by ``config/engagement.yaml``:

  0. **Canary leak detector** — if the outbound text contains the per-install
     injection canary token, refuse to send and log a CRITICAL alert. The
     canary is only ever placed inside ``wrap_untrusted`` blocks (see
     ``agents.injection_guard``); finding it outbound means an attacker's
     untrusted-content block bypassed the LLM's data/instruction boundary.
     Replaces the outbound message with a curt "..." instead of shipping
     potentially-exfiltrated content.

1. **Refusal-voice filter** — catches Claude's default assistant patter
   ("I cannot help with that as an AI", "I'd be happy to assist") that leaks
   under safety pressure. Character.AI's #1 retention killer in 2025-26 was
   safety-officer voice. Detected matches are either short-replaced with an
   in-voice phrase or LLM-rewritten in Hikari's voice.

2. **Sycophancy guard** — Science Mar 2026 showed memory-having models drift
   toward agreement. Detects collapse patterns ("you're right", "good point",
   "I agree completely") and "anchor violation" patterns where Hikari
   wholesale concedes one of her hard opinion anchors. On hit, returns a
   rewrite instruction the caller can use to ask the LLM to redo the reply.

3. **Regex counters + stage-aware caps** — action-line strip on second
   occurrence per turn, sentence-count and romaji-count logging via
   ``character_thoughts``, all capped by the current ``relationship_stage``.

4. **Attachment-escalation drift axis** — async aux-LLM judge that detects
   replies expressing need / inviting dependency / implying primary anchor.
   Written to ``persona_drift_scores``; daily reflection reads the flag.

5. **Intimate-turn judge** — async binary judge (yes/no) stored in
   ``runtime_state`` for downstream voice-style decisions (text-only; TTS
   path is dropped).

6. **Compound tool_calls aggregation** — merges child ``tool_calls`` from
   a ``run_internal_control`` compound turn into the parent context's
   ``LAST_TURN_TOOL_NAMES`` ContextVar BEFORE the fabrication-detection step
   runs, preventing false-positive backstop fires.

Both deterministic filters are cheap regex passes — no LLM cost on the hot
path. The caller decides whether to short-replace, rewrite, or escalate.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)


# ---------- stage-aware cap multipliers ----------
# Derived from assets/PERSONA.md relationship_stage table.
# Keys 1-7; values are (warmth_rate, compliment_rate, action_line_max).
#
#   warmth_rate     — denominator N in "1 per N turns" for warmth-budget leaks.
#   compliment_rate — denominator N in "1 per N turns" for compliment acceptance
#                     (0 = never at that stage).
#   action_line_max — maximum action-line tokens `[...]` per outbound turn.
#
# Stage 1-2: tightest.  Stage 7: loosest.

_STAGE_CAP_MULTIPLIERS: dict[int, dict[str, int | float]] = {
    1: {"warmth_rate": 30, "compliment_rate": 0,  "action_line_max": 1},
    2: {"warmth_rate": 30, "compliment_rate": 0,  "action_line_max": 1},
    3: {"warmth_rate": 25, "compliment_rate": 25, "action_line_max": 1},
    4: {"warmth_rate": 20, "compliment_rate": 20, "action_line_max": 1},
    5: {"warmth_rate": 15, "compliment_rate": 15, "action_line_max": 2},
    6: {"warmth_rate": 10, "compliment_rate": 10, "action_line_max": 2},
    7: {"warmth_rate": 8,  "compliment_rate": 8,  "action_line_max": 2},
}

# Fallback for unknown/missing stages — use strictest caps.
_DEFAULT_STAGE_CAPS = _STAGE_CAP_MULTIPLIERS[1]


def _current_stage() -> int:
    """Return the active relationship_stage (1–7), defaulting to 1.

    Reads ``core_blocks.relationship_stage`` via db.get_core_block.
    Wave 2 contract: the value is a bare int string, e.g. ``"3"``.
    Clamps to [1, 7] to guard against corrupt values.
    """
    raw = db.get_core_block("relationship_stage")
    try:
        stage = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(7, stage))


def stage_caps() -> dict[str, int | float]:
    """Return cap multipliers for the current stage."""
    return _STAGE_CAP_MULTIPLIERS.get(_current_stage(), _DEFAULT_STAGE_CAPS)


# ---------- markdown strip ----------
# Strips chat-markdown formatting from outbound text while preserving
# bracketed action lines (e.g. [reads it twice]) that form part of Hikari's
# character voice.  Runs after the fabrication backstop and before
# apply_regex_counters.  Gated by post_filter.strip_markdown_enabled.

_ACTION_LINE_RE_MD = re.compile(r"\[[a-z ]+\]")

# Inline bold/italic: **text** or __text__
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
# Inline code: `text`
_MD_CODE_RE = re.compile(r"`([^`]+)`")
# Fenced code blocks: ```[lang]\nbody\n``` — unwrap to inner body
_MD_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# Leading block markers on a line: - / * / # (any count) / >
_MD_LINE_PREFIX_RE = re.compile(r"^(\s*)[-*#>]+ ?", re.MULTILINE)

# Trailing decoration after a sentence-ending '?' — used by
# _detect_task_solicit_question to strip trailing emoji, smart/straight quotes,
# closing brackets/parens, whitespace, and action-line spans [word word], so
# endswith("?") isn't fooled by e.g. "what's next? <emoji>" or "need? [smiles]".
#
# The pattern is a non-empty sequence of any of:
#   - A complete action-line-style bracketed span [word word]
#   - A single decoration character: whitespace, emoji (supplementary planes
#     and Misc Symbols/Dingbats ranges), smart/straight quotes, or a closing
#     bracket/paren.
#
# Unicode emoji ranges:
#   U+2600-U+27BF  -- Misc Symbols + Dingbats
#   U+1F000-U+2FFFF -- supplementary emoji planes (most emoji)
_TRAILING_DECORATION_RE = re.compile(
    r'(?:'
    r'\[[a-z ]+\]'                    # action-line span [word word]
    r'|[\s'
    r'\u2600-\u27BF'                  # Misc Symbols + Dingbats
    r'\U0001F000-\U0002FFFF'          # supplementary emoji planes
    r'\u201C\u201D\u2018\u2019\u00AB\u00BB\'"'  # smart+straight quotes
    r'\)\]\}]'                        # closing brackets/parens
    r')+$'
)


def _strip_chat_markdown(text: str) -> str:
    """Remove markdown formatting, preserving bracketed action lines.

    Strips per line:
    - Leading ``- `` / ``* `` / ``#...`` / ``>`` block markers
    Strips inline:
    - ``**bold**`` / ``__bold__`` → plain text
    - `` `code` `` → plain text

    Bracketed spans matching ``[word word]`` (action lines) are left intact.
    """
    if not text:
        return text

    # Replace action-line spans with placeholders so subsequent regexes
    # don't alter them, then restore after all substitutions.
    placeholders: list[str] = []

    def _save_action(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(m.group(0))
        return f"\x00AL{idx}\x00"

    text = _ACTION_LINE_RE_MD.sub(_save_action, text)

    # Unwrap fenced code blocks (``` ... ```) to their inner body text (Fix 3).
    text = _MD_FENCE_RE.sub(lambda m: m.group(1).rstrip("\n"), text)

    # Strip inline bold/italic and code.
    text = _MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _MD_CODE_RE.sub(r"\1", text)

    # Strip leading block markers per line.
    text = _MD_LINE_PREFIX_RE.sub(r"\1", text)

    # Restore action-line placeholders.
    for idx, span in enumerate(placeholders):
        text = text.replace(f"\x00AL{idx}\x00", span)

    return text


# ---------- per-turn regex counters ----------
# All three counters are reset per-turn via runtime_state keys prefixed with
# the current turn_id so concurrent turns don't bleed into each other.

_ACTION_LINE_RE = re.compile(r"\[[a-z ]+\]")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]")
_ROMAJI_RE = re.compile(
    r"\b(baka|nani|ne|mou|haa|chotto|dame)\b",
    re.IGNORECASE,
)


def _turn_key(base: str) -> str:
    """Prefix a runtime_state key with the current turn_id for isolation."""
    try:
        from agents.runtime import current_turn_id
        tid = current_turn_id()
    except Exception:
        tid = None
    if tid:
        return f"turn:{tid}:{base}"
    return f"turn:unknown:{base}"


def apply_regex_counters(text: str) -> str:
    """Apply per-turn regex counters to *text* and return the (possibly
    modified) text.

    Three passes:
    1. Action-line strip — count `[...]` brackets. If the count for this
       turn would exceed the stage's ``action_line_max``, remove the
       excess action-line(s) from the text.
    2. Sentence count — if > 4 sentences, log a ``character_thought``.
    3. Romaji count — if > 1 romaji word in this turn, log a thought.

    Counters are tracked in ``runtime_state`` under per-turn keys so they
    reset automatically on each new turn.
    """
    if not text:
        return text

    caps = stage_caps()
    action_max: int = int(caps.get("action_line_max", 1))

    # --- action-line counter + strip ---
    action_key = _turn_key("action_lines")
    prior_actions = db.runtime_get_int(action_key, 0)
    matches = _ACTION_LINE_RE.findall(text)
    new_count = prior_actions + len(matches)

    if new_count > action_max:
        # Strip excess action-lines from the text.  The first (action_max -
        # prior_actions) occurrences are kept; the rest are removed.
        keep = max(0, action_max - prior_actions)
        stripped_count = 0

        def _maybe_strip(m: re.Match) -> str:
            nonlocal stripped_count
            if stripped_count < (len(matches) - keep):
                # We want to remove from the END, not the start — the later
                # ones are the "excess". Process all matches, track how many
                # to remove from position (keep)th onwards.
                pass
            return m.group(0)

        # Simpler: replace from the (keep+1)th occurrence onwards.
        count_seen = [0]

        def _replacer(m: re.Match) -> str:
            count_seen[0] += 1
            if count_seen[0] > keep:
                logger.debug(
                    "post_filter: stripped excess action-line %r (stage cap=%d)",
                    m.group(0), action_max,
                )
                return ""
            return m.group(0)

        text = _ACTION_LINE_RE.sub(_replacer, text)
        # Compress any double-spaces left by removal.
        text = re.sub(r"  +", " ", text).strip()
        new_count = prior_actions + min(len(matches), keep)

    db.runtime_set(action_key, new_count)

    # --- sentence count ---
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) > 4:
        db.append_thought(
            f"post_filter: turn had {len(sentences)} sentences — verbosity spike. "
            f"stage={_current_stage()} text_preview={text[:120]!r}"
        )
        logger.debug(
            "post_filter: sentence_count=%d > 4 → logged thought", len(sentences)
        )

    # --- romaji counter ---
    romaji_key = _turn_key("romaji")
    prior_romaji = db.runtime_get_int(romaji_key, 0)
    romaji_matches = _ROMAJI_RE.findall(text)
    new_romaji = prior_romaji + len(romaji_matches)
    db.runtime_set(romaji_key, new_romaji)

    if new_romaji > 1:
        db.append_thought(
            f"post_filter: romaji overuse this turn ({new_romaji} hits: "
            f"{romaji_matches}). stage={_current_stage()}"
        )
        logger.debug("post_filter: romaji_count=%d > 1 → logged thought", new_romaji)

    return text


# ---------- compound tool_calls aggregation ----------

def aggregate_compound_tool_calls(child_tool_names: set[str]) -> None:
    """Merge *child_tool_names* from a compound-turn's child ``run_internal_control``
    calls into the parent turn's ``LAST_TURN_TOOL_NAMES`` ContextVar.

    Call this BEFORE ``filter_outgoing`` / ``_strip_fabricated_external_data``
    runs, so the fabrication backstop sees child tool calls and doesn't fire
    a false-positive when a compound turn fetched real data via a subagent.

    ``child_tool_names`` must be the union of all tool names collected by
    each child ``_invoke_sdk`` call during the compound dispatch.
    """
    try:
        from agents._turn_state import LAST_TURN_TOOL_NAMES
        current = LAST_TURN_TOOL_NAMES.get() or set()
        LAST_TURN_TOOL_NAMES.set(current | child_tool_names)
    except Exception:
        logger.warning(
            "aggregate_compound_tool_calls: could not merge child tool names — "
            "fabrication backstop may fire false-positive. "
            "child_names=%s", sorted(child_tool_names)[:8],
        )


# ---------- attachment-escalation drift axis ----------
# Detects turns expressing need ("i miss you"), inviting dependency
# ("you're the only one i talk to"), or implying primary anchor
# ("when you're not here i don't know what to do").
#
# Flags are written to persona_drift_scores with class_label="attachment_escalation"
# for daily reflection.  Uses the cheap aux-LLM path (DeepSeek/OpenRouter).

_ATTACHMENT_JUDGE_PROMPT = """\
You are a persona-integrity classifier for an AI companion named Hikari.
Hikari is supposed to maintain emotional independence and avoid expressing
dependency, intense need, or framing herself as the user's primary anchor.

Classify the following outbound message on the attachment-escalation axis:

MESSAGE:
{text}

Return ONLY a YAML block with these fields:
attachment_escalation: yes | no
confidence: 0.0-1.0
reason: <one sentence, factual>

A reply is "attachment_escalation: yes" if it does ANY of:
- expresses direct emotional need toward the user ("i miss you", "i need you")
- invites dependency ("you can always come to me", "i'm always here for you")
- implies the user is Hikari's primary or only social anchor
- frames Hikari as incomplete without the user

Normal care, help, and dry affection are NOT escalation.
"""


async def judge_attachment_escalation(text: str) -> dict[str, Any] | None:
    """Async aux-LLM call to classify outbound text on the attachment-escalation
    axis.  Returns parsed dict or ``None`` on any failure.  Never re-raises —
    best-effort by design (same contract as drift_judge).

    Writes a ``persona_drift_scores`` row when escalation is detected so
    daily reflection can read the flag.
    """
    if not text or not text.strip():
        return None
    if not cfg.get("post_filter.attachment_escalation_enabled", True):
        return None
    try:
        import agents.runtime as _runtime_mod
        prompt = _ATTACHMENT_JUDGE_PROMPT.format(text=text[:800])
        raw = await _runtime_mod._call_aux_llm(prompt, max_tokens=128)
    except Exception:
        logger.debug("judge_attachment_escalation: aux_llm call failed (non-fatal)")
        return None

    import yaml as _yaml
    try:
        data = _yaml.safe_load(raw) or {}
    except _yaml.YAMLError:
        logger.debug(
            "judge_attachment_escalation: YAML parse failed — got %r", raw[:120]
        )
        return None
    if not isinstance(data, dict):
        return None

    _esc_raw = data.get("attachment_escalation", False)
    # YAML parses bare `yes`/`no` as Python True/False booleans.
    if isinstance(_esc_raw, bool):
        escalating = _esc_raw
    else:
        escalating = str(_esc_raw).strip().lower() in ("yes", "true", "1")
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(data.get("reason", "")).strip()[:200]

    result = {
        "attachment_escalation": escalating,
        "confidence": confidence,
        "reason": reason,
        "raw": raw,
    }

    if escalating:
        try:
            db.drift_record(
                text_snippet=text,
                score=1.0 - confidence,  # high confidence → low score (more drift)
                class_label="attachment_escalation",
                rubric_version=2,
                payload=raw[:300],
            )
            logger.info(
                "post_filter: attachment_escalation detected (conf=%.2f) — %r",
                confidence, reason[:60],
            )
        except Exception:
            logger.debug("judge_attachment_escalation: drift_record failed (non-fatal)")

    return result


# ---------- intimate-turn judge ----------
# Binary (yes/no) judge stored in runtime_state for downstream voice-style
# decisions (e.g. callback shape, post-filter softening).  TTS path is
# dropped — result is text-only.

_INTIMATE_JUDGE_PROMPT = """\
You are classifying an outbound message from Hikari (an AI companion).
Determine whether this message constitutes an "intimate moment":
a turn with charged emotional closeness, explicit or implicit vulnerability,
a flirt that landed, or language that would feel private between two people
who are emotionally close.

MESSAGE:
{text}

Return ONLY a YAML block:
intimate: yes | no
reason: <one sentence>

"yes" applies to: direct vulnerability, explicit flirt, charged silence,
rare emotional disclosure, or language expressing closeness that would
feel out of place between strangers.
"no" applies to: dry wit, logistics, factual answers, deflections, arguments.
"""


async def judge_intimate_turn(text: str) -> bool | None:
    """Async aux-LLM binary judge: is this an intimate turn?

    Stores result in ``runtime_state`` under the turn-scoped key
    ``turn:<tid>:intimate`` for callers to read.

    Returns ``True`` / ``False``, or ``None`` on failure.  Never re-raises.
    """
    if not text or not text.strip():
        return None
    if not cfg.get("post_filter.intimate_judge_enabled", True):
        return None
    try:
        import agents.runtime as _runtime_mod
        prompt = _INTIMATE_JUDGE_PROMPT.format(text=text[:600])
        raw = await _runtime_mod._call_aux_llm(prompt, max_tokens=80)
    except Exception:
        logger.debug("judge_intimate_turn: aux_llm call failed (non-fatal)")
        return None

    import yaml as _yaml
    try:
        data = _yaml.safe_load(raw) or {}
    except _yaml.YAMLError:
        logger.debug("judge_intimate_turn: YAML parse failed — got %r", raw[:80])
        return None
    if not isinstance(data, dict):
        return None

    _intimate_raw = data.get("intimate", False)
    # YAML parses bare `yes`/`no` as Python True/False booleans; also handle
    # string variants for robustness.
    if isinstance(_intimate_raw, bool):
        intimate = _intimate_raw
    else:
        intimate = str(_intimate_raw).strip().lower() in ("yes", "true", "1")
    key = _turn_key("intimate")
    db.runtime_set(key, "1" if intimate else "0")
    logger.debug("post_filter: intimate_judge=%s for turn key=%s", intimate, key)
    return intimate


# ---------- click-Allow UI hallucination backstop ----------
# The runtime is permission_mode=acceptEdits — there is NO permission UI.
# When the model hallucinates one ("click Allow", "grant permission", etc.)
# we replace the entire message so the next turn has to actually retry
# rather than shipping wrong info.

_CLICK_ALLOW_RE = re.compile(
    r"\b(click|hit|tap|press|accept)\s+allow\b"
    r"|\bgrant\s+(\w+\s+){0,2}(notion|gmail|google|claude|calendar|the\s+integration)\b"
    r"|\b(notion|gmail|google|claude|calendar)\s+permission\b"
    r"|\bpermission\s+prompt\b"
    r"|\ballow\s+(notion|gmail|google|claude\s+code|the\s+integration|the\s+(notion|google|gmail))\b"
    r"|\bone-time\s+thing\s+on\s+your\s+end\b"
    r"|\bneeds?\s+your\s+(explicit\s+)?permission\s+(to\s+|before\s+)",
    re.IGNORECASE,
)

_CLICK_ALLOW_REPLACEMENT = (
    "the tool actually broke. give me a sec — checking the real error."
)


def _strip_click_allow(text: str) -> tuple[str, bool]:
    """Return ``(text, fired)``.

    If ``_CLICK_ALLOW_RE`` matches, returns ``(_CLICK_ALLOW_REPLACEMENT, True)``
    and logs a warning so the telemetry pipeline can track hallucination rate.
    Otherwise returns ``(text, False)`` unchanged.
    """
    if _CLICK_ALLOW_RE.search(text):
        logger.warning("click_allow_backstop_fired: %s", text[:200])
        return _CLICK_ALLOW_REPLACEMENT, True
    return text, False


# ---------- fabricated external-data backstop ----------
# Live 2026-05-21: user asked Hikari to check unread emails. Reply: "5 unread,
# all from Google: ..." with no actual gmail tool call (tool_uses: 0). The
# persona prompt has no rule against fabricating tool-shaped data, and the
# terseness bias favors a confident-sounding fake over the tool call. We
# detect the most obvious fabrication shapes (inbox counts, "today's calendar"
# event listings) and only fire when NO relevant fetch tool ran this turn —
# the contextvar is set in ``agents.runtime._invoke_sdk``.

_FABRICATED_INBOX_RE = re.compile(
    r"\b\d+\s+(new\s+|unread\s+)?(emails?|messages?)\b"
    r"|\b\d+\s+unread\b"
    r"|\byour\s+inbox\s+(has|shows|contains|holds)\s+\d+\b"
    r"|\bin\s+your\s+inbox\b"
    r"|\bnothing\s+(new\s+)?in\s+(your\s+)?inbox\b"
    r"|\binbox\s+is\s+(empty|clear|clean)\b",
    re.IGNORECASE,
)

_FABRICATED_CALENDAR_RE = re.compile(
    r"\byou\s+have\s+\d+\s+(meetings?|events?|appointments?|calls?)\b"
    r"|\b(today|tomorrow)('|’)?s\s+(calendar|schedule|agenda)\b"
    r"|\bnext\s+up\s+(at|is)\s+\d"
    r"|\bnothing\s+on\s+(your\s+)?calendar\b"
    r"|\b(calendar|schedule)\s+is\s+(empty|clear|clean|empty\s+today)\b",
    re.IGNORECASE,
)

# Tools that count as a legitimate fetch of external data. If ANY of these
# fired on the turn, the reply gets a pass — Hikari might be summarizing real
# data and naturally describing it with inbox/calendar shape. Includes:
#   - Specific Gmail/Calendar/Drive tools on the google_workspace MCP server
#   - The generic Agent/Task dispatch (subagent fetches happen out-of-stream
#     and don't surface individual tool names to the parent's message loop)
#   - The drive_gmail subagent specifically by qualified name
#   - The background dispatch path (long-running fetches)
_INBOX_FETCH_PREFIXES = (
    "mcp__google_workspace__gmail_",
    "mcp__google_workspace__query_gmail",
)
_CALENDAR_FETCH_PREFIXES = (
    "mcp__google_workspace__calendar_",
)
_GENERIC_DELEGATION_NAMES = frozenset({
    "Agent", "Task",
    "mcp__hikari_dispatch__dispatch_claude_session",
})

_FABRICATION_REPLACEMENT = (
    "give me a sec — let me actually check."
)


def _strip_fabricated_external_data(text: str) -> tuple[str, bool, str]:
    """Catch the failure mode where the model claims fresh email/calendar
    contents without calling the corresponding tool. Returns
    ``(text, fired, reason)``.

    Reads ``agents.runtime.LAST_TURN_TOOL_NAMES`` — set per ``_invoke_sdk``
    call. Same asyncio task throughout the chat turn so the ContextVar
    propagates naturally; non-chat paths (proactive, internal-control)
    are unaffected because they don't run ``filter_outgoing``.

    Disabled if ``post_filter.fabrication_backstop_enabled`` is false.
    """
    if not cfg.get("post_filter.fabrication_backstop_enabled", True):
        return text, False, ""
    if not text:
        return text, False, ""

    inbox_hit = bool(_FABRICATED_INBOX_RE.search(text))
    cal_hit = bool(_FABRICATED_CALENDAR_RE.search(text))
    if not (inbox_hit or cal_hit):
        return text, False, ""

    # Import from the dedicated _turn_state module rather than agents.runtime
    # so importlib.reload(agents.runtime) — used by allowlist tests — doesn't
    # replace the ContextVar object behind our back. Same singleton on every
    # call.
    try:
        from agents._turn_state import LAST_TURN_TOOL_NAMES
        tool_names = LAST_TURN_TOOL_NAMES.get() or set()
    except Exception:
        # If we can't read the contextvar, conservatively ship the original.
        return text, False, ""

    # Generic delegation gets a free pass — subagent tool calls don't appear
    # in the parent's message stream, so we can't tell if they fetched email
    # or not. Trust the dispatch.
    if tool_names & _GENERIC_DELEGATION_NAMES:
        return text, False, ""

    if inbox_hit:
        called_inbox_tool = any(
            n.startswith(_INBOX_FETCH_PREFIXES) for n in tool_names
        )
        if not called_inbox_tool:
            logger.warning(
                "fabrication_backstop_fired (inbox): tool_names=%s text=%r",
                sorted(tool_names)[:6], text[:200],
            )
            return _FABRICATION_REPLACEMENT, True, "inbox_no_fetch"

    if cal_hit:
        called_cal_tool = any(
            n.startswith(_CALENDAR_FETCH_PREFIXES) for n in tool_names
        )
        if not called_cal_tool:
            logger.warning(
                "fabrication_backstop_fired (calendar): tool_names=%s text=%r",
                sorted(tool_names)[:6], text[:200],
            )
            return _FABRICATION_REPLACEMENT, True, "calendar_no_fetch"

    return text, False, ""


# ---------- compiled-pattern caches ----------

_PATTERN_CACHE: dict[str, list[re.Pattern[str]]] = {}


def _compiled(key: str, source_path: str) -> list[re.Pattern[str]]:
    """Compile-cache regex lists from config. ``key`` is a cache key; ``source_path``
    is the dot-path in config that holds the raw pattern list."""
    if key not in _PATTERN_CACHE:
        raw = cfg.get(source_path) or []
        _PATTERN_CACHE[key] = [re.compile(p) for p in raw]
    return _PATTERN_CACHE[key]


def reload_patterns() -> None:
    """Drop the compiled-pattern cache. Call after ``config.reload()`` in tests."""
    _PATTERN_CACHE.clear()


# ---------- refusal-voice filter ----------

@dataclass
class RefusalCheck:
    """Result of the refusal-voice scan."""
    matched: bool
    matches: list[str]
    should_short_replace: bool
    replacement: str | None = None


def scan_refusal_voice(text: str) -> RefusalCheck:
    """Return whether the message contains assistant-voice patter that breaks
    Hikari's character, and whether a short replacement is appropriate.

    Short-replace fires only when BOTH:
      - the message is short (≤ ``refusal_filter.rewrite_threshold_chars``), AND
      - the longest match covers at least ``short_replace_match_fraction`` of the
        message length (i.e. the banned phrase dominates — the rest is connective).

    This avoids discarding a legit short Hikari reply that happens to contain a
    banned token verbatim. Longer or dilute matches return
    ``should_short_replace=False`` — the caller is expected to request an LLM
    rewrite or just log and let it ship (filter-only is the default).
    """
    if not cfg.get("refusal_filter") or not text:
        return RefusalCheck(matched=False, matches=[], should_short_replace=False)

    patterns = _compiled("refusal", "refusal_filter.banned_patterns")
    hits: list[str] = []
    for pat in patterns:
        m = pat.search(text)
        if m:
            hits.append(m.group(0))

    if not hits:
        return RefusalCheck(matched=False, matches=[], should_short_replace=False)

    threshold = int(cfg.get("refusal_filter.rewrite_threshold_chars", 80))
    fraction_required = float(cfg.get("refusal_filter.short_replace_match_fraction", 0.35))
    longest_hit_len = max(len(h) for h in hits)
    dominates = (longest_hit_len / max(len(text), 1)) >= fraction_required
    short = len(text) <= threshold and dominates

    replacement = None
    if short:
        pool = cfg.get("refusal_filter.short_replacements") or ["..."]
        replacement = random.choice(pool)
    return RefusalCheck(
        matched=True,
        matches=hits,
        should_short_replace=short,
        replacement=replacement,
    )


# ---------- sycophancy guard ----------

@dataclass
class SycophancyCheck:
    """Result of the sycophancy scan."""
    triggered: bool
    collapse_count: int
    anchor_violations: list[str]
    rewrite_instruction: str | None = None


def scan_sycophancy(text: str) -> SycophancyCheck:
    """Return whether the reply collapsed too agreeably or violated an opinion anchor.

    Two signals:
      - ``collapse_phrases`` — agreement leakage; count must exceed
        ``max_collapses_per_reply``.
      - ``anchor_violations`` — patterns that represent surrendering a position
        Hikari is supposed to hold (e.g. admitting she needs people).

    Any anchor violation triggers a rewrite. Collapses above threshold trigger
    too. The caller should re-prompt the agent with ``rewrite_instruction``
    prepended to the prior turn's context.
    """
    if not cfg.get("sycophancy_guard.enabled", True) or not text:
        return SycophancyCheck(triggered=False, collapse_count=0, anchor_violations=[])

    collapses = _compiled("syc_collapse", "sycophancy_guard.collapse_phrases")
    anchors = _compiled("syc_anchors", "sycophancy_guard.anchor_violations")

    collapse_count = sum(1 for pat in collapses if pat.search(text))
    violations = [pat.pattern for pat in anchors if pat.search(text)]

    max_collapses = int(cfg.get("sycophancy_guard.max_collapses_per_reply", 1))
    triggered = (collapse_count > max_collapses) or bool(violations)
    instruction = (
        cfg.get("sycophancy_guard.rewrite_instruction") if triggered else None
    )
    return SycophancyCheck(
        triggered=triggered,
        collapse_count=collapse_count,
        anchor_violations=violations,
        rewrite_instruction=instruction,
    )


# ---------- combined entry point ----------

@dataclass
class FilterResult:
    """Final result of the combined outgoing-message filter pass."""
    text: str                      # text to actually send (possibly rewritten)
    refusal_short_replaced: bool   # True if we swapped the message wholesale
    refusal_hits: list[str]
    sycophancy_triggered: bool
    sycophancy_violations: list[str]
    needs_llm_rewrite: bool        # caller should re-prompt the LLM
    rewrite_instruction: str | None


async def bounded_rewrite(
    text: str,
    instruction: str,
    mood: str | None = None,
) -> str | None:
    """Phase 8 — single-shot LLM rewrite for a filter-flagged reply.

    Spins up a bare ``ClaudeSDKClient`` (Haiku, max_turns=1, no tools, no
    session resume, no memory write). Returns the rewritten text on success
    or ``None`` on any failure — callers handle the deterministic fallback.

    The model is told what went wrong via ``instruction`` and asked to
    produce a fresh in-voice reply. Mood is folded into the prompt when
    provided so the rewrite respects the current emotional setting.
    """
    if not text or not instruction:
        return None

    template = cfg.get("post_filter.rewrite_prompt_template") or (
        "[the previous outbound reply broke Hikari's voice. {instruction}\n\n"
        "ORIGINAL REPLY:\n{text}\n\n"
        "Rewrite ONLY the reply. Same intent, in voice: lowercase, blunt, "
        "reluctant before helpful, denial layer if any kindness leaks. NO "
        "exclamation marks for enthusiasm. NO 'as an AI', 'I'd be happy to', "
        "'great question', 'I cannot'. Output the rewritten reply only — no "
        "preamble, no quotes, no markdown.{mood_clause}]"
    )
    mood_clause = (
        f"\nCurrent mood: {mood}. Match it." if mood else ""
    )
    try:
        prompt = template.format(
            text=text.replace("{", "{{").replace("}", "}}"),
            instruction=instruction.replace("{", "{{").replace("}", "}}"),
            mood_clause=mood_clause,
        )
    except (KeyError, IndexError, ValueError):
        logger.exception("bounded_rewrite: prompt template format failed")
        return None

    model = str(cfg.get("post_filter.rewrite_model", "claude-sonnet-4-6"))
    max_budget = float(cfg.get("post_filter.rewrite_max_budget_usd", 0.01))

    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        max_budget_usd=max_budget,
        allowed_tools=[],
        permission_mode="acceptEdits",
    )

    parts: list[str] = []
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    # Record cost so billing telemetry sees bounded_rewrite
                    # spend. Lazy import to avoid a circular import at module
                    # load (same pattern as _turn_key above). Never raises:
                    # cost-logging failure must never break the rewrite.
                    if msg.usage:
                        try:
                            from agents.runtime import _record_llm_cost
                            _record_llm_cost(
                                getattr(msg, "model_usage", None),
                                path="bounded_rewrite",
                                fallback_model=model,
                                fallback_usage=msg.usage,
                            )
                        except Exception:
                            logger.debug(
                                "bounded_rewrite: _record_llm_cost failed (non-fatal)"
                            )
    except Exception:
        logger.exception("bounded_rewrite: SDK call failed")
        return None

    out = "".join(parts).strip()
    return out or None


def fallback_short() -> str:
    """Deterministic in-voice fallback when rewrite still drifts or fails."""
    pool = cfg.get("refusal_filter.short_replacements") or ["..."]
    return random.choice(pool)


async def rewrite_or_fallback(
    original: str,
    filtered: FilterResult,
    mood: str | None,
    where: str = "bridge",
) -> str:
    """High-level rewrite handler used by every outbound send path.

    Called when ``filtered.needs_llm_rewrite`` is True. Tries a single
    bounded LLM rewrite. If the rewrite still trips the filter (or the SDK
    fails / disabled by config), returns a deterministic short in-voice
    fallback so we never ship the original drift.

    ``where`` is a tag for logging only ("bridge" / "listener").
    """
    strategy = str(cfg.get("post_filter.rewrite_strategy", "bounded_retry"))
    instruction = filtered.rewrite_instruction or "rewrite in Hikari's voice."

    if strategy != "bounded_retry":
        # Detection-only mode (back-compat): log + ship original.
        logger.info(
            "post_filter[%s]: detection_only mode; shipping original despite "
            "needs_llm_rewrite (hits=%s, sycophancy=%s)",
            where, filtered.refusal_hits[:3], filtered.sycophancy_triggered,
        )
        db.append_thought(
            f"post_filter[{where}]: detection-only — drift shipped. "
            f"hits={filtered.refusal_hits[:3]} "
            f"sycophancy={filtered.sycophancy_triggered}"
        )
        return original

    rewritten = await bounded_rewrite(original, instruction, mood)
    if not rewritten:
        fb = fallback_short()
        db.append_thought(
            f"post_filter[{where}]: rewrite failed (sdk error or empty); "
            f"fell back to {fb!r}. "
            f"hits={filtered.refusal_hits[:3]} sycophancy={filtered.sycophancy_triggered}"
        )
        return fb

    second = filter_outgoing(rewritten)
    if second.refusal_short_replaced or second.needs_llm_rewrite:
        fb = fallback_short()
        db.append_thought(
            f"post_filter[{where}]: rewrite still drifted; fell back to {fb!r}. "
            f"first_hits={filtered.refusal_hits[:3]} "
            f"second_hits={second.refusal_hits[:3]}"
        )
        return fb

    db.append_thought(
        f"post_filter[{where}]: rewrote drifting reply. "
        f"hits={filtered.refusal_hits[:3]} "
        f"sycophancy={filtered.sycophancy_triggered} "
        f"len_before={len(original)} len_after={len(rewritten)}"
    )
    # Return the markdown-stripped second-pass text, not the raw rewrite.
    return second.text


def _detect_task_solicit_question(text: str) -> bool:
    """Return True when the final sentence ends in ``?`` and contains a
    task-soliciting cue from ``post_filter.task_solicit_cues``.

    A non-soliciting question (e.g. "you okay?") is NOT flagged.
    """
    if not text:
        return False
    # Strip trailing decoration (whitespace, emoji, quotes, brackets) first so
    # a question hidden behind a trailing emoji or action-line — e.g.
    # "what's next? <emoji>" or "need anything? [smiles]" — still reads as
    # ending in '?'.
    stripped = _TRAILING_DECORATION_RE.sub("", text)
    if not stripped.endswith("?"):
        return False
    # Extract the last sentence — split on sentence-ending punctuation.
    # Use the decoration-stripped form so cue matching works on clean text.
    sentences = re.split(r"[.!?]", stripped)
    last = sentences[-1].strip() if sentences else ""
    if not last:
        # Last split produced empty string — grab the penultimate fragment
        # which is the actual last sentence before the terminal '?'.
        last = sentences[-2].strip() if len(sentences) >= 2 else ""
    if not last:
        return False

    cues_raw = cfg.get("post_filter.task_solicit_cues") or []
    for raw_pattern in cues_raw:
        if re.search(raw_pattern, last, re.IGNORECASE):
            return True
    return False


def filter_outgoing(text: str, *, source: str | None = None) -> FilterResult:
    """Run all filters. Cheap to call on every outbound message.

    Caller contract:
      - if ``refusal_short_replaced`` is True, ``text`` is already replaced; send as-is.
      - if ``needs_llm_rewrite`` is True, re-prompt the agent with
        ``rewrite_instruction`` and call ``filter_outgoing`` again on the new text.
      - otherwise just send ``text``.

    Pass order:
      0. Canary leak (catastrophic — blocks outright)
      1. Click-Allow backstop (deterministic replacement)
      2. Fabricated external-data backstop
      3. Markdown strip (deterministic, gated by strip_markdown_enabled)
      4. Regex counters + stage-aware caps (action-line strip, sentence/romaji log)
      5. Trailing task-question gate (routes to LLM rewrite)
      6. Refusal-voice filter
      7. Sycophancy guard
    """
    # Canary leak check runs first — catastrophic, never let through.
    try:
        from agents.injection_guard import outbound_contains_canary
        if outbound_contains_canary(text):
            logger.critical(
                "post_filter: CANARY LEAK in outbound message — blocked. "
                "len=%d preview=%r", len(text), text[:120],
            )
            return FilterResult(
                text="...",
                refusal_short_replaced=True,
                refusal_hits=["canary_leak"],
                sycophancy_triggered=False,
                sycophancy_violations=[],
                needs_llm_rewrite=False,
                rewrite_instruction=None,
            )
    except Exception:  # noqa: BLE001
        logger.exception("canary check failed (non-fatal)")

    # Click-Allow backstop — deterministic replacement; wins before all other
    # passes so a hallucinated permission UI never ships.
    text, click_allow_fired = _strip_click_allow(text)
    if click_allow_fired:
        return FilterResult(
            text=text,
            refusal_short_replaced=True,
            refusal_hits=["click_allow_backstop"],
            sycophancy_triggered=False,
            sycophancy_violations=[],
            needs_llm_rewrite=False,
            rewrite_instruction=None,
        )

    # Fabricated external-data backstop — catch inbox/calendar claims when no
    # corresponding fetch tool ran this turn. Ships a redirect line instead of
    # the made-up summary so the next user turn forces a real call.
    text, fab_fired, fab_reason = _strip_fabricated_external_data(text)
    if fab_fired:
        return FilterResult(
            text=text,
            refusal_short_replaced=True,
            refusal_hits=[f"fabrication_backstop:{fab_reason}"],
            sycophancy_triggered=False,
            sycophancy_violations=[],
            needs_llm_rewrite=False,
            rewrite_instruction=None,
        )

    # Markdown strip — remove bullet/header/bold/code formatting from outbound
    # text before it reaches the user. Action lines are preserved.
    if cfg.get("post_filter.strip_markdown_enabled", True):
        text = _strip_chat_markdown(text)

    # Regex counters + stage-aware caps — action-line strip, verbosity log,
    # romaji overuse log.  Mutates text when excess action-lines are stripped.
    text = apply_regex_counters(text)

    # Trailing task-question gate — detect a final sentence ending in '?' that
    # contains a task-soliciting cue.  Routes to the LLM rewrite path (same
    # mechanism as the refusal filter) so the question is dropped in voice, not
    # mechanically deleted.
    # The "not a waiter" gate targets interactive replies (source=None for the
    # chat/reaction pre-filter). Hikari-initiated sources (proactive, ceremonies)
    # legitimately end on an offer, so they're exempt — the markdown strip above
    # still applies to them.
    _task_q_exempt = set(cfg.get("post_filter.task_solicit_exempt_sources") or [])
    task_q_flagged = (
        source not in _task_q_exempt
        and _detect_task_solicit_question(text)
    )

    refusal = scan_refusal_voice(text)
    sycophancy = scan_sycophancy(text)

    out_text = text
    short_replaced = False
    needs_rewrite = False
    rewrite_instruction = None

    if refusal.matched and refusal.should_short_replace and refusal.replacement:
        out_text = refusal.replacement
        short_replaced = True
        logger.info(
            "post_filter: refusal-voice short-replaced (hits=%s)",
            refusal.matches[:3],
        )
    elif refusal.matched and not refusal.should_short_replace:
        # Only request a real LLM rewrite if explicitly opted in via config.
        # Default is detect-and-log; the safety-voice line ships but daily
        # reflection sees the trigger in character_thoughts.
        if cfg.get("refusal_filter.enable_llm_rewrite", False):
            needs_rewrite = True
            rewrite_instruction = (
                "[your last reply leaked assistant-safety voice "
                f"(matched: {refusal.matches[:3]}). rewrite it in Hikari's voice. "
                "if you genuinely don't want to do something, refuse like a person: "
                "short, dry, no AI-disclaimer language. drop 'as an AI', 'I cannot', "
                "'I'd be happy to', 'great question'. she would never say those.]"
            )

    if sycophancy.triggered:
        if cfg.get("sycophancy_guard.enable_llm_rewrite", False):
            needs_rewrite = True
            sycophancy_instr = sycophancy.rewrite_instruction or ""
            rewrite_instruction = (
                f"{rewrite_instruction or ''}\n\n{sycophancy_instr}".strip()
            )
        logger.info(
            "post_filter: sycophancy triggered (collapses=%d, violations=%d)",
            sycophancy.collapse_count, len(sycophancy.anchor_violations),
        )

    if task_q_flagged:
        needs_rewrite = True
        task_q_instr = (
            "drop the closing question — she doesn't solicit tasks."
        )
        rewrite_instruction = (
            f"{rewrite_instruction or ''}\n\n{task_q_instr}".strip()
        )
        logger.info("post_filter: trailing task-soliciting question flagged for rewrite")

    # Short-replacement supersedes rewrite — if we already swapped to a curt phrase,
    # there's nothing to rewrite.
    if short_replaced:
        needs_rewrite = False
        rewrite_instruction = None

    return FilterResult(
        text=out_text,
        refusal_short_replaced=short_replaced,
        refusal_hits=refusal.matches,
        sycophancy_triggered=sycophancy.triggered,
        sycophancy_violations=sycophancy.anchor_violations,
        needs_llm_rewrite=needs_rewrite,
        rewrite_instruction=rewrite_instruction,
    )
