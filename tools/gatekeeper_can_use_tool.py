"""SDK can_use_tool callable for the gatekeeper gate.

Phase E (Sprint 2). Routes through ``tools.gatekeeper.GATEKEEPER.request()``
for tools whose registry entry has ``gate: gatekeeper``; passes everything
else through immediately (PermissionResultAllow).

This is a module-level callable — no closures over per-turn state. The
persistent live client is built once at boot, so the can_use_tool must be
importable without side effects and must work across concurrent turns.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from agents import config as cfg

logger = logging.getLogger(__name__)


def _gate_for(tool_name: str) -> str | None:
    """Return the gate kind for a tool ('gatekeeper', 'defer', None)."""
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        spec = reg._resolve(tool_name)
        return spec.gate if spec else None
    except Exception:
        logger.debug("gatekeeper_can_use_tool: registry lookup failed for %s", tool_name, exc_info=True)
        return None


def _deadline_for(tool_name: str) -> datetime:
    """Compute the approval deadline for a tool."""
    MAX_TIMEOUT_S = 600  # 10-minute hard cap to prevent event-loop task pileup
    # Per-tool override first, then global default.
    per_tool_secs: int | None = None
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        # gatekeeper_timeout_s is not a ToolSpec field yet; read from cfg as fallback.
    except Exception:
        pass
    secs = per_tool_secs or int(cfg.get("gatekeeper.default_timeout_s", 300))
    secs = min(secs, MAX_TIMEOUT_S)
    return datetime.now(timezone.utc) + timedelta(seconds=secs)


def _resolve_chat_id() -> int:
    raw = os.environ.get("OWNER_TELEGRAM_ID") or cfg.get("telegram.owner_chat_id")
    if not raw:
        raise RuntimeError("OWNER_TELEGRAM_ID / telegram.owner_chat_id not set")
    return int(raw)


def _summarize(tool_name: str, input_args: dict) -> str:
    # Per-tool readable summary first; fall back to JSON dump if unmapped.
    try:
        from tools.gatekeeper import summarize as _per_tool_summary
        return _per_tool_summary(tool_name, input_args)
    except (ImportError, NotImplementedError):
        pass
    try:
        pretty = json.dumps(input_args, ensure_ascii=False)
    except (TypeError, ValueError):
        pretty = str(input_args)
    if len(pretty) > 200:
        pretty = pretty[:197] + "..."
    return f"{tool_name}: {pretty}"


async def gatekeeper_can_use_tool(
    tool_name: str,
    input: dict,
    context,
):
    """SDK can_use_tool hook. Must be a module-level async callable.

    ``context`` is a ``ToolPermissionContext``; we duck-type it so tests can
    pass a simple namespace without importing the SDK.
    """
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    gate = _gate_for(tool_name)
    if gate != "gatekeeper":
        return PermissionResultAllow(updated_input=input)

    tool_use_id = getattr(context, "tool_use_id", None) or f"missing-{tool_name}-{id(input)}"
    deadline = _deadline_for(tool_name)
    summary = _summarize(tool_name, input)

    try:
        chat_id = _resolve_chat_id()
    except RuntimeError as e:
        logger.error("gatekeeper_can_use_tool: cannot resolve chat_id: %s", e)
        return PermissionResultDeny(message=f"gatekeeper config error: {e}")

    try:
        from agents.injection_guard import flag_args_with_untrusted_content  # noqa: PLC0415
        taint_flag, taint_reason = flag_args_with_untrusted_content(input)
        if taint_flag:
            logger.warning(
                "gatekeeper_can_use_tool: args flagged untrusted-origin (%s) for %s",
                taint_reason, tool_name,
            )
            return PermissionResultDeny(
                message=f"gatekeeper denied: untrusted-origin content in args ({taint_reason})"
            )
    except Exception:
        logger.debug(
            "gatekeeper_can_use_tool: taint check failed for %s", tool_name, exc_info=True
        )

    try:
        from tools.gatekeeper import GATEKEEPER
        outcome = await GATEKEEPER.request(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            chat_id=chat_id,
            args=input,
            summary=summary,
            deadline=deadline,
        )
    except Exception as e:
        logger.exception("gatekeeper_can_use_tool: request failed for %s", tool_name)
        return PermissionResultDeny(message=f"gatekeeper error: {e}")

    if outcome == "approved":
        return PermissionResultAllow(updated_input=input)
    return PermissionResultDeny(message=f"approval {outcome}")
