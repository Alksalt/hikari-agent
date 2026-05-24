"""Tests for McpManager.call() — direct MCP invocation surface (Sprint 7B).

All tests mock mcp.client.stdio.stdio_client and mcp.ClientSession so no real
subprocess is spawned.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from agents.mcp_manager import McpCallError, McpManager, _result_to_dict

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_text_result(text: str, is_error: bool = False) -> CallToolResult:
    content = [TextContent(type="text", text=text)]
    return CallToolResult(content=content, isError=is_error)


def _make_structured_result(data: dict) -> CallToolResult:
    return CallToolResult(content=[], structuredContent=data, isError=False)


def _make_manager() -> McpManager:
    """Fresh McpManager with a real .mcp.json read stubbed out."""
    return McpManager()


def _patch_spawn(session_mock: AsyncMock):
    """Context manager that patches _spawn_session to return (session, exit_stack)."""
    stack_mock = AsyncMock()
    stack_mock.aclose = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield session_mock, stack_mock

    return patch(
        "agents.mcp_manager._spawn_session",
        new=AsyncMock(return_value=(session_mock, stack_mock)),
    ), stack_mock


# ---------------------------------------------------------------------------
# _result_to_dict unit tests
# ---------------------------------------------------------------------------

def test_result_to_dict_structured():
    result = _make_structured_result({"foo": "bar", "n": 42})
    assert _result_to_dict(result) == {"foo": "bar", "n": 42}


def test_result_to_dict_text():
    result = _make_text_result("hello world")
    assert _result_to_dict(result) == {"text": "hello world"}


def test_result_to_dict_empty():
    result = CallToolResult(content=[], isError=False)
    assert _result_to_dict(result) == {}


# ---------------------------------------------------------------------------
# test_call_spawns_session_lazily
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_spawns_session_lazily():
    """First call to call() triggers spawn; second call reuses the session."""
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("ok"))

    stack = AsyncMock()
    stack.aclose = AsyncMock()

    spawn_call_count = 0

    async def fake_spawn(server_name: str):
        nonlocal spawn_call_count
        spawn_call_count += 1
        return session, stack

    manager = _make_manager()

    with patch("agents.mcp_manager._spawn_session", side_effect=fake_spawn):
        r1 = await manager.call("apple_events", "calendar_events", {})
        r2 = await manager.call("apple_events", "calendar_events", {})

    assert spawn_call_count == 1, "spawn should only happen once"
    assert session.call_tool.call_count == 2
    assert r1 == {"text": "ok"}
    assert r2 == {"text": "ok"}


# ---------------------------------------------------------------------------
# test_call_normalizes_result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_normalizes_result():
    """Structured content comes back as a plain dict."""
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_make_structured_result({"events": ["a", "b"]})
    )
    stack = AsyncMock()

    manager = _make_manager()
    with patch(
        "agents.mcp_manager._spawn_session",
        new=AsyncMock(return_value=(session, stack)),
    ):
        result = await manager.call("google_workspace", "calendar_get_events", {"maxResults": 5})

    assert result == {"events": ["a", "b"]}


# ---------------------------------------------------------------------------
# test_call_propagates_mcp_error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_propagates_mcp_error():
    """isError=True in CallToolResult raises McpCallError with the error text."""
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_make_text_result("server exploded", is_error=True)
    )
    stack = AsyncMock()

    manager = _make_manager()
    with patch(
        "agents.mcp_manager._spawn_session",
        new=AsyncMock(return_value=(session, stack)),
    ):
        with pytest.raises(McpCallError) as exc_info:
            await manager.call("notion", "API-post-search", {"query": "test"})

    err = exc_info.value
    assert err.server == "notion"
    assert err.tool == "API-post-search"
    assert "server exploded" in err.message
    assert "notion" in str(err)


@pytest.mark.asyncio
async def test_call_propagates_exception_as_mcp_error():
    """Exception raised by session.call_tool wraps as McpCallError."""
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("connection reset"))
    stack = AsyncMock()

    manager = _make_manager()
    with patch(
        "agents.mcp_manager._spawn_session",
        new=AsyncMock(return_value=(session, stack)),
    ):
        with pytest.raises(McpCallError) as exc_info:
            await manager.call("github", "search_code", {"query": "foo"})

    err = exc_info.value
    assert err.server == "github"
    assert err.tool == "search_code"
    assert "connection reset" in err.message


# ---------------------------------------------------------------------------
# test_call_bumps_ttl
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_bumps_ttl():
    """acquire() is called after a successful tool call to bump the warm-pool TTL."""
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("data"))
    stack = AsyncMock()

    manager = _make_manager()
    acquire_calls: list[str] = []
    original_acquire = manager.acquire

    async def recording_acquire(server_name: str) -> None:
        acquire_calls.append(server_name)
        await original_acquire(server_name)

    manager.acquire = recording_acquire  # type: ignore[method-assign]

    with patch(
        "agents.mcp_manager._spawn_session",
        new=AsyncMock(return_value=(session, stack)),
    ):
        await manager.call("apple_events", "calendar_events", {})

    assert "apple_events" in acquire_calls, "acquire should be called with the server name"


# ---------------------------------------------------------------------------
# test_shutdown_closes_all_sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shutdown_closes_all_sessions():
    """shutdown_sessions() closes the exit stack for every cached session."""
    stack_a = AsyncMock()
    stack_a.aclose = AsyncMock()
    stack_b = AsyncMock()
    stack_b.aclose = AsyncMock()

    session_a = AsyncMock()
    session_a.call_tool = AsyncMock(return_value=_make_text_result("a"))
    session_b = AsyncMock()
    session_b.call_tool = AsyncMock(return_value=_make_text_result("b"))

    spawn_results = {
        "google_workspace": (session_a, stack_a),
        "notion": (session_b, stack_b),
    }

    async def fake_spawn(server_name: str):
        return spawn_results[server_name]

    manager = _make_manager()
    with patch("agents.mcp_manager._spawn_session", side_effect=fake_spawn):
        await manager.call("google_workspace", "calendar_get_events", {})
        await manager.call("notion", "API-post-search", {"query": "x"})

    await manager.shutdown_sessions()

    stack_a.aclose.assert_awaited_once()
    stack_b.aclose.assert_awaited_once()
    assert manager._sessions == {}


# ---------------------------------------------------------------------------
# test_concurrent_calls_share_session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_calls_share_session():
    """5 concurrent calls to the same server trigger exactly 1 spawn."""
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("concurrent"))
    stack = AsyncMock()

    spawn_call_count = 0

    async def fake_spawn(server_name: str):
        nonlocal spawn_call_count
        # Small delay to make concurrent races more likely to surface double-spawn
        await asyncio.sleep(0)
        spawn_call_count += 1
        return session, stack

    manager = _make_manager()
    with patch("agents.mcp_manager._spawn_session", side_effect=fake_spawn):
        results = await asyncio.gather(
            *[manager.call("playwright", "browser_snapshot", {}) for _ in range(5)]
        )

    assert spawn_call_count == 1, f"expected 1 spawn, got {spawn_call_count}"
    assert all(r == {"text": "concurrent"} for r in results)
    assert session.call_tool.call_count == 5


# ---------------------------------------------------------------------------
# McpCallError class contract
# ---------------------------------------------------------------------------

def test_mcp_call_error_attributes():
    err = McpCallError("srv", "tool", "bad thing happened")
    assert err.server == "srv"
    assert err.tool == "tool"
    assert err.message == "bad thing happened"
    assert "srv" in str(err)
    assert "tool" in str(err)
    assert "bad thing happened" in str(err)
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# _build_server_params / _load_mcp_json integration (light)
# ---------------------------------------------------------------------------

def test_build_server_params_reads_mcp_json():
    """_build_server_params returns a StdioServerParameters for a known server."""
    from agents.mcp_manager import _build_server_params

    params = _build_server_params("notion")
    assert params.command == "npx"
    assert any("notion-mcp-server" in a for a in params.args)


def test_build_server_params_missing_server():
    from agents.mcp_manager import _build_server_params

    with pytest.raises(KeyError, match="nonexistent_server"):
        _build_server_params("nonexistent_server")
