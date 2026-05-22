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

Both filters are deterministic regex passes — no LLM cost on the hot path.
The caller decides whether to short-replace, rewrite, or escalate.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)


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

    model = str(cfg.get("post_filter.rewrite_model", "claude-haiku-4-5"))
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
    filtered: "FilterResult",
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
    return rewritten


def filter_outgoing(text: str) -> FilterResult:
    """Run all filters. Cheap to call on every outbound message.

    Caller contract:
      - if ``refusal_short_replaced`` is True, ``text`` is already replaced; send as-is.
      - if ``needs_llm_rewrite`` is True, re-prompt the agent with
        ``rewrite_instruction`` and call ``filter_outgoing`` again on the new text.
      - otherwise just send ``text``.
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
