"""Out-of-band approval framework — single-tier (Phase 8).

Pattern: gated tools call `request_approval(...)` which inserts an approval row,
sends a Telegram prompt, registers an on-approve callback, and returns
immediately with a "queued for approval" message. When the user replies
`CONFIRM-SEND`, the bridge calls `resolve_pending_approval(...)` which runs
the captured callback and writes an audit row.

Phase 8 collapsed the prior Tier-1 (`y/yes/ok/go`) path. Per user directive
"delete as much approval as possible", the gate is now reserved for
irreversible/outbound operations only — wiki/Notion/draft writes auto-run.
Everything that defers requires the explicit phrase `CONFIRM-SEND`.
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
from tools._response import ok as _ok

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


def _confirm_hint() -> str:
    """Phase 8: single hint format — typed-phrase confirmation only."""
    phrase = cfg.get("approvals.tier_2_phrase", "CONFIRM-SEND")
    return f"type {phrase} exactly to send. {_timeout_sec()}s."


# Backwards-compat alias for any legacy caller.
_tier_2_hint = _confirm_hint


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


async def send_defer_prompt(chat_id: int, tier: int, summary: str) -> None:  # noqa: ARG001
    """Compose + send the user-facing approval prompt for a defer event.

    Phase 8: tier is ignored (kept for callsite compatibility); the hint is
    always the typed-phrase form. Tier-1 (`y`) confirmation no longer exists.
    """
    prompt = f"⏸️  {summary}\n\n{_confirm_hint()}"
    await _safe_send(chat_id, prompt)


async def request_approval(
    *,
    chat_id: int,
    tool_name: str,
    tier: int = 2,  # noqa: ARG002 — accepted for backwards compatibility, ignored
    summary: str,
    args: dict[str, Any],
    on_approve: Callable[[dict[str, Any]], Awaitable[Any]],
) -> dict[str, Any]:
    """Queue an approval request. Returns a result dict for the calling tool.

    Phase 8: ``tier`` is accepted but ignored — every approval uses
    CONFIRM-SEND. The parameter is preserved so legacy callers don't break.
    """
    if db.approval_pending_for(chat_id):
        return _ok(
            "approval queue is busy — there's already one waiting. "
            "ask the user to resolve that first, then retry."
        )

    aid = db.approval_create(chat_id, tool_name, 2, summary, args)
    prompt = f"⏸️  {summary}\n\n{_confirm_hint()}"
    await _safe_send(chat_id, prompt)

    PENDING_CALLBACKS[aid] = (asyncio.Event(), on_approve, args)
    asyncio.create_task(_timeout_watcher(aid, chat_id))

    return _ok(
        f"approval queued (id {aid}). user has {_timeout_sec()}s to type "
        f"CONFIRM-SEND. don't repeat the summary back to them — they just "
        f"saw it. acknowledge briefly in voice if you want, then move on."
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
    text_clean = text.strip()

    # Phase 8: single-tier model. Confirmation is the explicit phrase only.
    # Reject phrases stay; everything else falls through to normal chat.
    reject_phrases = [
        str(p).lower() for p in cfg.get("approvals.reject_phrases", []) or []
    ]
    confirm_phrase = str(cfg.get("approvals.tier_2_phrase", "CONFIRM-SEND"))
    lower = text_clean.lower()

    approved = text_clean == confirm_phrase
    rejected = lower in reject_phrases

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
    """Phase 6/8: resume a deferred SDK tool call after user approval.

    The PreToolUse hook halted the SDK with ``permissionDecision="defer"``;
    the SDK exited and the original ``_run_query`` returned. We resume by
    starting a *fresh* ``_run_query`` (same ``session_id``) with a synthetic
    system-prompt that tells Sonnet to call the post-approval sibling tool
    (e.g. ``dispatch_claude_session_confirmed``) with the captured args. The
    sibling tool is added to ``allowed_tools`` for just this turn via
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

    # Look up the post-approval sibling tool from config. The dispatch case
    # uses a separate `*_confirmed` MCP server attached only during the resume
    # turn (via `extra_allowed_tools`). For all other gated tools (Gmail,
    # Drive, Notion, GitHub, python_run...) there is no separate sibling — the
    # post-approval action is to call the same tool by its original name. In
    # that case fall through to identity and rely on the `IN_APPROVAL_RESUME_TOOL`
    # contextvar (set below) to bypass the PreToolUse defer hook for this tool
    # only, so the resume turn doesn't loop.
    confirmed_map = cfg.get("approvals.defer_confirmed_tools") or {}
    confirmed_tool = confirmed_map.get(tool_name)
    use_identity_fallback = False
    if not confirmed_tool:
        confirmed_tool = tool_name
        use_identity_fallback = True
        logger.info(
            "resume_after_defer: identity fallback for %s "
            "(no defer_confirmed_tools entry)",
            tool_name,
        )

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
    # Phase 13 (Stream C): approval resume is a stateless control prompt —
    # never resume the live SDK session, never mutate session_id, never
    # touch `messages`. The confirmed-tool allowlist still flows through.
    from agents.hooks import IN_APPROVAL_RESUME_TOOL
    from agents.runtime import run_internal_control

    # CRITICAL ORDERING: do NOT mark the approval resolved until the SDK
    # actually completes. If we marked early and execution failed, the row
    # would be unrecoverable (no longer 'pending' so restart-recovery skips it).
    reply: str | None = None
    # Identity-fallback path: set the contextvar so the PreToolUse defer hook
    # skips defer for *this exact tool name* during the resume turn. Without
    # this, the hook would re-defer and the resume would loop. The dispatch
    # case uses a separate `_confirmed` tool name that isn't in the gated
    # list, so no bypass is needed there; passing the confirmed sibling via
    # `extra_allowed_tools` is enough.
    token = (
        IN_APPROVAL_RESUME_TOOL.set(tool_name)
        if use_identity_fallback else None
    )
    try:
        try:
            reply = await run_internal_control(
                prompt,
                max_turns=5,
                max_budget_usd=0.30,
                # Identity case: tool is already in the base allowlist via the
                # appropriate wildcard (mcp__google_workspace__*, mcp__notion__*,
                # mcp__github__*) or the utility registry (python_run). No need
                # to extend allowed_tools — and we must not, since that would
                # trigger `_build_options` to attach the `_confirmed` sibling
                # server based on a name that doesn't live there.
                extra_allowed_tools=(
                    None if use_identity_fallback else [confirmed_tool]
                ),
            )
        except Exception as e:
            logger.exception(
                "resume_after_defer: run_internal_control failed for aid=%s",
                aid,
            )
            await _safe_send(
                chat_id, f"approval ran but execution fell over: {e}",
            )
            # Mark the row failed so restart-recovery doesn't loop forever on
            # it, but leave an audit trace explaining what happened.
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
            result_summary=(
                f"deferred->resumed via {confirmed_tool} (tu={tool_use_id})"
            ),
            approved_by="owner",
        )
        if reply:
            # H-4 (Phase 13.1 fixup): the bridge's module-level
            # ``_send_text_with_choreography`` already does filter → send →
            # persist (with ``telegram_message_id`` stamped) →
            # ``postsend.mark_pending_surfaced``.
            # We feed it the same bot the rest of approvals uses via ``_bot()``.
            try:
                from agents.telegram_bridge import (  # noqa: PLC0415
                    _send_text_with_choreography,
                )
                await _send_text_with_choreography(_bot(), chat_id, reply[:1000])
            except Exception:
                logger.exception(
                    "resume_after_defer: bridge choreography failed; "
                    "falling back to _safe_send (no persist)"
                )
                await _safe_send(chat_id, reply[:1000])
        return True
    finally:
        if token is not None:
            IN_APPROVAL_RESUME_TOOL.reset(token)


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
