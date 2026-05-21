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
import os
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from storage import db
from tools import location as location_mod

from . import affect as affect_mod
from . import config as cfg
from . import handoff as handoff_mod
from . import tool_inventory as tool_inventory_mod

logger = logging.getLogger(__name__)


# Set to the qualified tool name currently being resumed after CONFIRM-SEND
# approval. When set, ``defer_gated_tools`` skips the defer for that specific
# tool name only — other gated tools called during the same resume turn still
# defer normally. Set by ``tools.approvals._resume_after_defer`` when it falls
# back to identity (no separate ``_confirmed`` sibling exists for the gated
# tool). Without this, the resume turn would re-trigger the defer hook and
# loop forever.
IN_APPROVAL_RESUME_TOOL: ContextVar[str | None] = ContextVar(
    "hikari_in_approval_resume_tool", default=None,
)


def _resolve_local_tz_name() -> str:
    """Pick the local tz the model should reason about.

    Priority: explicit ``HOME_TZ`` env > ``scheduler.timezone`` config >
    Europe/Oslo as a last resort (matches the existing scheduler default).
    Location-coord-derived tz is intentionally NOT used here — adding a
    coords->tz lookup would mean a new dependency, and ``HOME_TZ`` covers
    the single-user case.
    """
    env_tz = (os.environ.get("HOME_TZ") or "").strip()
    if env_tz:
        return env_tz
    cfg_tz = cfg.get("scheduler.timezone")
    if cfg_tz:
        return str(cfg_tz)
    return "Europe/Oslo"


def _format_now() -> str:
    """Inject ``# now`` so the model can compute ISO timestamps for
    ``reminder_create`` from relative phrases ("in 1h", "через годину").

    Always present. Format mirrors the other ``# memory: …`` blocks but
    uses the shorter ``# now`` header — this block is small and
    high-priority enough to deserve a distinct top-level name.
    """
    now_utc = datetime.now(UTC)
    tz_name = _resolve_local_tz_name()
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name))
        local_line = f"local: {local.strftime('%Y-%m-%d %H:%M')} {tz_name}"
    except ZoneInfoNotFoundError:
        logger.warning("inject_memory: unknown tz %r — falling back to UTC", tz_name)
        local_line = f"local: (unknown tz {tz_name!r}, using UTC)"
    return (
        "# now\n"
        f"utc: {now_utc.isoformat(timespec='seconds')}\n"
        f"{local_line}"
    )


def _format_tools_available() -> str:
    try:
        return tool_inventory_mod.format_for_injection()
    except Exception:
        logger.exception("tool_inventory format failed")
        # The dynamic enumeration broke, but we still want the
        # no-allowlist footer present — that's the single line that
        # prevented the May 20 "blocked by allowlist" hallucination.
        # Silently dropping the whole block re-opens that surface.
        return (
            "# tools available\n"
            "(inventory render failed — see logs. note: there is no "
            "claude-code allowlist applying here — permission_mode=acceptEdits.)"
        )


def _format_core_blocks() -> str:
    """Dump the fast-path core_blocks (mood_today, preoccupation, weekly_consolidation).

    Phase 7: the legacy ``user_profile`` block is filtered out — its content
    has been migrated into the new ``peer_representation`` table (see
    ``_format_peer_representation``). Filtering here is defensive: even if
    a legacy ``user_profile`` row lingers, it doesn't double-inject.
    """
    blocks = db.all_core_blocks()
    if not blocks:
        return ""
    excluded_labels = {"user_profile"}
    blocks = [b for b in blocks if b["label"] not in excluded_labels]
    if not blocks:
        return ""
    lines = ["# memory: core (always-on)"]
    for b in blocks:
        lines.append(f"## {b['label']}")
        lines.append(b["content"].strip())
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_peer_representation() -> str:
    """Phase 7: structured user model. Replaces the flat ``user_profile``
    block with communication_style / values / domain_expertise /
    current_concerns / blindspots / summary."""
    try:
        from agents import peer_model
        model = db.get_peer_representation()
    except Exception:
        logger.exception("peer_representation read failed")
        return ""
    if not model:
        return ""
    return peer_model.format_for_injection(model)


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
    """Pattern observations (e.g. 'you always go quiet around 11pm').

    Phase 13 (Stream C): no longer marks rows surfaced inline. The injected
    IDs are stashed in ``runtime_state`` under
    ``pending_surfaced_observation_ids`` and the bridge calls
    ``agents.postsend.mark_pending_surfaced()`` only after Telegram
    delivery + DB append succeed. Codex P2 fix: observations no longer
    disappear after being offered to the model if the reply never lands.
    """
    import json as _json
    # Always clear any stale pending IDs from a prior turn so this turn's
    # set is authoritative — even when there's nothing fresh to inject, the
    # previous turn's IDs should not bleed into the next post-send pass.
    db.runtime_set("pending_surfaced_observation_ids", None)
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
    ids: list[int] = []
    for r in rows:
        lines.append(f"- [{r['kind']}] {r['summary']}")
        try:
            ids.append(int(r["id"]))
        except (TypeError, ValueError):
            continue
    if ids:
        db.runtime_set(
            "pending_surfaced_observation_ids",
            _json.dumps(ids),
        )
    return "\n".join(lines)


def _format_noticings() -> str:
    """Week-over-week noticings (e.g. 'you stopped mentioning the side project').

    Phase 13 (Stream C): same deferred-marking pattern as
    ``_format_observations``. IDs are stashed under
    ``pending_surfaced_noticing_ids`` and committed by ``postsend.mark_pending_surfaced``
    after a successful send.
    """
    import json as _json
    db.runtime_set("pending_surfaced_noticing_ids", None)
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
    ids: list[int] = []
    for r in rows:
        lines.append(f"- {r['summary']}")
        try:
            ids.append(int(r["id"]))
        except (TypeError, ValueError):
            continue
    if ids:
        db.runtime_set(
            "pending_surfaced_noticing_ids",
            _json.dumps(ids),
        )
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
        # `# now` goes first so the model has a clock for reminder_create
        # and any other time-relative reasoning.
        now_block = _format_now()
        if now_block:
            parts.append(now_block)
        block = _format_core_blocks()
        if block:
            parts.append(block)
        # Phase 7: structured peer model goes right after the fast-path
        # core_blocks (mood, preoccupation). It's the "who they are" frame
        # Hikari needs before everything else.
        peer = _format_peer_representation()
        if peer:
            parts.append(peer)
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
        # Live tool surface — kept at the bottom so it doesn't crowd
        # higher-priority blocks, but always present so Hikari stops
        # confabulating her capabilities.
        tools_block = _format_tools_available()
        if tools_block:
            parts.append(tools_block)
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


def _is_defer_gated(tool_name: str, tool_input: dict[str, Any] | None = None) -> bool:
    """Decide whether a tool call must be deferred for owner approval.

    Phase 8: ``defer_gated_tools`` entries are *regex patterns* matched against
    the full qualified tool name. A match always defers unless the tool has a
    per-arg condition in ``defer_when_args_match`` — in which case the call
    only defers when the named arg contains one of the configured needles.

    Returns True iff the call should be deferred.
    """
    gated = cfg.get("approvals.defer_gated_tools") or []
    if not gated:
        return False

    matched_pattern: str | None = None
    for pat in gated:
        try:
            if re.fullmatch(str(pat), tool_name):
                matched_pattern = str(pat)
                break
        except re.error:
            logger.warning("defer_gated_tools: invalid regex %r", pat)
            continue

    if matched_pattern is None:
        return False

    arg_specs = cfg.get("approvals.defer_when_args_match") or {}
    # Phase 8 / review-H1: arg-spec keys are matched against the tool name with
    # the SAME regex semantics as ``defer_gated_tools``, so a wildcard pattern
    # like ``^mcp__hikari_dispatch__.*$`` in the gated list still finds its
    # condition spec under a key that matches that name. Exact-string keys
    # remain valid (they're trivially regex-valid).
    spec = None
    for key, candidate in arg_specs.items():
        try:
            if re.fullmatch(str(key), tool_name):
                spec = candidate
                break
        except re.error:
            logger.warning("defer_when_args_match: invalid regex key %r", key)
            continue
    if not spec:
        return True  # unconditional defer

    if not isinstance(tool_input, dict):
        return True  # be conservative when args are missing

    key = str(spec.get("key") or "")
    needles_raw = spec.get("contains_any") or []
    case_insensitive = bool(spec.get("case_insensitive", True))
    if not key or not needles_raw:
        return True

    raw_val = tool_input.get(key)
    haystack = str(raw_val) if raw_val is not None else ""
    if case_insensitive:
        haystack = haystack.lower()
        needles = [str(n).lower() for n in needles_raw]
    else:
        needles = [str(n) for n in needles_raw]

    return any(n and n in haystack for n in needles)


def _tier_for_tool(tool_name: str) -> int:  # noqa: ARG001
    """Phase 8: single-tier model. Everything that defers uses tier 2
    (CONFIRM-SEND). Kept as a function for backwards compatibility with the
    legacy approval row schema."""
    return 2


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

    # Approval-resume bypass: when ``tools.approvals._resume_after_defer`` is
    # running a fresh turn to execute the approved tool by its original name
    # (identity fallback for tools without a separate ``_confirmed`` sibling),
    # the hook would otherwise re-defer the same tool and the resume would
    # loop. Bypass is scoped to the exact tool name — any *other* gated tool
    # the model decides to call during the resume turn still defers normally.
    if tool_name and IN_APPROVAL_RESUME_TOOL.get() == tool_name:
        logger.info(
            "defer_gated_tools: bypassing defer for %s (approved resume)",
            tool_name,
        )
        return {}

    input_dict = tool_input if isinstance(tool_input, dict) else {}
    if not tool_name or not _is_defer_gated(tool_name, input_dict):
        return {}

    # Persist + prompt are best-effort; if either fails we still defer (we'd
    # rather lose a confirmation than autorun a gated tool).
    try:
        import asyncio as _asyncio

        from tools import approvals as approval_tools
        tier = _tier_for_tool(tool_name)
        summary = _summary_for_defer(tool_name, input_dict)
        chat_id = approval_tools.owner_chat_id()
        approval_id = db.approval_create_deferred(
            chat_id=chat_id,
            tool_name=tool_name,
            tier=tier,
            summary=summary,
            args=input_dict,
            deferred_tool_use_id=sdk_tool_use_id,
            deferred_tool_input=input_dict,
        )
        logger.info(
            "defer_gated_tools: deferring %s (tool_use_id=%s, approval_id=%s)",
            tool_name, sdk_tool_use_id, approval_id,
        )
        # Phase 8 — Codex P0 fix: the defer path now schedules its own timeout
        # watcher (previously only the legacy in-process callback path did,
        # so the "60s" copy in the prompt was a lie for SDK defer).
        _asyncio.create_task(
            approval_tools._timeout_watcher(approval_id, chat_id)
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
