"""SDK can_use_tool callable for the gatekeeper gate.

Phase E (Sprint 2). Routes through ``tools.gatekeeper.GATEKEEPER.request()``
for tools whose registry entry has ``gate: gatekeeper``; passes everything
else through immediately (PermissionResultAllow).

This is a module-level callable — no closures over per-turn state. The
persistent live client is built once at boot, so the can_use_tool must be
importable without side effects and must work across concurrent turns.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from agents import config as cfg

logger = logging.getLogger(__name__)

_CRITICAL_FIELDS = {
    # recipients / addressees
    "recipients", "to", "cc", "bcc", "addressees", "target_email", "bcc_list",
    # filesystem / object identity (anything that names what gets touched/deleted)
    "file_paths", "paths", "path", "files",
    "object_id", "page_id", "block_id", "issue_number", "pull_number", "pullNumber",
    "branch", "from_branch", "ref", "base", "head",
    "draft_id", "message_id", "event_id", "calendar_id", "file_id", "folder_id",
    "document_id", "spreadsheet_id", "presentation_id", "slide_id", "reminder_id",
    "data_source_id", "parent",
    # repo identity
    "owner", "repo", "repository",
    # executable / queries
    "executable_code", "code", "query", "sql",
    # payloads the operator must read in full to consent (body/content can hide
    # malicious tail past a 100-char truncation)
    "body", "html_body", "content", "text", "message",
    "title", "subject", "name",
    "values", "range",
}


def _gate_for(tool_name: str) -> str | None:
    """Return the gate kind for a tool ('gatekeeper' or None)."""
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        spec = reg._resolve(tool_name)
        return spec.gate if spec else None
    except Exception:
        logger.debug(
            "gatekeeper_can_use_tool: registry lookup failed for %s", tool_name, exc_info=True
        )
        return None


def _resolve_spec_and_kind(tool_name: str):
    """Return (spec, match_kind) from the registry, or (None, None) on failure."""
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        return reg._resolve_with_kind(tool_name)
    except Exception:
        logger.debug(
            "gatekeeper_can_use_tool: _resolve_with_kind failed for %s", tool_name, exc_info=True
        )
        return None, None


def _deadline_for(tool_name: str) -> datetime:
    """Compute the approval deadline for a tool.

    Priority: per-tool gate_timeout_sec in tools.yaml → global
    gatekeeper.default_timeout_s config → 300s fallback.
    Hard cap: 600s (10 min) to prevent event-loop task pileup.
    """
    MAX_TIMEOUT_S = 600
    per_tool_secs: int | None = None
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        spec = reg._resolve(tool_name)
        if spec is not None:
            per_tool_secs = spec.gate_timeout_sec
    except Exception:
        logger.debug(
            "_deadline_for: registry lookup failed for %s", tool_name, exc_info=True
        )
    secs = per_tool_secs if per_tool_secs is not None else int(
        cfg.get("gatekeeper.default_timeout_s", 300)
    )
    secs = min(secs, MAX_TIMEOUT_S)
    return datetime.now(UTC) + timedelta(seconds=secs)


def _resolve_chat_id() -> int:
    raw = os.environ.get("OWNER_TELEGRAM_ID") or cfg.get("telegram.owner_chat_id")
    if not raw:
        raise RuntimeError("OWNER_TELEGRAM_ID / telegram.owner_chat_id not set")
    return int(raw)


def _summarize(tool_name: str, input_args: dict) -> str:
    """Render a human-readable approval preview for a tool call.

    Per-tool summarizer wins if available. Fallback renders each arg
    individually: critical fields (recipients, paths, body, code, etc.) are
    shown in full so the operator can consent on what's actually happening;
    other fields are truncated to 100 chars. If the critical fields alone
    exceed the 2000-char cap, we REFUSE to render values at all and force
    the operator to reject — better blind rejection than rubber-stamped
    half-shown payload. If critical fields fit but non-critical pushes over,
    we keep critical in full and elide the rest with a sentinel.
    """
    try:
        from tools.gatekeeper import summarize as _per_tool_summary
        return _per_tool_summary(tool_name, input_args)
    except (ImportError, NotImplementedError):
        pass
    crit_parts: list[str] = []
    other_parts: list[str] = []
    for key, value in input_args.items():
        if key in _CRITICAL_FIELDS:
            crit_parts.append(f"{key}: {value!r}")
        else:
            v_str = str(value)
            if len(v_str) > 100:
                v_str = v_str[:100] + "…"
            other_parts.append(f"{key}: {v_str}")
    crit_body = "\n  ".join(crit_parts)
    if len(crit_body) > 2000:
        return (
            f"{tool_name}:\n"
            f"  ⚠ CRITICAL FIELDS EXCEED 2000 CHARS — REFUSE THIS APPROVAL "
            f"AND ASK HIKARI TO SPLIT THE CALL\n"
            f"  field_names: {sorted(input_args)}"
        )
    body = "  " + "\n  ".join(crit_parts + other_parts)
    if len(body) > 2000:
        body = (
            "  " + crit_body
            + "\n  …\n  ⚠ NON-CRITICAL FIELDS ELIDED — critical fields shown in full above"
        )
    return f"{tool_name}:\n{body}"


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

    spec, match_kind = _resolve_spec_and_kind(tool_name)
    if spec is None:
        # Unknown tool — fall through to deny-safe default.
        return PermissionResultDeny(message=f"refused: {tool_name} not found in tool registry")

    gate = spec.gate
    access_mode = spec.access_mode

    if gate is None and match_kind == "wildcard" and access_mode in {"write", "destructive"}:
        logger.warning(
            "wildcard write/destructive without gate: %s (mode=%s)",
            tool_name, access_mode,
        )
        return PermissionResultDeny(
            message=(
                f"refused: {tool_name} resolves via wildcard with "
                f"access_mode={access_mode}; add an explicit gated entry"
            )
        )

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
