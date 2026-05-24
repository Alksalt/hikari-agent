"""Stream A gating policy regression: Gmail sends, calendar event delete, and
Drive file delete must all be gatekeeper-gated after Phase E.

Phase E: all tools migrated from gate: defer → gate: gatekeeper.
The defer hook no longer fires for any of these tools — they are gated through
Gatekeeper.canUseTool instead.
"""
from __future__ import annotations

import importlib
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


# Phase E: all previously defer-gated workspace tools are now gatekeeper-gated.
_GATEKEEPER_GATED_TOOLS = [
    "mcp__google_workspace__gmail_send_email",
    "mcp__google_workspace__gmail_reply_to_email",
    "mcp__google_workspace__gmail_bulk_delete_messages",
    "mcp__google_workspace__delete_calendar_event",
    "mcp__google_workspace__drive_delete_file",
    "mcp__google_workspace__create_calendar_event",
    "mcp__google_workspace__drive_delete_folder",
    "mcp__google_workspace__drive_upload_file",
]


def test_no_defer_gated_tools_remain():
    """Phase 6C: gate='defer' is dead — all tools must use 'gatekeeper' or null."""
    from tools._tools_yaml import load_registry
    deferred = [t.id for t in load_registry().specs() if t.gate == "defer"]
    assert not deferred, (
        f"Unexpected gate:defer tools (dead path): {deferred}"
    )


@pytest.mark.parametrize("tool_name", _GATEKEEPER_GATED_TOOLS)
def test_gw_tools_are_gatekeeper_gated(tool_name):
    """Phase E: Google Workspace destructive tools must have gate: gatekeeper."""
    from tools._tools_yaml import load_registry
    spec = load_registry()._resolve(tool_name)
    assert spec is not None, f"No registry entry for {tool_name}"
    assert spec.gate == "gatekeeper", (
        f"{tool_name!r} must have gate: gatekeeper after Phase E migration."
    )


@pytest.mark.parametrize("tool_name", _GATEKEEPER_GATED_TOOLS)
@pytest.mark.asyncio
async def test_defer_hook_does_not_fire_for_gatekeeper_tool(tool_name, monkeypatch):
    """Phase E: gatekeeper-gated tools must NOT trigger the defer hook.

    These tools are now gated through Gatekeeper.canUseTool, not PreToolUse defer.
    The defer hook must return {} (pass-through) for them.
    """
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
            "tool_input": {"dummy": "value"},
        },
        None,
        None,
    )

    decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
    assert decision != "defer", (
        f"Expected NO defer for gatekeeper-gated tool {tool_name!r} but got: {out}"
    )
    assert sent == [], (
        f"send_defer_prompt should not have fired for gatekeeper tool {tool_name!r}"
    )
