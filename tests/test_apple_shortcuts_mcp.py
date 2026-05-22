"""Apple Shortcuts MCP wiring smoke tests."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_apple_shortcuts_in_mcp_json():
    """`.mcp.json` has an `apple_shortcuts` server entry using npx."""
    config = json.loads((REPO_ROOT / ".mcp.json").read_text())
    assert "apple_shortcuts" in config["mcpServers"]
    entry = config["mcpServers"]["apple_shortcuts"]
    assert entry["command"] == "npx"
    assert "mcp-server-apple-shortcuts" in " ".join(entry["args"])


def test_apple_shortcuts_in_allowlist():
    """tools.yaml allowlist contains mcp__apple_shortcuts__*."""
    import yaml as _yaml
    cfg = _yaml.safe_load((REPO_ROOT / "config" / "tools.yaml").read_text())
    tool_ids = [t["id"] for t in cfg.get("tools", [])]
    assert "mcp__apple_shortcuts__*" in tool_ids, (
        f"mcp__apple_shortcuts__* not found in tools.yaml ids: {tool_ids}"
    )


def test_apple_shortcuts_in_wrap_patterns():
    """Shortcuts output can include external content (RSS, HTTP fetches),
    so the MCP must be in prompt_injection.wrap_patterns. Mirrors the
    treatment of apple_events for consistency with project precedent.

    Phase A: wrap_patterns sourced from tools.yaml registry.
    """
    from tools._tools_yaml import load_registry
    patterns = load_registry().wrap_patterns()
    matched = any(
        re.match(pat, "mcp__apple_shortcuts__run_shortcut")
        for pat in patterns
    )
    assert matched, (
        f"no wrap_pattern matched mcp__apple_shortcuts__*; patterns: {patterns}"
    )
