"""Out-of-band tiered approval framework.

Pattern: gated tools call `request_approval(...)` which inserts an approval row,
sends an in-voice prompt to the user via Telegram, registers an on-approve callback,
and **returns immediately** to the LLM with a "queued for approval" message. The
agent can move on or react. When the user replies (`y` for Tier-1, `CONFIRM-SEND`
for Tier-2), the bridge calls `resolve_pending_approval(...)` which runs the
captured callback and writes an audit row.

Tiers:
  - Tier 1: 'y' / 'yes' to confirm. For: wiki_append, notion_create_*, draft email,
    any reversible write.
  - Tier 2: typed phrase 'CONFIRM-SEND'. For: gmail_send, calendar_create_event
    with attendees, git push, irreversible operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agents import config as cfg
from storage import db

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

# Bridge sets this in post_init so tools can access it without circular imports.
_BOT_REF: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _BOT_REF
    _BOT_REF = bot


def _bot() -> Bot:
    if _BOT_REF is None:
        raise RuntimeError("approvals.set_bot() not called; bridge not started?")
    return _BOT_REF


def owner_chat_id() -> int:
    """Shared accessor for the owner's Telegram chat_id (single-user bot)."""
    raw = os.environ.get("OWNER_TELEGRAM_ID")
    if not raw:
        raise RuntimeError("OWNER_TELEGRAM_ID not set")
    return int(raw)


# approval_id -> (event, on_approve callback, captured args)
PENDING_CALLBACKS: dict[int, tuple[asyncio.Event,
                                   Callable[[dict[str, Any]], Awaitable[Any]],
                                   dict[str, Any]]] = {}


def _timeout_sec() -> int:
    return int(cfg.get("approvals.timeout_sec", 60))


def _tier_1_hint() -> str:
    return f"reply `y` to confirm, anything else to skip. {_timeout_sec()}s."


def _tier_2_hint() -> str:
    phrase = cfg.get("approvals.tier_2_phrase", "CONFIRM-SEND")
    return f"type {phrase} exactly to send. {_timeout_sec()}s."


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


async def _safe_send(chat_id: int, text: str) -> None:
    """Send a Telegram message through the canary-leak filter. Approval-side
    sends previously bypassed ``filter_outgoing`` and could ship a canary if
    the LLM was injected into producing one in an approval summary."""
    from agents.post_filter import filter_outgoing
    filtered = filter_outgoing(text)
    to_send = filtered.text
    if filtered.refusal_short_replaced and filtered.refusal_hits == ["canary_leak"]:
        logger.critical(
            "approvals: blocked outbound containing canary leak (aid path)"
        )
    try:
        await _bot().send_message(chat_id=chat_id, text=to_send)
    except Exception:
        logger.exception("approval send_message failed")


async def send_defer_prompt(chat_id: int, tier: int, summary: str) -> None:
    """Compose + send the user-facing approval prompt for a defer event.

    Called from the PreToolUse defer hook in ``agents/hooks.py``. Mirrors
    the prompt style of the legacy ``request_approval`` path so users see a
    consistent experience whether a tool was deferred via SDK or via the
    old in-process callback pattern.
    """
    hint = _tier_2_hint() if tier == 2 else _tier_1_hint()
    prompt = f"⏸️  {summary}\n\n{hint}"
    await _safe_send(chat_id, prompt)


async def request_approval(
    *,
    chat_id: int,
    tool_name: str,
    tier: int,
    summary: str,
    args: dict[str, Any],
    on_approve: Callable[[dict[str, Any]], Awaitable[Any]],
) -> dict[str, Any]:
    """Queue an approval request. Returns a result dict for the calling tool."""
    if tier not in (1, 2):
        raise ValueError(f"tier must be 1 or 2, got {tier}")
    if db.approval_pending_for(chat_id):
        return _ok(
            "approval queue is busy — there's already one waiting. "
            "ask the user to resolve that first, then retry."
        )

    aid = db.approval_create(chat_id, tool_name, tier, summary, args)
    hint = _tier_2_hint() if tier == 2 else _tier_1_hint()
    prompt = f"⏸️  {summary}\n\n{hint}"
    await _safe_send(chat_id, prompt)

    PENDING_CALLBACKS[aid] = (asyncio.Event(), on_approve, args)
    asyncio.create_task(_timeout_watcher(aid, chat_id))

    return _ok(
        f"approval queued (id {aid}, tier {tier}). user has {_timeout_sec()}s. "
        f"don't repeat the summary back to them — they just saw it. "
        f"acknowledge briefly in voice if you want, then move on."
    )


async def _timeout_watcher(aid: int, chat_id: int) -> None:
    await asyncio.sleep(_timeout_sec())
    # Still pending?
    pending = db.approval_pending_for(chat_id)
    if pending and int(pending["id"]) == aid:
        db.approval_resolve(aid, "timeout")
        PENDING_CALLBACKS.pop(aid, None)
        await _safe_send(chat_id, f"⌛ approval {aid} timed out. didn't do it.")


async def resolve_pending_approval(chat_id: int, text: str) -> bool:
    """Bridge calls this on every inbound text message BEFORE routing to respond().

    Returns True if the message was consumed (i.e. the user replied to a pending
    approval) — in which case the bridge should NOT pass it on to the agent.

    Routes to one of two resume paths depending on the row shape:
    - Legacy (PENDING_CALLBACKS callback): runs ``_run_approval``.
    - Phase-6 SDK defer (``deferred_tool_use_id`` set): runs
      ``_resume_after_defer`` which fires a fresh ``_run_query``.
    """
    pending = db.approval_pending_for(chat_id)
    if not pending:
        return False
    aid = int(pending["id"])
    tier = int(pending["tier"])
    text_clean = text.strip()

    # Coerce to str defensively — YAML can parse bare yes/no as bool.
    tier_1_phrases = [
        str(p).lower() for p in cfg.get("approvals.tier_1_phrases", []) or []
    ]
    reject_phrases = [
        str(p).lower() for p in cfg.get("approvals.reject_phrases", []) or []
    ]
    tier_2_phrase = str(cfg.get("approvals.tier_2_phrase", "CONFIRM-SEND"))
    lower = text_clean.lower()

    if tier == 1:
        approved = lower in tier_1_phrases
        rejected = lower in reject_phrases
    elif tier == 2:
        approved = text_clean == tier_2_phrase
        rejected = lower in reject_phrases
    else:
        return False

    if approved:
        # Route by row shape — defer rows have a deferred_tool_use_id; legacy
        # rows don't (and have a callback in PENDING_CALLBACKS).
        if pending.get("deferred_tool_use_id"):
            return await _resume_after_defer(aid, pending)
        return await _run_approval(aid, pending)
    if rejected:
        db.approval_resolve(aid, "rejected")
        PENDING_CALLBACKS.pop(aid, None)
        await _safe_send(chat_id, "ok. didn't do it.")
        return True

    # Not a match — let the message route normally. Approval stays pending.
    return False


async def _run_approval(aid: int, pending: dict[str, Any]) -> bool:
    from agents.injection_guard import flag_args_with_untrusted_content

    db.approval_resolve(aid, "approved")
    entry = PENDING_CALLBACKS.pop(aid, None)
    chat_id = int(pending["chat_id"])

    if not entry:
        # Callback already gone (race?) — just ack.
        await _safe_send(chat_id, f"approval {aid} ok but no callback.")
        return True

    _, callback, args = entry
    try:
        result = await callback(args)
    except Exception as e:
        logger.exception("approval callback failed for aid=%s", aid)
        await _safe_send(chat_id, f"approval ran but the tool fell over: {e}")
        return True

    # Flag tool calls whose args contain untrusted-origin content (canary token,
    # known untrusted URLs). Recorded in the audit log even if approved.
    flag, reason = flag_args_with_untrusted_content(args)
    audit_summary = (str(result) if result is not None else "")[:500]
    if flag:
        audit_summary = f"[UNTRUSTED:{reason}] {audit_summary}"
        logger.warning(
            "approval %s: args flagged untrusted-origin (%s)", aid, reason,
        )

    db.audit_append(
        tool=pending["tool_name"],
        args_json_redacted=_redact(pending["args_json"])[:500],
        result_summary=audit_summary,
        approved_by="owner",
    )

    if result is not None:
        await _safe_send(chat_id, str(result)[:1000])
    return True


async def _resume_after_defer(aid: int, pending: dict[str, Any]) -> bool:
    """Phase 6: resume a deferred SDK tool call after user approval.

    The PreToolUse hook halted the SDK with ``permissionDecision="defer"``;
    the SDK exited and the original ``_run_query`` returned. We resume by
    starting a *fresh* ``_run_query`` (same ``session_id``) with a synthetic
    system-prompt that tells Sonnet to call the post-approval sibling tool
    (e.g. ``wiki_append_confirmed``) with the captured args. The sibling tool
    is added to ``allowed_tools`` for just this turn via
    ``extra_allowed_tools``.

    The original ``deferred_tool_use_id`` is recorded in the audit log so the
    chain is traceable.
    """
    import json
    chat_id = int(pending["chat_id"])
    tool_name = str(pending.get("deferred_tool_name") or pending["tool_name"])
    tool_use_id = str(pending.get("deferred_tool_use_id") or "")
    try:
        tool_input = json.loads(pending.get("deferred_tool_input_json") or "{}")
    except (ValueError, TypeError):
        tool_input = {}

    # Look up the post-approval sibling tool from config. Note: the runtime
    # attaches the `hikari_wiki_confirmed` MCP server only when the resume
    # codepath passes the matching tool name via `extra_allowed_tools`, so the
    # name we read here must match the server-namespaced form.
    confirmed_map = cfg.get("approvals.defer_confirmed_tools") or {}
    confirmed_tool = confirmed_map.get(tool_name)
    if not confirmed_tool:
        db.approval_resolve(aid, "rejected")
        await _safe_send(
            chat_id,
            f"approval {aid}: no confirmed-tool mapping for {tool_name}. "
            "aborted.",
        )
        logger.error("resume_after_defer: missing defer_confirmed_tools entry "
                     "for %s", tool_name)
        return True

    template = cfg.get("approvals.defer_resume_prompt_template") or (
        "[system: the deferred tool call {tool_use_id} ({tool_name}) was "
        "approved by the owner. execute it now by calling {confirmed_tool} "
        "with these args: {tool_input}. do not ask for confirmation; do not "
        "paraphrase the args.]"
    )
    # JSON content can contain literal `{` and `}` which would crash
    # ``str.format``. Escape both before interpolation.
    tool_input_str = (
        json.dumps(tool_input, ensure_ascii=False)
        .replace("{", "{{")
        .replace("}", "}}")
    )
    try:
        prompt = template.format(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            confirmed_tool=confirmed_tool,
            tool_input=tool_input_str,
        )
    except (KeyError, IndexError, ValueError) as fmt_err:
        # Template references an unknown placeholder OR has its own raw braces.
        # Fall back to a hardcoded safe prompt rather than dropping the call.
        logger.warning(
            "resume_after_defer: prompt template format failed (%s); using fallback",
            fmt_err,
        )
        prompt = (
            f"[system: the deferred tool call {tool_use_id} ({tool_name}) was "
            f"approved by the owner. execute it now by calling {confirmed_tool} "
            f"with these args: {tool_input_str}. do not ask for confirmation; "
            f"do not paraphrase the args.]"
        )

    # Lazy import to avoid circular (agents.runtime imports tools.approvals
    # indirectly via hooks).
    from agents.runtime import _run_query

    # CRITICAL ORDERING: do NOT mark the approval resolved until _run_query
    # actually completes. If we marked early and execution failed, the row
    # would be unrecoverable (no longer 'pending' so restart-recovery skips it).
    reply: str | None = None
    try:
        reply = await _run_query(
            prompt,
            max_turns=5,
            max_budget_usd=0.30,
            log_to_memory=False,
            extra_allowed_tools=[confirmed_tool],
        )
    except Exception as e:
        logger.exception("resume_after_defer: _run_query failed for aid=%s", aid)
        await _safe_send(chat_id, f"approval ran but execution fell over: {e}")
        # Mark the row failed so restart-recovery doesn't loop forever on it,
        # but leave an audit trace explaining what happened.
        try:
            db.approval_resolve(aid, "rejected")
            db.audit_append(
                tool=tool_name,
                args_json_redacted=_redact(json.dumps(tool_input))[:500],
                result_summary=(
                    f"deferred->resume FAILED via {confirmed_tool} "
                    f"(tu={tool_use_id}): {e}"
                )[:500],
                approved_by="owner",
            )
        except Exception:
            logger.exception("resume_after_defer: cleanup writes failed")
        return True

    # Success path: only NOW mark approved + write the audit row.
    db.approval_resolve(aid, "approved")
    db.audit_append(
        tool=tool_name,
        args_json_redacted=_redact(json.dumps(tool_input))[:500],
        result_summary=f"deferred->resumed via {confirmed_tool} (tu={tool_use_id})",
        approved_by="owner",
    )
    if reply:
        await _safe_send(chat_id, reply[:1000])
    return True


_REDACT_PATTERNS = [
    (r"sk-[a-zA-Z0-9_-]{20,}", "[REDACTED-API-KEY]"),
    (r"ya29\.[a-zA-Z0-9_-]+", "[REDACTED-OAUTH-TOKEN]"),
    (r"Bearer [a-zA-Z0-9._-]+", "Bearer [REDACTED]"),
    # Emails are usually fine to keep in approvals but redact in audit
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}", "[REDACTED-EMAIL]"),
]


def _redact(text: str) -> str:
    import re
    out = text
    for pattern, replacement in _REDACT_PATTERNS:
        out = re.sub(pattern, replacement, out)
    return out


def _safe_args_dump(args: dict[str, Any]) -> str:
    """Dump args to JSON, redacting common secret patterns."""
    return _redact(json.dumps(args, default=str, ensure_ascii=False))
