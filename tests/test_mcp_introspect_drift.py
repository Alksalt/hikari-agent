"""Sprint 4 Phase 4C-3c -- MCP server introspection + drift detection."""
import asyncio
import pathlib
import sys
import pytest


FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fake_mcp_server.py"

# A one-liner that reads stdin forever and never writes -- used to test timeout.
_SILENT_SERVER = ["-c", "import sys, time\nfor _ in sys.stdin: time.sleep(1)"]


async def test_list_server_tools_returns_fake_manifest():
    from tools.mcp_introspect import list_server_tools
    tools = await list_server_tools(sys.executable, (str(FIXTURE),), timeout_sec=5.0)
    assert tools == {"fake_read", "fake_destructive_write"}


async def test_list_server_tools_timeout():
    """A server that never responds must raise asyncio.TimeoutError within the budget."""
    from tools.mcp_introspect import list_server_tools
    with pytest.raises(asyncio.TimeoutError):
        await list_server_tools(sys.executable, tuple(_SILENT_SERVER), timeout_sec=0.5)


async def test_introspect_all_returns_per_server():
    from tools.mcp_introspect import introspect_all
    servers = {
        "fake": {"command": sys.executable, "args": [str(FIXTURE)]},
    }
    result = await introspect_all(servers, timeout_sec=5.0)
    assert "fake" in result
    assert result["fake"] == {"fake_read", "fake_destructive_write"}


async def test_introspect_all_handles_failure():
    from tools.mcp_introspect import introspect_all
    servers = {
        "bad": {"command": "/nonexistent/binary", "args": []},
    }
    result = await introspect_all(servers, timeout_sec=2.0)
    assert "bad" in result
    assert isinstance(result["bad"], Exception)
