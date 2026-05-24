"""Subagent prompt vs runtime tool registry drift tests.

This test file covers two invariants:

1. MCP reference consistency (original Sprint 6E checks):
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

2. AUTO-POLICY block consistency:
   Each subagent prompt contains a section delimited by
   <!-- BEGIN AUTO-POLICY --> / <!-- END AUTO-POLICY --> markers.
   The content between the markers must match byte-for-byte what
   scripts/regen_subagent_policy.py would generate for that subagent's
   allowlist from config/tools.yaml. Any change to a gated tool in
   config/tools.yaml either fails this test or triggers a prompt update
   via `uv run python scripts/regen_subagent_policy.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from tools._tools_yaml import load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "agents" / "subagents" / "prompts"

# mcp__<server>__<tool_or_*> — server is alphanumeric+underscore, tool same or `*`.
_MCP_REF_RE = re.compile(r"\bmcp__([a-zA-Z0-9_]+)__([a-zA-Z0-9_*-]+)")

def _derive_gw_gatekeeper_tools() -> set[str]:
    """Derive all Google Workspace tool short names that have gate: gatekeeper.

    Reads config/tools.yaml at import time so this set is always consistent
    with the actual registry — no hand-maintained list.
    """
    registry = load_registry()
    result: set[str] = set()
    for spec in registry.specs():
        if not spec.id.startswith("mcp__google_workspace__"):
            continue
        if spec.gate == "gatekeeper":
            # short name is the suffix after the server prefix
            short = spec.id[len("mcp__google_workspace__"):]
            result.add(short)
    return result


# Derived from config/tools.yaml at test-collection time — never hand-maintained.
_BARE_WRITE_TOOLS: set[str] = _derive_gw_gatekeeper_tools()


_BEGIN_MARKER = "<!-- BEGIN AUTO-POLICY -->"
_END_MARKER = "<!-- END AUTO-POLICY -->"


def _extract_policy_block(text: str) -> str | None:
    """Return the text between AUTO-POLICY markers, or None if markers absent."""
    begin_idx = text.find(_BEGIN_MARKER)
    end_idx = text.find(_END_MARKER)
    if begin_idx == -1 or end_idx == -1 or end_idx <= begin_idx:
        return None
    # Content starts after the marker + newline (if present).
    content_start = begin_idx + len(_BEGIN_MARKER)
    if text[content_start:content_start + 1] == "\n":
        content_start += 1
    return text[content_start:end_idx]


def _all_prompt_text() -> list[tuple[Path, str]]:
    if not PROMPTS_DIR.exists():
        pytest.skip(f"prompts dir {PROMPTS_DIR} missing")
    return [(p, p.read_text("utf-8")) for p in sorted(PROMPTS_DIR.glob("*.md"))]


def test_prompts_dir_has_files():
    assert _all_prompt_text(), "no subagent prompts found — drift test is vacuous"


# ---------------------------------------------------------------------------
# AUTO-POLICY block consistency
# ---------------------------------------------------------------------------

def test_auto_policy_blocks_match_registry():
    """Each subagent prompt's AUTO-POLICY block must match regen_subagent_policy output.

    Imports the generator logic directly so no subprocess is needed and the
    assertion is deterministic. Any change to a gated tool in config/tools.yaml
    must be followed by `uv run python scripts/regen_subagent_policy.py`.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.regen_subagent_policy import _build_policy_block, _inject_markers

    registry = load_registry()
    failures: list[str] = []

    for subagent_id, spec in registry._subagents_spec.items():
        prompt_path = REPO_ROOT / spec.prompt_path
        if not prompt_path.exists():
            failures.append(f"{subagent_id}: prompt file {spec.prompt_path!r} not found")
            continue

        current_text = prompt_path.read_text("utf-8")
        actual_block = _extract_policy_block(current_text)

        if actual_block is None:
            failures.append(
                f"{subagent_id} ({spec.prompt_path}): missing AUTO-POLICY markers. "
                "Run: uv run python scripts/regen_subagent_policy.py"
            )
            continue

        expected_body = _build_policy_block(subagent_id, spec.tools)
        if actual_block != expected_body:
            failures.append(
                f"{subagent_id} ({spec.prompt_path}): AUTO-POLICY block is out of sync "
                "with config/tools.yaml.\n"
                f"  expected:\n{expected_body!r}\n"
                f"  actual:\n{actual_block!r}\n"
                "Run: uv run python scripts/regen_subagent_policy.py"
            )

    assert not failures, (
        f"{len(failures)} subagent prompt(s) have stale AUTO-POLICY blocks:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


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


# ---------------------------------------------------------------------------
# Registry gate invariant: no LLM-facing destructive write has gate: null
# ---------------------------------------------------------------------------

def test_no_llm_facing_destructive_write_ungated():
    """Every external (bucket-3) explicit write/destructive tool must have a
    non-null gate unless its id ends with '_unsafe' (scheduler-bypass
    convention) or its server is in the explicit exemption set.

    In-process tools (bucket-1, hikari_* servers) are intentionally ungated —
    they run as Hikari's own internal tools, not as external service writes.

    Wildcards are excluded — their policy is governed by the wildcard-write deny
    in gatekeeper_can_use_tool, not by gate: value.
    """
    # apple_shortcuts wildcard has access_mode=destructive but no explicit write
    # entries — exempted because it has no network-reachable external accounts.
    # apple_events is NOT exempted: its LLM-facing write tools carry gate:
    # confirm_send (added in 8B), so they pass the gate != null check naturally.
    _EXEMPT_SERVERS = {"apple_shortcuts"}

    registry = load_registry()
    failures: list[str] = []
    for spec in registry.specs():
        if spec.id.endswith("*"):
            continue
        if spec.id.endswith("_unsafe"):
            continue
        # In-process tools are deliberately ungated — they are Hikari's own
        # utilities, not external service writes.
        if spec.bucket == 1:
            continue
        if spec.server in _EXEMPT_SERVERS:
            continue
        if spec.access_mode not in {"write", "destructive"}:
            continue
        if spec.gate is None:
            failures.append(
                f"{spec.id!r}: access_mode={spec.access_mode!r} but gate=null. "
                "Add gate: gatekeeper or gate: confirm_send, or use the _unsafe "
                "suffix convention for scheduler-internal bypass paths."
            )
    assert not failures, (
        f"{len(failures)} LLM-facing destructive tool(s) with gate: null:\n"
        + "\n".join(f"  {f}" for f in failures)
    )


# ---------------------------------------------------------------------------
# AUTH_PRECHECK: /settings get AUTH_PRECHECK must match what hooks enforces
# ---------------------------------------------------------------------------

def test_auth_precheck_resolve_mode_reads_config(monkeypatch):
    """When no env vars are set and config says 'enforce', resolve_mode()
    must return 'enforce' (not 'off' or 'shadow')."""
    monkeypatch.delenv("AUTH_PRECHECK_OVERRIDE", raising=False)
    monkeypatch.delenv("AUTH_PRECHECK", raising=False)

    from agents.auth_precheck import resolve_mode
    mode = resolve_mode()
    # engagement.yaml sets auth.precheck: enforce; with no env vars it must win.
    assert mode == "enforce", (
        f"Expected resolve_mode() == 'enforce' (from engagement.yaml auth.precheck) "
        f"but got {mode!r}. Check agents/auth_precheck.py priority chain."
    )


def test_auth_precheck_override_env_wins(monkeypatch):
    """AUTH_PRECHECK_OVERRIDE must beat AUTH_PRECHECK and config."""
    monkeypatch.setenv("AUTH_PRECHECK_OVERRIDE", "off")
    monkeypatch.setenv("AUTH_PRECHECK", "enforce")

    from agents.auth_precheck import resolve_mode
    assert resolve_mode() == "off"


def test_auth_precheck_direct_env_beats_config(monkeypatch):
    """AUTH_PRECHECK env (no override) must beat the config file value."""
    monkeypatch.delenv("AUTH_PRECHECK_OVERRIDE", raising=False)
    monkeypatch.setenv("AUTH_PRECHECK", "shadow")

    from agents.auth_precheck import resolve_mode
    assert resolve_mode() == "shadow"
