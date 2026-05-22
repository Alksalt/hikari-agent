"""Phase 8 — PostToolUse hook that wraps text output from configured untrusted
tools via ``wrap_untrusted``.

Background: Codex flagged that `wrap_untrusted` was only called at two
hand-rolled sites (``tools/wiki.wiki_read`` and ``mcp_external/server``).
Google Workspace / Notion / Web* outputs reached the model raw despite being
listed as untrusted in config — the prompt-injection defense was
structural at the wiki boundary but only aspirational everywhere else.

This module installs one generic PostToolUse hook that:
  - matches the qualified tool name against config-driven regex patterns
    (``prompt_injection.wrap_patterns``);
  - on a match, walks the tool_response content blocks and replaces each
    text payload with ``wrap_untrusted(tool_name, original_text)``;
  - writes an audit row per wrap activation so the defense is observable.

The hook returns the modified output via the SDK's ``updatedToolOutput``
mechanism (PostToolUseHookSpecificOutput). Non-matching tools pass through
unchanged with an empty hook output.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from storage import db

from . import config as cfg
from .injection_guard import wrap_untrusted

logger = logging.getLogger(__name__)


def _compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for raw in patterns:
        try:
            compiled.append(re.compile(str(raw)))
        except re.error:
            logger.warning("external_wrap_hook: invalid regex %r", raw)
    return compiled


def _matches_any(tool_name: str, compiled: list[re.Pattern[str]]) -> bool:
    return any(p.search(tool_name) for p in compiled)


def _wrap_content_blocks(tool_name: str, content: Any) -> Any:
    """Walk an MCP-shaped content list and replace each text block's payload
    with the untrusted-wrapped form. Non-text blocks (images, etc.) are
    passed through unchanged. Returns a NEW list — does not mutate input."""
    if not isinstance(content, list):
        return content
    out: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            wrapped = wrap_untrusted(tool_name, block["text"])
            new_block = dict(block)
            new_block["text"] = wrapped
            out.append(new_block)
        else:
            out.append(block)
    return out


def _wrap_tool_response(tool_name: str, tool_response: Any) -> Any:
    """Return a new tool_response value with text payloads wrapped.

    Handles every MCP envelope shape we've observed:
      - ``{"content": [{type: text, text: ...}, ...]}`` — walk list, wrap each text block
      - ``{"content": "raw string"}`` — Gmail/Notion sometimes emit this flat shape;
        wrap the string in-place (high-risk path that an earlier version of this
        hook silently passed through with "wrap applied" in the audit log).
      - ``"raw string"`` — bare string response; wrap directly.

    Other shapes (numbers, lists-of-non-blocks, custom dicts without "content")
    pass through unchanged. ``_audit_wrap`` is only called in
    ``wrap_post_tool_use`` when this function actually changed the value.

    B-3 finding (Stream B): the ``data`` field in MCP tool return dicts is
    PROGRAMMATIC-ONLY and is never exposed to the model. The SDK's
    ``create_sdk_mcp_server`` call_tool handler (``__init__.py``) only reads
    ``result["content"]`` when constructing the ``CallToolResult``; the
    ``data`` key is silently ignored and never forwarded to the CLI subprocess
    (confirmed in ``claude_agent_sdk/_internal/query.py`` lines 645-700).
    Therefore ``data`` does not need to be wrapped — it is safe to preserve
    it unchanged in PostToolUse hook responses.
    """
    # Common MCP envelope.
    if isinstance(tool_response, dict) and "content" in tool_response:
        raw_content = tool_response["content"]
        if isinstance(raw_content, list):
            new_resp = dict(tool_response)
            new_resp["content"] = _wrap_content_blocks(tool_name, raw_content)
            return new_resp
        if isinstance(raw_content, str):
            # H2 fix: flat-string content gets wrapped in place. Previously
            # this path returned `dict(tool_response)` unchanged + an audit
            # row claiming "wrap applied", which silently bypassed the
            # untrusted-content defense for Gmail/Notion-style responses.
            new_resp = dict(tool_response)
            new_resp["content"] = wrap_untrusted(tool_name, raw_content)
            return new_resp
        # Some other content shape (None, dict, number). Pass through.
        return tool_response
    # Bare string — wrap it directly.
    if isinstance(tool_response, str):
        return wrap_untrusted(tool_name, tool_response)
    return tool_response


def _audit_wrap(tool_name: str) -> None:
    """Append a small audit row so the wrap activation is observable. Best
    effort — never raises."""
    try:
        db.audit_append(
            tool=f"wrap_external:{tool_name}",
            args_json_redacted="",
            result_summary="postToolUse wrap applied",
            approved_by="auto",
        )
    except Exception:
        logger.exception("external_wrap_hook: audit_append failed (non-fatal)")


def make_post_tool_use_hook(
    patterns: list[str] | None = None,
) -> Callable[..., Any]:
    """Build the PostToolUse hook closure.

    Pattern source priority:
      1. Explicit ``patterns`` arg (tests / callers may override).
      2. ``prompt_injection.wrap_patterns`` from engagement.yaml when present.
      3. ``tools._tools_yaml.load_registry().wrap_patterns()`` — single-source registry.
    """
    if patterns is not None:
        raw_patterns = list(patterns)
    else:
        cfg_patterns = cfg.get("prompt_injection.wrap_patterns")
        if cfg_patterns is not None:
            raw_patterns = list(cfg_patterns)
        else:
            try:
                from tools._tools_yaml import load_registry
                raw_patterns = load_registry().wrap_patterns()
            except Exception:
                raw_patterns = []
    compiled = _compile_patterns(raw_patterns)

    async def wrap_post_tool_use(
        input_data: dict[str, Any] | Any,
        tool_use_id: str | None,  # noqa: ARG001
        context: Any,  # noqa: ARG001
    ) -> dict[str, Any]:
        if not compiled or not isinstance(input_data, dict):
            return {}
        tool_name = str(input_data.get("tool_name") or "")
        if not tool_name or not _matches_any(tool_name, compiled):
            return {}

        tool_response = input_data.get("tool_response")
        try:
            updated = _wrap_tool_response(tool_name, tool_response)
        except Exception:
            logger.exception(
                "external_wrap_hook: wrap failed for %s; passing through raw",
                tool_name,
            )
            return {}

        if updated is tool_response:
            # Nothing to do — non-string, non-MCP shape.
            return {}

        _audit_wrap(tool_name)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated,
            }
        }

    return wrap_post_tool_use
