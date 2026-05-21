"""Readwise MCP smoke test: server entry parses, allowlist contains it."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import agents.runtime as runtime
    importlib.reload(runtime)


def test_readwise_in_mcp_json():
    mcp = json.loads(
        (Path(__file__).parent.parent / ".mcp.json").read_text())
    assert "readwise" in mcp.get("mcpServers", {})


def test_readwise_wildcard_in_allowlist():
    from agents.runtime import allowed_tool_names
    tools = allowed_tool_names()
    assert any("readwise" in t for t in tools)
