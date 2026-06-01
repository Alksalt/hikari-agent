"""Phase 15: run_scheduled_action runtime entry + per-turn timeout override.

Pins the contract that:
  - run_scheduled_action sets _CURRENT_TURN_TIMEOUT to the elevated default
  - the chat-path read of sdk_turn_timeout_s consults the contextvar first
  - sdk_pool.in_autonomous_window() is True during the SDK call and False
    before/after (contract #1 — window is module-level, not ContextVar)
  - the gatekeeper bypass fires only when both the flag is set AND the tool
    is in the autonomous-safe whitelist
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from agents import runtime, sdk_pool
from tools import gatekeeper_can_use_tool as gk


def test_context_vars_default_to_inactive():
    """Outside a scheduled action, the flags must be off and timeout None."""
    assert runtime.current_turn_timeout() is None
    assert sdk_pool.in_autonomous_window() is False


def test_run_scheduled_action_signature():
    """The entry point keeps the documented kwargs and defaults."""
    sig = inspect.signature(runtime.run_scheduled_action)
    params = sig.parameters
    assert set(params) == {"seed_prompt", "timeout_s", "max_budget_usd", "max_turns"}
    assert params["timeout_s"].default is None
    assert params["max_budget_usd"].default is None
    assert params["max_turns"].default is None


@pytest.mark.asyncio
async def test_run_scheduled_action_sets_context_vars_then_clears():
    """During the SDK call, _CURRENT_TURN_TIMEOUT must be set and
    sdk_pool.in_autonomous_window() must be True.
    After the call returns, both must reset to defaults."""
    observed = {}

    async def _fake_invoke_sdk(*args, **kwargs):
        observed["timeout"] = runtime.current_turn_timeout()
        observed["action"] = sdk_pool.in_autonomous_window()
        observed["max_turns"] = kwargs.get("max_turns")
        observed["max_budget_usd"] = kwargs.get("max_budget_usd")
        return "ok"

    with patch.object(runtime, "_invoke_sdk", _fake_invoke_sdk):
        result = await runtime.run_scheduled_action(
            "do the thing", timeout_s=240, max_budget_usd=0.5, max_turns=8,
        )

    assert result == "ok"
    assert observed["timeout"] == 240.0
    assert observed["action"] is True
    assert observed["max_turns"] == 8
    assert observed["max_budget_usd"] == 0.5
    # After return, the state must be reset.
    assert runtime.current_turn_timeout() is None
    assert sdk_pool.in_autonomous_window() is False


@pytest.mark.asyncio
async def test_run_scheduled_action_defaults_from_config():
    """When the caller passes no overrides, the entry pulls config values:
    sdk_scheduled_action_timeout_s=180, scheduled_action_max_budget_usd=0.40,
    scheduled_action_max_turns=6."""
    observed = {}

    async def _fake_invoke_sdk(*args, **kwargs):
        observed["timeout"] = runtime.current_turn_timeout()
        observed["max_turns"] = kwargs.get("max_turns")
        observed["max_budget_usd"] = kwargs.get("max_budget_usd")
        return "ok"

    with patch.object(runtime, "_invoke_sdk", _fake_invoke_sdk):
        await runtime.run_scheduled_action("default budgets")

    assert observed["timeout"] == 180.0
    assert observed["max_turns"] == 6
    assert observed["max_budget_usd"] == 0.40


@pytest.mark.asyncio
async def test_run_scheduled_action_resets_contextvars_on_exception():
    """If _invoke_sdk raises, the contextvars and module state must still reset."""
    async def _fake_invoke_sdk(*args, **kwargs):
        raise RuntimeError("simulated SDK failure")

    with patch.object(runtime, "_invoke_sdk", _fake_invoke_sdk):
        with pytest.raises(RuntimeError, match="simulated"):
            await runtime.run_scheduled_action("explode")

    assert runtime.current_turn_timeout() is None
    assert sdk_pool.in_autonomous_window() is False


@pytest.mark.asyncio
async def test_chat_turn_timeout_unaffected_by_run_scheduled_action():
    """run_user_turn must NOT set the timeout contextvar — only scheduled
    actions get the bumped budget."""
    observed_during_chat = {}

    async def _fake_invoke_sdk(*args, **kwargs):
        observed_during_chat["timeout"] = runtime.current_turn_timeout()
        observed_during_chat["action"] = sdk_pool.in_autonomous_window()
        return "ok"

    with patch.object(runtime, "_invoke_sdk", _fake_invoke_sdk):
        await runtime.run_user_turn("hi")

    assert observed_during_chat["timeout"] is None
    assert observed_during_chat["action"] is False  # window stays False during normal turns


# ---------------------------------------------------------------------------
# Gatekeeper bypass — only when both the flag is set AND tool is whitelisted
# ---------------------------------------------------------------------------

class _FakeContext:
    """Duck-types ToolPermissionContext for the can_use_tool callable."""
    def __init__(self) -> None:
        self.tool_use_id = "test-tool-use-id-12345"


@pytest.mark.asyncio
async def test_gatekeeper_bypasses_notion_write_in_autonomous_action(monkeypatch):
    """When autonomous mode is on and the tool is autonomous-action-safe,
    the gatekeeper must allow without calling GATEKEEPER.request."""
    from claude_agent_sdk.types import PermissionResultAllow

    # Pretend notion-post-page is gated (it is in real tools.yaml).
    class _StubSpec:
        gate = "gatekeeper"
        access_mode = "write"

    monkeypatch.setattr(gk, "_resolve_spec_and_kind",
                        lambda name: (_StubSpec(), "exact"))
    monkeypatch.setattr(gk, "_resolve_chat_id", lambda: 12345)

    request_mock = AsyncMock()
    from tools.gatekeeper import GATEKEEPER
    monkeypatch.setattr(GATEKEEPER, "request", request_mock)

    sdk_pool.set_autonomous_window(True)
    try:
        result = await gk.gatekeeper_can_use_tool(
            "mcp__notion__API-post-page",
            {"parent": {"data_source_id": "abc"}, "properties": {}},
            _FakeContext(),
        )
    finally:
        sdk_pool.set_autonomous_window(False)

    assert isinstance(result, PermissionResultAllow)
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_gatekeeper_still_gates_non_whitelisted_tool_in_autonomous_action(monkeypatch):
    """Even in autonomous mode, a non-whitelisted gated tool (gmail send,
    notion delete) must still hit GATEKEEPER.request."""

    class _StubSpec:
        gate = "gatekeeper"
        access_mode = "write"

    monkeypatch.setattr(gk, "_resolve_spec_and_kind",
                        lambda name: (_StubSpec(), "exact"))
    monkeypatch.setattr(gk, "_resolve_chat_id", lambda: 12345)

    request_mock = AsyncMock(return_value="approved")
    from tools.gatekeeper import GATEKEEPER
    monkeypatch.setattr(GATEKEEPER, "request", request_mock)

    sdk_pool.set_autonomous_window(True)
    try:
        await gk.gatekeeper_can_use_tool(
            "mcp__notion__API-delete-a-block",   # NOT whitelisted
            {"block_id": "abc"},
            _FakeContext(),
        )
    finally:
        sdk_pool.set_autonomous_window(False)

    request_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_gatekeeper_gates_normally_outside_autonomous_action(monkeypatch):
    """When autonomous flag is off (default), even a whitelisted tool gates."""

    class _StubSpec:
        gate = "gatekeeper"
        access_mode = "write"

    monkeypatch.setattr(gk, "_resolve_spec_and_kind",
                        lambda name: (_StubSpec(), "exact"))
    monkeypatch.setattr(gk, "_resolve_chat_id", lambda: 12345)

    request_mock = AsyncMock(return_value="approved")
    from tools.gatekeeper import GATEKEEPER
    monkeypatch.setattr(GATEKEEPER, "request", request_mock)

    assert sdk_pool.in_autonomous_window() is False

    await gk.gatekeeper_can_use_tool(
        "mcp__notion__API-post-page",
        {"parent": {"data_source_id": "abc"}, "properties": {}},
        _FakeContext(),
    )

    request_mock.assert_awaited_once()


def test_autonomous_safe_set_excludes_destructive_notion():
    """The whitelist must not include the high-risk delete operation."""
    assert "mcp__notion__API-delete-a-block" not in gk._AUTONOMOUS_ACTION_SAFE_TOOLS


def test_autonomous_safe_set_excludes_non_notion_writes():
    """Whitelist scope is Notion only — gmail/drive/github writes still gate."""
    for tool in [
        "mcp__google_workspace__gmail_send_email",
        "mcp__google_workspace__delete_calendar_event",
        "mcp__github__merge_pull_request",
        "mcp__github__create_pull_request",
    ]:
        assert tool not in gk._AUTONOMOUS_ACTION_SAFE_TOOLS
