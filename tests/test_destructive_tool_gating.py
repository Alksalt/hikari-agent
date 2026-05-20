"""Phase 13.1 (Stream K) — regression: destructive tool gating coverage.

Extends the Google Workspace gating tests (test_google_workspace_send_policy.py)
to cover Notion write operations, GitHub create_issue / create_pull_request,
and Apple Events mutating operations.

These tests pin the expected gating decisions for Stream J. If J's changes
haven't landed in config/engagement.yaml yet, these tests will fail — that is
intentional (they specify the target behaviour).

Coordinate with Stream J for final Apple Events gating decisions.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from agents import config


@pytest.fixture(autouse=True)
def _reload_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    config.reload()
    yield


def _is_matched_by_patterns(tool_name: str) -> bool:
    """Replicate the exact matching logic from agents/hooks.py:_is_defer_gated."""
    patterns = config.get("approvals.defer_gated_tools") or []
    for pat in patterns:
        try:
            if re.fullmatch(str(pat), tool_name):
                return True
        except re.error:
            pass
    return False


# ---------------------------------------------------------------------------
# Notion write operations (J-1)
# ---------------------------------------------------------------------------

_NOTION_WRITE_TOOLS = [
    "mcp__notion__API-patch-block-children",
    "mcp__notion__API-patch-page",
    "mcp__notion__API-post-page",
]


@pytest.mark.parametrize("tool_name", _NOTION_WRITE_TOOLS)
def test_notion_write_tools_are_gated(tool_name):
    """Notion write operations must be in defer_gated_tools (Stream J)."""
    assert _is_matched_by_patterns(tool_name), (
        f"{tool_name!r} must be gated in approvals.defer_gated_tools. "
        "Stream J should have added Notion write operations to the gating list."
    )


@pytest.mark.parametrize("tool_name", _NOTION_WRITE_TOOLS)
@pytest.mark.asyncio
async def test_notion_write_tools_trigger_defer_hook(tool_name, monkeypatch):
    """Notion write tools must actually trigger permissionDecision='defer'."""
    from agents import hooks
    from tools import approvals as approval_tools

    sent: list = []

    async def fake_send_defer(chat_id, tier, summary):
        sent.append((chat_id, tier, summary))

    monkeypatch.setattr(approval_tools, "send_defer_prompt", fake_send_defer)

    out = await hooks.defer_gated_tools(
        {
            "tool_name": tool_name,
            "tool_use_id": f"tu_{tool_name[:20]}",
            "tool_input": {"page_id": "abc123", "properties": {}},
        },
        None,
        None,
    )

    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "defer", (
        f"Expected defer for {tool_name!r} but got: {out}"
    )


# ---------------------------------------------------------------------------
# GitHub create operations (J-1)
# ---------------------------------------------------------------------------

_GITHUB_CREATE_TOOLS = [
    "mcp__github__create_issue",
    "mcp__github__create_pull_request",
]


@pytest.mark.parametrize("tool_name", _GITHUB_CREATE_TOOLS)
def test_github_create_tools_are_gated(tool_name):
    """GitHub create_issue / create_pull_request must be in defer_gated_tools."""
    assert _is_matched_by_patterns(tool_name), (
        f"{tool_name!r} must be gated in approvals.defer_gated_tools. "
        "Stream J should have added GitHub create operations to the gating list."
    )


@pytest.mark.parametrize("tool_name", _GITHUB_CREATE_TOOLS)
@pytest.mark.asyncio
async def test_github_create_tools_trigger_defer_hook(tool_name, monkeypatch):
    """GitHub create tools must actually trigger permissionDecision='defer'."""
    from agents import hooks
    from tools import approvals as approval_tools

    sent: list = []

    async def fake_send_defer(chat_id, tier, summary):
        sent.append((chat_id, tier, summary))

    monkeypatch.setattr(approval_tools, "send_defer_prompt", fake_send_defer)

    out = await hooks.defer_gated_tools(
        {
            "tool_name": tool_name,
            "tool_use_id": f"tu_{tool_name[:20]}",
            "tool_input": {"title": "test", "body": "test body"},
        },
        None,
        None,
    )

    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "defer", (
        f"Expected defer for {tool_name!r} but got: {out}"
    )


# ---------------------------------------------------------------------------
# Apple Events writes — intentionally NOT gated (Phase 13.1 review).
# Apple Reminders / Calendar writes are local-device only (iPhone/Mac via
# iCloud), not internet-attached, so the spam-attack value from prompt
# injection is low. The bot also relies on these tools to mirror its own
# legitimate reminders. Gating would require either a caller-tag arg the MCP
# server doesn't expose, or a contextvar-based exemption. Skipping the gate
# keeps the mirror path working and avoids name-drift bugs between the prompt
# and the gate regex.
# ---------------------------------------------------------------------------

_APPLE_UNGATED_TOOLS = [
    "mcp__apple_events__create_reminder",
    "mcp__apple_events__delete_reminder",
    "mcp__apple_events__create_calendar_event",
    "mcp__apple_events__delete_calendar_event",
    "mcp__apple_events__reminders_tasks",
]


@pytest.mark.parametrize("tool_name", _APPLE_UNGATED_TOOLS)
def test_apple_events_writes_intentionally_not_gated(tool_name):
    """Apple Events writes must NOT be in defer_gated_tools (Phase 13.1)."""
    assert not _is_matched_by_patterns(tool_name), (
        f"{tool_name!r} appears to be gated, but Phase 13.1 deliberately "
        "leaves Apple Events writes ungated (see config/engagement.yaml comment "
        "near the apple_events block)."
    )


@pytest.mark.parametrize("tool_name", _APPLE_UNGATED_TOOLS)
@pytest.mark.asyncio
async def test_apple_events_writes_do_not_trigger_defer_hook(tool_name, monkeypatch):
    """Apple Events writes must NOT trigger permissionDecision='defer'."""
    from agents import hooks
    from tools import approvals as approval_tools

    sent: list = []

    async def fake_send_defer(chat_id, tier, summary):
        sent.append((chat_id, tier, summary))

    monkeypatch.setattr(approval_tools, "send_defer_prompt", fake_send_defer)

    out = await hooks.defer_gated_tools(
        {
            "tool_name": tool_name,
            "tool_use_id": f"tu_{tool_name[:20]}",
            "tool_input": {"title": "test reminder"},
        },
        None,
        None,
    )

    decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
    assert decision != "defer", (
        f"Expected NO defer for ungated apple_events tool {tool_name!r} but got: {out}"
    )
    assert sent == [], (
        f"send_defer_prompt should not have fired for {tool_name!r}"
    )


# ---------------------------------------------------------------------------
# Google Workspace: existing gated tools still work (non-regression)
# ---------------------------------------------------------------------------

_EXISTING_GW_GATED_TOOLS = [
    "mcp__google_workspace__gmail_send_email",
    "mcp__google_workspace__gmail_bulk_delete_messages",
    "mcp__google_workspace__delete_calendar_event",
    "mcp__google_workspace__drive_delete_file",
]


@pytest.mark.parametrize("tool_name", _EXISTING_GW_GATED_TOOLS)
def test_existing_gw_gated_tools_still_gated(tool_name):
    """Existing Stream A gated tools must not have been accidentally ungated."""
    assert _is_matched_by_patterns(tool_name), (
        f"{tool_name!r} was previously gated and must remain so. "
        "Non-regression check for Stream J changes."
    )
