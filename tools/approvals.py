"""Out-of-band approval framework — gatekeeper path only (Phase F).

Phase F: legacy PreToolUse defer plumbing deleted. This module retains:
  - resolve_pending_approval: routes inbound text to the gatekeeper or legacy
    callback path. Gatekeeper rows (gate_kind='gatekeeper') are resolved via
    GATEKEEPER.resolve; legacy callback rows are auto-cancelled.
  - send_defer_prompt: kept for backwards compat (dead code after Phase E);
    the gatekeeper sends its own prompts.
  - set_bot / _bot / owner_chat_id: wired by the bridge at startup.
  - _safe_send: canary-leak-filtered send helper.
  - _redact / _safe_args_dump: used by gatekeeper audit chain.
  - always_approve / _check_always_approve: per-session per-tool allowlist.

Gatekeeper approval flow:
  1. SDK can_use_tool calls GATEKEEPER.request() which writes an approvals row
     (gate_kind='gatekeeper') and sends a Telegram prompt, then awaits an event.
  2. Bridge calls resolve_pending_approval on every inbound user text.
  3. resolve_pending_approval sees gate_kind='gatekeeper', calls
     GATEKEEPER.resolve(tool_use_id, outcome) which wakes the awaiting event.
  4. can_use_tool returns Allow or Deny to the SDK.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx  # noqa: F401 — kept for third-party consumers that may import from here

from agents import config as cfg
from storage import db
from tools._response import ok as _ok  # noqa: F401 — re-exported for legacy callers

if __import__("typing").TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

# Bridge sets this in post_init so tools can access it without circular imports.
_BOT_REF: "Bot | None" = None


def set_bot(bot: "Bot") -> None:
    global _BOT_REF
    _BOT_REF = bot


def _bot() -> "Bot":
    if _BOT_REF is None:
        raise RuntimeError("approvals.set_bot() not called; bridge not started?")
    return _BOT_REF


def owner_chat_id() -> int:
    """Shared accessor for the owner's Telegram chat_id (single-user bot)."""
    raw = os.environ.get("OWNER_TELEGRAM_ID")
    if not raw:
        raise RuntimeError("OWNER_TELEGRAM_ID not set")
    return int(raw)


def _timeout_sec() -> int:
    return int(cfg.get("approvals.timeout_sec", 60))


def _confirm_hint() -> str:
    """Phase 8: single hint format — typed-phrase confirmation only."""
    phrase = cfg.get("approvals.tier_2_phrase", "CONFIRM-SEND")
    return f"type {phrase} exactly to send. {_timeout_sec()}s."


# Backwards-compat alias for any legacy caller.
_tier_2_hint = _confirm_hint


async def _safe_send(chat_id: int, text: str) -> None:
    """Send a Telegram message through the canary-leak filter."""
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

    Kept for backwards compatibility; Gatekeeper sends its own prompts now.
    """
    prompt = f"⏸️  {summary}\n\n{_confirm_hint()}"
    await _safe_send(chat_id, prompt)


# ---------------------------------------------------------------------------
# alwaysApprove per-session per-tool (Feature 1, Phase F)
# ---------------------------------------------------------------------------

_ALWAYS_APPROVE: dict[tuple[int, str], float] = {}  # (chat_id, tool_name) -> expires_at_epoch


def always_approve(chat_id: int, tool_name: str, ttl_seconds: int = 3600) -> None:
    """Whitelist (chat_id, tool_name) for ttl_seconds.

    Gatekeeper.request returns PermissionResultAllow without prompting during
    the TTL. State is in-process (not durable across restarts).
    """
    _ALWAYS_APPROVE[(chat_id, tool_name)] = time.time() + ttl_seconds
    logger.info(
        "always_approve: whitelisted %s for chat_id=%s for %ds",
        tool_name, chat_id, ttl_seconds,
    )


def _check_always_approve(chat_id: int, tool_name: str) -> bool:
    """Return True if (chat_id, tool_name) has an active always-approve entry."""
    key = (chat_id, tool_name)
    exp = _ALWAYS_APPROVE.get(key)
    if exp is None:
        return False
    if exp < time.time():
        _ALWAYS_APPROVE.pop(key, None)
        return False
    return True


# ---------------------------------------------------------------------------
# Pending approval resolution (gatekeeper path)
# ---------------------------------------------------------------------------

async def resolve_pending_approval(chat_id: int, text: str) -> bool:
    """Bridge calls this on every inbound text message BEFORE routing to respond().

    Returns True if the message was consumed (i.e. the user replied to a pending
    approval) — in which case the bridge should NOT pass it on to the agent.

    Phase F: only the gatekeeper branch is live. Any pending row without
    gate_kind='gatekeeper' is treated as a stale artefact and left untouched.

    Implicit-cancel: when a gatekeeper approval is pending and the user sends a
    non-CONFIRM-SEND, non-reject message, the approval is auto-cancelled and the
    original message still routes to the agent (returns False).
    """
    pending = db.approval_pending_for(chat_id)
    if not pending:
        return False
    text_clean = text.strip()

    reject_phrases = [
        str(p).lower() for p in cfg.get("approvals.reject_phrases", []) or []
    ]
    confirm_phrase = str(cfg.get("approvals.tier_2_phrase", "CONFIRM-SEND"))
    lower = text_clean.lower()

    approved = text_clean == confirm_phrase
    rejected = lower in reject_phrases

    gate_kind = pending.get("gate_kind")
    tool_use_id_gk = pending.get("tool_use_id")
    if gate_kind == "gatekeeper" and tool_use_id_gk:
        from tools.gatekeeper import GATEKEEPER
        if approved:
            await GATEKEEPER.resolve(str(tool_use_id_gk), "approved")
            return True
        else:
            await GATEKEEPER.resolve(str(tool_use_id_gk), "rejected")
            if rejected:
                await _safe_send(chat_id, "ok. didn't do it.")
                return True
            else:
                tool_name_gk = str(pending.get("tool_name") or "")
                short_name = tool_name_gk.rsplit("__", 1)[-1] or "that"
                await _safe_send(
                    chat_id, f"...dropping the {short_name} thing. moving on.",
                )
                return False

    # Non-gatekeeper pending row (stale legacy artefact): don't consume the
    # message; just let it route to the agent normally.
    return False


# ---------------------------------------------------------------------------
# Redaction helpers (used by gatekeeper audit chain)
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = [
    (r"sk-[a-zA-Z0-9_-]{20,}", "[REDACTED-API-KEY]"),
    (r"ya29\.[a-zA-Z0-9_-]+", "[REDACTED-OAUTH-TOKEN]"),
    (r"Bearer [a-zA-Z0-9._-]+", "Bearer [REDACTED]"),
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
    import json
    return _redact(json.dumps(args, default=str, ensure_ascii=False))
