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
    """runtime.py allowlist contains mcp__apple_shortcuts__*."""
    src = (REPO_ROOT / "agents" / "runtime.py").read_text()
    assert "mcp__apple_shortcuts__*" in src


def test_apple_shortcuts_in_wrap_patterns():
    """Shortcuts output can include external content (RSS, HTTP fetches),
    so the MCP must be in prompt_injection.wrap_patterns. Mirrors the
    treatment of apple_events for consistency with project precedent."""
    cfg = yaml.safe_load((REPO_ROOT / "config" / "engagement.yaml").read_text())
    patterns = cfg["prompt_injection"]["wrap_patterns"]
    matched = any(
        re.match(pat, "mcp__apple_shortcuts__run_shortcut")
        for pat in patterns
    )
    assert matched, (
        f"no wrap_pattern matched mcp__apple_shortcuts__*; patterns: {patterns}"
    )
