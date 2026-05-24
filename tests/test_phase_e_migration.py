"""Phase E migration: every destructive tool routes through Gatekeeper."""
from __future__ import annotations

import pytest

from tools._tools_yaml import load_registry

_EXPECTED_GATEKEEPER_TOOLS = {
    "mcp__hikari_dispatch__dispatch_claude_session",
    "mcp__hikari_utility__python_run",
    "mcp__google_workspace__gmail_send_email",
    "mcp__google_workspace__gmail_reply_to_email",
    "mcp__google_workspace__gmail_bulk_delete_messages",
    "mcp__google_workspace__delete_calendar_event",
    "mcp__google_workspace__drive_delete_file",
    "mcp__google_workspace__create_calendar_event",
    "mcp__google_workspace__drive_delete_folder",
    "mcp__google_workspace__drive_upload_file",
    "mcp__notion__API-patch-block-children",
    "mcp__notion__API-update-a-block",
    "mcp__notion__API-delete-a-block",
    "mcp__notion__API-patch-page",
    "mcp__notion__API-post-page",
    "mcp__github__create_issue",
    "mcp__github__create_pull_request",
    "mcp__github__merge_pull_request",
    "mcp__github__delete_file",
    "mcp__github__delete_repository",
}


def test_all_destructive_tools_routed_through_gatekeeper():
    registry = load_registry()
    actual = {t.id for t in registry.specs() if t.gate == "gatekeeper"}
    missing = _EXPECTED_GATEKEEPER_TOOLS - actual
    assert not missing, f"tools not on gatekeeper: {missing}"


def test_no_defer_gated_tools_remain():
    """Phase 6C: gate='defer' is dead — no tools should use it."""
    registry = load_registry()
    deferred = {t.id for t in registry.specs() if t.gate == "defer"}
    assert not deferred, f"defer-gated tools still present: {deferred}"


@pytest.mark.parametrize("tool_id", sorted(_EXPECTED_GATEKEEPER_TOOLS))
def test_gatekeeper_summarize_handles_tool(tool_id):
    from tools.gatekeeper import summarize
    out = summarize(tool_id, {})
    assert isinstance(out, str) and out, f"summarize({tool_id}) returned {out!r}"
