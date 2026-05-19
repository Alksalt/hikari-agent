"""Agent hooks. UserPromptSubmit injects always-on memory (core_blocks + open tasks)
into the agent's context window on every user turn. PostToolUseFailure logs failures
so silent breakage stops.

Retrieval is owned by the `recall` subagent (see agents/subagents.py) — Hikari
delegates to it on demand instead of paying a top-8 retrieval tax every turn. The
age-framing helpers (_frame_fact / _frame_episode) are still exported because the
recall-agent's prompt formatter can reuse them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from storage import db
from tools import location as location_mod

from . import affect as affect_mod
from . import config as cfg
from . import handoff as handoff_mod

logger = logging.getLogger(__name__)


def _format_core_blocks() -> str:
    blocks = db.all_core_blocks()
    if not blocks:
        return ""
    lines = ["# memory: core (always-on)"]
    for b in blocks:
        lines.append(f"## {b['label']}")
        lines.append(b["content"].strip())
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_open_tasks() -> str:
    tasks = db.open_tasks()
    if not tasks:
        return ""
    lines = ["# memory: open tasks / loops"]
    for t in tasks:
        due = f" (due {t['due_at']})" if t["due_at"] else ""
        status = t["status"]
        lines.append(f"- [#{t['id']} {status}{due}] {t['subject']}")
        if t.get("description"):
            lines.append(f"    {t['description']}")
    return "\n".join(lines)


def _format_lexicon() -> str:
    """Inject top lexicon entry as a private-language hint. Sparing — at most
    one per turn, gated by score threshold."""
    if not cfg.get("lexicon.enabled", True):
        return ""
    n = int(cfg.get("lexicon.inject_top_n_per_turn", 1))
    min_score = float(cfg.get("lexicon.inject_min_score", 0.30))
    half_life = float(cfg.get("lexicon.recency_half_life_days", 14))
    try:
        entries = db.lexicon_top(limit=n, half_life_days=half_life)
    except Exception:
        logger.exception("lexicon top failed")
        return ""
    eligible = [e for e in entries if float(e.get("score") or 0) >= min_score]
    if not eligible:
        return ""
    lines = ["# memory: shared lexicon (private phrases between you and them)"]
    for e in eligible:
        lines.append(f"- \"{e['phrase']}\" (source: {e['source']})")
    return "\n".join(lines)


def _format_session_handoff() -> str:
    data = handoff_mod.consume_handoff()
    if not data:
        return ""
    return handoff_mod.format_for_injection(data)


def _format_location() -> str:
    """User-shared location (with weather), deferred + freshness-gated."""
    try:
        return location_mod.format_for_injection()
    except Exception:
        logger.exception("location format failed")
        return ""


def _format_affect() -> str:
    """Emotional half-life — decayed intensity from a prior heavy moment."""
    return affect_mod.inject_affect_block()


def _format_observations() -> str:
    """Pattern observations (e.g. 'you always go quiet around 11pm')."""
    if not cfg.get("pattern_detection.enabled", True):
        return ""
    limit = int(cfg.get("pattern_detection.surface_max_per_session", 1))
    min_conf = float(cfg.get("pattern_detection.min_confidence", 0.6))
    re_surface_days = int(cfg.get("pattern_detection.re_surface_min_days", 7))
    try:
        rows = db.observations_unsurfaced(
            min_confidence=min_conf,
            limit=limit,
            re_surface_min_days=re_surface_days,
        )
    except Exception:
        logger.exception("observations read failed")
        return ""
    if not rows:
        return ""
    lines = ["# noticed patterns (you can raise these sideways, not as diagnoses)"]
    for r in rows:
        lines.append(f"- [{r['kind']}] {r['summary']}")
        # Mark as surfaced immediately so the next turn doesn't re-inject.
        try:
            db.observation_mark_surfaced(int(r["id"]))
        except Exception:
            logger.exception("observation mark_surfaced failed for id=%s", r.get("id"))
    return "\n".join(lines)


def _format_noticings() -> str:
    """Week-over-week noticings (e.g. 'you stopped mentioning the side project')."""
    if not cfg.get("noticings.enabled", True):
        return ""
    try:
        rows = db.noticings_unsurfaced(limit=1)
    except Exception:
        logger.exception("noticings read failed")
        return ""
    if not rows:
        return ""
    lines = ["# noticed changes about them (surface obliquely, not as a checkup)"]
    for r in rows:
        lines.append(f"- {r['summary']}")
        try:
            db.noticing_mark_surfaced(int(r["id"]))
        except Exception:
            logger.exception("noticing mark_surfaced failed for id=%s", r.get("id"))
    return "\n".join(lines)


def _days_since(iso: str) -> int | None:
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return max(0, (datetime.now(UTC) - ts).days)
    except (ValueError, TypeError):
        return None


def _frame_fact(text: str, iso: str) -> str:
    days = _days_since(iso)
    if days is None:
        return f"vague impression that: {text}"
    if days < 7:
        return f"she said recently: {text}"
    if days < 30:
        return f"she mentioned a while ago: {text}"
    return f"vague impression that: {text}"


def _frame_episode(text: str, iso: str) -> str:
    days = _days_since(iso)
    if days is None:
        return text
    if days == 0:
        suffix = "earlier today"
    elif days == 1:
        suffix = "yesterday"
    else:
        suffix = f"{days} days ago"
    return f"{text} ({suffix})"


async def inject_memory(
    input_data: dict[str, Any] | Any,
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """UserPromptSubmit hook — runs once per user turn before Claude sees the prompt."""
    user_prompt = ""
    if isinstance(input_data, dict):
        user_prompt = str(input_data.get("prompt") or input_data.get("user_prompt") or "")
    parts: list[str] = []
    try:
        block = _format_core_blocks()
        if block:
            parts.append(block)
        # Affect goes right after core_blocks so the "you're still in [state]"
        # framing has primacy over the other blocks. Hooks aren't strictly
        # ordered by Claude but earlier blocks read first.
        affect = _format_affect()
        if affect:
            parts.append(affect)
        tasks = _format_open_tasks()
        if tasks:
            parts.append(tasks)
        lex = _format_lexicon()
        if lex:
            parts.append(lex)
        loc = _format_location()
        if loc:
            parts.append(loc)
        obs = _format_observations()
        if obs:
            parts.append(obs)
        notc = _format_noticings()
        if notc:
            parts.append(notc)
        ho = _format_session_handoff()
        if ho:
            parts.append(ho)
        # Retrieval moved to the recall subagent — Hikari calls it on demand.
        _ = user_prompt
    except Exception:
        logger.exception("inject_memory hook failed; continuing with empty context")
        return {}

    if not parts:
        return {}

    additional = "\n\n".join(parts)
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional,
        }
    }


async def log_tool_failure(
    input_data: dict[str, Any] | Any,
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """PostToolUseFailure hook — surface failures instead of silently swallowing."""
    tool_name = ""
    error = ""
    if isinstance(input_data, dict):
        tool_name = str(input_data.get("tool_name") or "")
        error = str(input_data.get("error") or input_data.get("tool_response") or "")
    logger.warning("tool failure: tool=%s tool_use_id=%s error=%s",
                   tool_name, tool_use_id, error[:300])
    return {}


def _is_defer_gated(tool_name: str) -> bool:
    gated = cfg.get("approvals.defer_gated_tools") or []
    return tool_name in gated


def _tier_for_tool(tool_name: str) -> int:
    """Infer tier from config. Falls back to Tier-1 if not explicitly mapped.

    Tier-2 tools (irreversible side effects) are listed in
    ``approvals.tier_2_tools``. Everything else defaults to Tier-1.
    """
    tier_2 = cfg.get("approvals.tier_2_tools") or []
    return 2 if tool_name in tier_2 else 1


def _summary_for_defer(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Render a one-line human-readable summary of the pending tool call.
    The args are JSON-truncated; user sees enough to make an informed yes/no."""
    import json
    pretty = json.dumps(tool_input, ensure_ascii=False)
    if len(pretty) > 240:
        pretty = pretty[:237] + "..."
    return f"{tool_name}\nargs: {pretty}"


async def defer_gated_tools(
    input_data: dict[str, Any] | Any,
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """PreToolUse hook — if the tool is in ``approvals.defer_gated_tools``,
    persist a deferred-row + send the Telegram prompt + tell the SDK to halt.

    Resume happens in ``tools/approvals._resume_after_defer`` when the user
    replies. See plan ``linked-nibbling-shell.md`` Stage C for the full flow.
    """
    if not isinstance(input_data, dict):
        return {}
    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input") or {}
    sdk_tool_use_id = str(input_data.get("tool_use_id") or tool_use_id or "")

    if not tool_name or not _is_defer_gated(tool_name):
        return {}

    # Persist + prompt are best-effort; if either fails we still defer (we'd
    # rather lose a confirmation than autorun a gated tool).
    try:
        from tools import approvals as approval_tools
        tier = _tier_for_tool(tool_name)
        summary = _summary_for_defer(tool_name, tool_input)
        chat_id = approval_tools.owner_chat_id()
        approval_id = db.approval_create_deferred(
            chat_id=chat_id,
            tool_name=tool_name,
            tier=tier,
            summary=summary,
            args=tool_input if isinstance(tool_input, dict) else {},
            deferred_tool_use_id=sdk_tool_use_id,
            deferred_tool_input=tool_input if isinstance(tool_input, dict) else {},
        )
        logger.info(
            "defer_gated_tools: deferring %s (tool_use_id=%s, approval_id=%s)",
            tool_name, sdk_tool_use_id, approval_id,
        )
        # Best-effort out-of-band Telegram prompt. Wrap in try so the hook
        # still returns "defer" even if Telegram is unreachable.
        try:
            await approval_tools.send_defer_prompt(
                chat_id=chat_id, tier=tier, summary=summary,
            )
        except Exception:
            logger.exception("defer_gated_tools: prompt send failed (non-fatal)")
    except Exception:
        logger.exception(
            "defer_gated_tools: persistence failed; still deferring to halt run"
        )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "defer",
            "permissionDecisionReason": (
                f"{tool_name} requires owner approval (tier {tier})"
            ),
        }
    }
