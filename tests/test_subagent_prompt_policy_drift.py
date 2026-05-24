"""Sprint 6E — subagent prompt vs runtime tool registry drift.

Subagent prompts in agents/subagents/prompts/*.md tell the LLM which MCP
servers / tools to call. If a prompt names a tool the registry no longer
exposes (renamed, removed, gated, behind a different server prefix), the
subagent silently runs without that capability and the user only finds
out when the request fails mid-conversation.

This test parses every subagent prompt + description, extracts:
  - `mcp__<server>__<tool>` literals
  - `mcp__<server>__*` wildcards
  - bare tool names mentioned with a write/destructive verb context
    (gmail_send_email / drive_delete_file / etc.)

then verifies:
  1. Every server prefix is a real MCP server in config/tools.yaml.
  2. Every literal tool resolves in the registry.
  3. Any literal write/destructive tool resolves to an EXPLICIT registry
     entry (not via wildcard) so its gate is unambiguous.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools._tools_yaml import load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "agents" / "subagents" / "prompts"

# mcp__<server>__<tool_or_*> — server is alphanumeric+underscore, tool same or `*`.
_MCP_REF_RE = re.compile(r"\bmcp__([a-zA-Z0-9_]+)__([a-zA-Z0-9_*-]+)")

# Bare write/destructive tool names that prompts list inline. Keep this
# narrow — only the ones actually referenced as of Sprint 6E. Extend on
# the next subagent prompt edit if more are added.
_BARE_WRITE_TOOLS = {
    "gmail_send_email",
    "gmail_reply_to_email",
    "gmail_bulk_delete_messages",
    "delete_calendar_event",
    "drive_delete_file",
}


def _all_prompt_text() -> list[tuple[Path, str]]:
    if not PROMPTS_DIR.exists():
        pytest.skip(f"prompts dir {PROMPTS_DIR} missing")
    return [(p, p.read_text("utf-8")) for p in sorted(PROMPTS_DIR.glob("*.md"))]


def test_prompts_dir_has_files():
    assert _all_prompt_text(), "no subagent prompts found — drift test is vacuous"


def test_every_mcp_server_prefix_in_prompts_exists_in_registry():
    """For each mcp__<server>__<tool> reference, <server> must be a real MCP server."""
    registry = load_registry()
    valid_servers = set(registry.mcp_servers().keys())
    for path, text in _all_prompt_text():
        for m in _MCP_REF_RE.finditer(text):
            server = m.group(1)
            # Skip in-process hikari_* servers — those are registered dynamically.
            if server.startswith("hikari_"):
                continue
            assert server in valid_servers, (
                f"{path.name}: references mcp__{server}__... but "
                f"'{server}' is NOT a registered MCP server. "
                f"Known servers: {sorted(valid_servers)}"
            )


def test_every_literal_mcp_tool_in_prompts_resolves():
    """Literal `mcp__server__tool` (not wildcard) must resolve in the registry."""
    registry = load_registry()
    for path, text in _all_prompt_text():
        for m in _MCP_REF_RE.finditer(text):
            tool = m.group(2)
            if tool in {"*"}:
                continue
            fullname = f"mcp__{m.group(1)}__{tool}"
            spec = registry._resolve(fullname)
            assert spec is not None, (
                f"{path.name}: references {fullname!r} but it does NOT "
                f"resolve to any tool in config/tools.yaml (no explicit "
                f"entry, no matching wildcard)."
            )


def test_bare_write_tool_mentions_resolve_via_explicit_entry():
    """If a prompt names a write/destructive tool inline, the registry must
    have an explicit entry for it — not just a wildcard. Explicit entries
    have their gate/policy declared; wildcards inherit defaults that may
    not match the prompt's contract.
    """
    registry = load_registry()
    for path, text in _all_prompt_text():
        for tool in _BARE_WRITE_TOOLS:
            if tool not in text:
                continue
            # The actual fully-qualified name depends on the MCP server.
            # For Google Workspace tools, the prefix is mcp__google_workspace__.
            fullname = f"mcp__google_workspace__{tool}"
            spec, kind = registry._resolve_with_kind(fullname)
            assert spec is not None, (
                f"{path.name}: mentions write tool {tool!r} but "
                f"{fullname!r} doesn't resolve in the registry."
            )
            assert kind == "explicit", (
                f"{path.name}: mentions write tool {tool!r}, but "
                f"{fullname!r} resolves only via wildcard ({kind}). "
                f"Add an explicit entry in config/tools.yaml with the "
                f"intended gate/policy."
            )
