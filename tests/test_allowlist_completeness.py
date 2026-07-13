"""External and local allowlist regression.

Google Workspace uses explicit tool names so unreviewed upstream tools stay hidden.
Stream B: Read, Glob, Grep removed; mcp__hikari_utility__read_attachment replaces them.
Stream D: mcp__hikari_scratch__* removed (scratch tool deleted).
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _set_owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    from agents import runtime
    importlib.reload(runtime)
    yield


def _get_allowed_tools() -> list[str]:
    from agents.runtime import allowed_tool_names
    return allowed_tool_names()


def test_google_workspace_in_allowlist():
    """Reviewed Google Workspace tools, but no catch-all, are allowlisted."""
    tools = _get_allowed_tools()
    assert "mcp__google_workspace__query_gmail_emails" in tools
    assert "mcp__google_workspace__gmail_send_email" in tools
    assert "mcp__google_workspace__*" not in tools


def test_notion_in_allowlist():
    """mcp__notion__* must be in the allowlist."""
    tools = _get_allowed_tools()
    assert any("notion" in t for t in tools), (
        "mcp__notion__* not found in allowed_tool_names()"
    )


def test_read_attachment_in_allowlist():
    """Stream B: mcp__hikari_utility__read_attachment must be present."""
    tools = _get_allowed_tools()
    assert "mcp__hikari_utility__read_attachment" in tools, (
        "mcp__hikari_utility__read_attachment not in allowed_tool_names(); "
        "Stream B added it as the scoped replacement for Read/Glob/Grep"
    )


def test_read_not_in_allowlist():
    """Stream B: unscoped Read must have been removed."""
    tools = _get_allowed_tools()
    assert "Read" not in tools, (
        "'Read' is still in allowed_tool_names(); Stream B removed it in favour "
        "of the scoped mcp__hikari_utility__read_attachment"
    )


def test_glob_not_in_allowlist():
    """Stream B: Glob must have been removed."""
    tools = _get_allowed_tools()
    assert "Glob" not in tools, (
        "'Glob' is still in allowed_tool_names(); Stream B removed it"
    )


def test_grep_not_in_allowlist():
    """Stream B: Grep must have been removed."""
    tools = _get_allowed_tools()
    assert "Grep" not in tools, (
        "'Grep' is still in allowed_tool_names(); Stream B removed it"
    )


def test_scratch_not_in_allowlist():
    """Stream D: mcp__hikari_scratch__* must NOT be in the allowlist (deleted)."""
    tools = _get_allowed_tools()
    assert not any("hikari_scratch" in t for t in tools), (
        "mcp__hikari_scratch__* still in allowed_tool_names(); "
        "Stream D deleted the scratch tool"
    )
