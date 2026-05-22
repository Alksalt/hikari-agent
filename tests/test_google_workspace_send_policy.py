"""Stream A gating policy regression: Gmail sends, calendar event delete, and
Drive file delete must all be in the defer_gated_tools list and must actually
trigger the defer hook on a fake PreToolUse invocation.

Phase E: gmail_bulk_delete_messages has been migrated from gate: defer to
gate: gatekeeper. It is excluded from the defer-path assertions here and
tested separately in test_gatekeeper_integration.py.
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


# Phase E: gmail_bulk_delete_messages removed from defer list (now gatekeeper-gated).
_DEFER_GATED_TOOLS = [
    "mcp__google_workspace__gmail_send_email",
    "mcp__google_workspace__gmail_reply_to_email",
    "mcp__google_workspace__delete_calendar_event",
    "mcp__google_workspace__drive_delete_file",
]


def _is_matched_by_patterns(tool_name: str) -> bool:
    """Replicate the exact matching logic from agents/hooks.py:_is_defer_gated.

    Phase A (step 9): defer_gated_tools removed from engagement.yaml;
    patterns now sourced from tools._tools_yaml registry with config fallback.
    """
    cfg_patterns = config.get("approvals.defer_gated_tools")
    if cfg_patterns is not None:
        patterns = cfg_patterns
    else:
        from tools._tools_yaml import load_registry
        patterns = load_registry().defer_gated_patterns()
    for pat in patterns:
        try:
            if re.fullmatch(str(pat), tool_name):
                return True
        except re.error:
            pass
    return False


def test_defer_gated_tools_contains_required_patterns():
    """config/tools.yaml must list patterns that match every Stream A defer-gated tool.

    Phase A (step 9): source is now tools.yaml registry, not engagement.yaml.
    Phase E: gmail_bulk_delete_messages excluded (now gatekeeper-gated).
    """
    from tools._tools_yaml import load_registry
    patterns = load_registry().defer_gated_patterns()
    assert patterns, "tools.yaml defer_gated_patterns() is empty"

    for tool_name in _DEFER_GATED_TOOLS:
        assert _is_matched_by_patterns(tool_name), (
            f"{tool_name!r} is not matched by any pattern in "
            f"tools.yaml defer_gated_patterns: {patterns}"
        )


@pytest.mark.parametrize("tool_name", _DEFER_GATED_TOOLS)
@pytest.mark.asyncio
async def test_defer_hook_fires_for_gated_workspace_tool(tool_name, monkeypatch):
    """A fake PreToolUse event for each defer-gated tool actually triggers defer.

    Phase E: gmail_bulk_delete_messages excluded (now gatekeeper-gated, not defer).
    """
    from agents import hooks
    from tools import approvals as approval_tools

    # Stub OOB telegram prompt so no real network call is made.
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

    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "defer", (
        f"Expected defer for {tool_name!r} but got: {out}"
    )
    assert len(sent) == 1, (
        f"Expected 1 OOB prompt for {tool_name!r} but sent {len(sent)}"
    )
