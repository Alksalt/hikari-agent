"""Phase 13.1 (Stream K) — regression: destructive tool gating coverage.

Phase E update: all destructive tools migrated from gate: defer → gate: gatekeeper.
The old _is_matched_by_patterns / defer hook trigger tests are replaced with
gatekeeper-gate assertions.

Phase 4 (control-plane-lies sweep): Apple Events writes migrated from
gate: confirm_send → gate: gatekeeper, unified onto the single owner-approval
state machine.
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


def _gate_for(tool_name: str) -> str | None:
    """Return the gate value from the registry for a given tool name."""
    from tools._tools_yaml import load_registry
    spec = load_registry()._resolve(tool_name)
    return spec.gate if spec else None


# ---------------------------------------------------------------------------
# Notion write operations — Phase E: all gate: gatekeeper
# ---------------------------------------------------------------------------

_NOTION_WRITE_TOOLS = [
    "mcp__notion__API-patch-block-children",
    "mcp__notion__API-patch-page",
    "mcp__notion__API-post-page",
    "mcp__notion__API-update-a-block",
    "mcp__notion__API-delete-a-block",
]


@pytest.mark.parametrize("tool_name", _NOTION_WRITE_TOOLS)
def test_notion_write_tools_are_gatekeeper_gated(tool_name):
    """Phase E: Notion write operations must have gate: gatekeeper."""
    assert _gate_for(tool_name) == "gatekeeper", (
        f"{tool_name!r} must have gate: gatekeeper after Phase E migration."
    )




# ---------------------------------------------------------------------------
# GitHub operations — Phase E: all gate: gatekeeper
# ---------------------------------------------------------------------------

_GITHUB_CREATE_TOOLS = [
    "mcp__github__create_issue",
    "mcp__github__create_pull_request",
    "mcp__github__merge_pull_request",
    "mcp__github__delete_file",
    "mcp__github__delete_repository",
]


@pytest.mark.parametrize("tool_name", _GITHUB_CREATE_TOOLS)
def test_github_create_tools_are_gatekeeper_gated(tool_name):
    """Phase E: GitHub destructive tools must have gate: gatekeeper."""
    assert _gate_for(tool_name) == "gatekeeper", (
        f"{tool_name!r} must have gate: gatekeeper after Phase E migration."
    )




# ---------------------------------------------------------------------------
# Apple Events writes — Phase 4 (control-plane-lies sweep): all gate: gatekeeper.
# Apple Reminders / Calendar writes are now owner-gated through the single
# gatekeeper state machine.  The _unsafe variants (gate: null) remain the
# scheduler-internal MANAGER.call() bypass and are never presented to the LLM.
# ---------------------------------------------------------------------------

_APPLE_GATEKEEPER_TOOLS = [
    "mcp__apple_events__create_reminder",
    "mcp__apple_events__delete_reminder",
    "mcp__apple_events__create_calendar_event",
    "mcp__apple_events__delete_calendar_event",
    "mcp__apple_events__reminders_tasks",
]


@pytest.mark.parametrize("tool_name", _APPLE_GATEKEEPER_TOOLS)
def test_apple_events_writes_are_gatekeeper_gated(tool_name):
    """Phase 4: Apple Events writes must have gate: gatekeeper."""
    assert _gate_for(tool_name) == "gatekeeper", (
        f"{tool_name!r} must have gate: gatekeeper after Phase 4 migration."
    )


@pytest.mark.parametrize("tool_name", _APPLE_GATEKEEPER_TOOLS)
@pytest.mark.asyncio
async def test_apple_events_writes_do_not_trigger_defer_hook(tool_name, monkeypatch):
    """Apple Events writes must NOT trigger the legacy permissionDecision='defer' hook.
    (They go through the gatekeeper state machine instead.)"""
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
# Google Workspace: Phase E — all gatekeeper-gated
# ---------------------------------------------------------------------------

_GW_GATEKEEPER_TOOLS = [
    "mcp__google_workspace__gmail_send_email",
    "mcp__google_workspace__gmail_reply_to_email",
    "mcp__google_workspace__delete_calendar_event",
    "mcp__google_workspace__drive_delete_file",
    "mcp__google_workspace__create_calendar_event",
    "mcp__google_workspace__drive_delete_folder",
    "mcp__google_workspace__drive_upload_file",
]


@pytest.mark.parametrize("tool_name", _GW_GATEKEEPER_TOOLS)
def test_gw_tools_are_gatekeeper_gated(tool_name):
    """Phase E: all Google Workspace destructive tools must have gate: gatekeeper."""
    assert _gate_for(tool_name) == "gatekeeper", (
        f"{tool_name!r} must have gate: gatekeeper after Phase E migration."
    )


def test_gmail_bulk_delete_is_not_registered():
    """Bulk deletion is hidden entirely instead of being approval-gated."""
    from tools._tools_yaml import load_registry
    spec = load_registry()._resolve("mcp__google_workspace__gmail_bulk_delete_messages")
    assert spec is None
