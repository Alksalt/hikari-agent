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
import re
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

# Tools that must NEVER be whitelisted via always_approve — they require a real
# human turn (typed CONFIRM-SEND) every single call.  Closing the always_approve
# bypass hole is Phase 5, decision A.
_NEVER_ALWAYS_APPROVE: frozenset[str] = frozenset({
    "mcp__hikari_utility__skill_approve",
})


def always_approve(chat_id: int, tool_name: str, ttl_seconds: int = 3600) -> None:
    """Whitelist (chat_id, tool_name) for ttl_seconds.

    Gatekeeper.request returns PermissionResultAllow without prompting during
    the TTL. State is in-process (not durable across restarts).

    Tools in _NEVER_ALWAYS_APPROVE are silently refused — they require a typed
    CONFIRM-SEND on every call and must never be bulk-whitelisted.
    """
    if tool_name in _NEVER_ALWAYS_APPROVE:
        logger.warning(
            "always_approve: refused for %s (in _NEVER_ALWAYS_APPROVE) — "
            "this tool requires a real human turn every call",
            tool_name,
        )
        return
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

    Accepts: CONFIRM-SEND [id] | REJECT [id]
    If <id> is given, targets that specific pending row (chat_id must match).
    If absent, falls back to the most-recent pending row for this chat.

    Implicit-cancel: when a gatekeeper approval is pending and the user sends a
    non-matching message, the approval is auto-cancelled and the original message
    still routes to the agent (returns False).
    """
    text_clean = text.strip()
    confirm_phrase = str(cfg.get("approvals.tier_2_phrase", "CONFIRM-SEND"))
    _resolve_re = re.compile(
        r"^(" + re.escape(confirm_phrase) + r"|REJECT)(?:\s+(\d+))?$"
    )
    m = _resolve_re.match(text_clean)
    reject_phrases = [
        str(p).lower() for p in cfg.get("approvals.reject_phrases", []) or []
    ]

    if m:
        action = m.group(1)   # confirm_phrase or "REJECT"
        explicit_id = m.group(2)  # digit string or None

        if explicit_id is not None:
            # Explicit id: look it up directly.
            row = db.approval_get(int(explicit_id))
            if row is None or row.get("status") != "pending":
                await _safe_send(chat_id, f"approval {explicit_id}: not found or already resolved.")
                return True
            if row.get("chat_id") != chat_id:
                await _safe_send(chat_id, f"approval {explicit_id}: not yours.")
                return True
            pending = row
        else:
            # No id: fall back to most-recent pending for this chat.
            pending = db.approval_pending_for(chat_id)
            if not pending:
                return False

        gate_kind = pending.get("gate_kind")
        tool_use_id_gk = pending.get("tool_use_id")
        if gate_kind == "gatekeeper" and tool_use_id_gk:
            from tools.gatekeeper import GATEKEEPER
            if action == confirm_phrase:
                await GATEKEEPER.resolve(str(tool_use_id_gk), "approved")
                return True
            else:
                await GATEKEEPER.resolve(str(tool_use_id_gk), "rejected")
                await _safe_send(chat_id, "ok. didn't do it.")
                return True

        # Non-gatekeeper row with explicit id — consume but warn.
        return True

    # Not a CONFIRM-SEND/REJECT pattern — check if there's a pending approval
    # for the implicit-cancel side-channel (no explicit id only).
    lower = text_clean.lower()
    rejected = lower in reject_phrases
    if rejected:
        pending = db.approval_pending_for(chat_id)
        if pending:
            gate_kind = pending.get("gate_kind")
            tool_use_id_gk = pending.get("tool_use_id")
            if gate_kind == "gatekeeper" and tool_use_id_gk:
                from tools.gatekeeper import GATEKEEPER
                await GATEKEEPER.resolve(str(tool_use_id_gk), "rejected")
                await _safe_send(chat_id, "ok. didn't do it.")
                return True

    # Regular message while a gatekeeper approval is pending — implicit cancel.
    pending = db.approval_pending_for(chat_id)
    if not pending:
        return False
    gate_kind = pending.get("gate_kind")
    tool_use_id_gk = pending.get("tool_use_id")
    if gate_kind == "gatekeeper" and tool_use_id_gk:
        from tools.gatekeeper import GATEKEEPER
        await GATEKEEPER.resolve(str(tool_use_id_gk), "rejected")
        tool_name_gk = str(pending.get("tool_name") or "")
        short_name = tool_name_gk.rsplit("__", 1)[-1] or "that"
        await _safe_send(
            chat_id, f"...dropping the {short_name} thing. moving on.",
        )
        return False

    return False


# ---------------------------------------------------------------------------
# Redaction helpers (used by gatekeeper audit chain)
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = [
    (r"sk-[a-zA-Z0-9_-]{20,}", "[REDACTED-API-KEY]"),
    (r"sk-ant-[a-zA-Z0-9_-]{20,}", "[REDACTED-ANTHROPIC]"),
    (r"sk-or-[a-zA-Z0-9_-]{20,}", "[REDACTED-OPENROUTER]"),
    (r"ya29\.[a-zA-Z0-9_-]+", "[REDACTED-OAUTH-TOKEN]"),
    (r"Bearer\s+[a-zA-Z0-9._\-+/=]+", "Bearer [REDACTED]"),
    (r"ghp_[A-Za-z0-9]{30,}", "[REDACTED-GH-PAT]"),
    (r"github_pat_[A-Za-z0-9_]{60,}", "[REDACTED-GH-PAT]"),
    (r"gh[oprs]_[A-Za-z0-9]{30,}", "[REDACTED-GH-TOKEN]"),
    (r"xox[abprs]-[A-Za-z0-9-]{10,}", "[REDACTED-SLACK]"),
    (r"secret_[A-Za-z0-9]{40,}", "[REDACTED-NOTION]"),
    (r"\b\d{9,10}:[A-Za-z0-9_-]{35}\b", "[REDACTED-TG-BOT]"),
    (r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_\-+/=]+", "[REDACTED-JWT]"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}", "[REDACTED-EMAIL]"),
]

_SENSITIVE_KEY_NAMES = re.compile(
    r'"(authorization|api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?token|'
    r'refresh[_-]?token|client[_-]?secret|webhook[_-]?url|private[_-]?key)"\s*:\s*"[^"]*"',
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    if not text:
        return text
    out = _SENSITIVE_KEY_NAMES.sub(
        lambda m: m.group(0).split('":')[0] + '": "[REDACTED]"', text
    )
    for pattern, replacement in _REDACT_PATTERNS:
        out = re.sub(pattern, replacement, out)
    return out


def _safe_args_dump(args: dict[str, Any]) -> str:
    """Dump args to JSON, redacting common secret patterns."""
    import json
    return _redact(json.dumps(args, default=str, ensure_ascii=False))
