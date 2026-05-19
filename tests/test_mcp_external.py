"""Phase 7 — external MCP server: bearer auth, tool registration, wrap-untrusted
on outputs, audit logging."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------- bearer-token check ----------

def test_bearer_rejects_when_secret_unset(monkeypatch):
    """If HIKARI_MCP_SECRET is unset, no token is ever valid — refuse
    to authenticate by default."""
    monkeypatch.delenv("HIKARI_MCP_SECRET", raising=False)
    from mcp_external import server
    assert not server.check_bearer_token("anything")
    assert not server.check_bearer_token("")
    assert not server.check_bearer_token(None)


def test_bearer_accepts_correct_token(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external import server
    assert server.check_bearer_token("abc123")
    assert server.check_bearer_token("Bearer abc123")
    assert server.check_bearer_token("bearer abc123")  # case-insensitive prefix


def test_bearer_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external import server
    assert not server.check_bearer_token("xyz789")
    assert not server.check_bearer_token("Bearer xyz789")


def test_bearer_rejects_empty(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external import server
    assert not server.check_bearer_token("")
    assert not server.check_bearer_token(None)


def test_bearer_constant_time_compare(monkeypatch):
    """sanity — secrets.compare_digest is invoked (no early return on
    first mismatch). Hard to assert directly; we test that prefixes don't
    leak by checking that 'abc' and 'abc123' both rejected when secret is 'abc123'."""
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external import server
    assert not server.check_bearer_token("abc")
    assert server.check_bearer_token("abc123")
    assert not server.check_bearer_token("abc1234")


# ---------- server construction + tool registration ----------

def test_build_server_registers_five_tools(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-secret")
    from mcp_external.server import build_server
    server_inst = build_server()
    # FastMCP exposes _tool_manager / list_tools in different versions; use
    # the public list_tools method if available, else inspect _tool_manager.
    import asyncio
    if hasattr(server_inst, "list_tools"):
        # FastMCP.list_tools is async
        tools = asyncio.run(server_inst.list_tools())
        tool_names = {t.name for t in tools}
    else:  # fallback to internal manager
        tools = server_inst._tool_manager._tools
        tool_names = set(tools.keys())
    expected = {
        "hikari_recall", "hikari_lexicon_top", "hikari_observations",
        "hikari_open_loops", "hikari_wiki_search",
    }
    assert expected.issubset(tool_names), f"missing tools: {expected - tool_names}"


# ---------- tool outputs are wrapped + audit-logged ----------

@pytest.mark.asyncio
async def test_hikari_lexicon_top_wraps_and_audits(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-secret")
    db.lexicon_record("attention sinks", source="mutual")
    db.lexicon_record("attention sinks")  # bump weight

    from mcp_external.server import build_server
    server_inst = build_server()
    # Call the tool directly via the FastMCP tool manager.
    tool = server_inst._tool_manager._tools["hikari_lexicon_top"]
    result = await tool.fn(limit=3)
    assert "attention sinks" in result
    # Wrap delimiters present.
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in result
    assert "<<<HIKARI_UNTRUSTED_END>>>" in result
    # Audit row written.
    with db._conn() as c:
        rows = c.execute(
            "SELECT tool FROM audit_log WHERE tool LIKE 'external_mcp:%'"
        ).fetchall()
    assert any("lexicon_top" in r["tool"] for r in rows)


@pytest.mark.asyncio
async def test_hikari_open_loops_empty_case(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-secret")
    from mcp_external.server import build_server
    server_inst = build_server()
    tool = server_inst._tool_manager._tools["hikari_open_loops"]
    result = await tool.fn()
    assert "no open loops" in result.lower()
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in result


@pytest.mark.asyncio
async def test_hikari_open_loops_with_data(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-secret")
    db.create_task("ask about the cabbage")
    from mcp_external.server import build_server
    server_inst = build_server()
    tool = server_inst._tool_manager._tools["hikari_open_loops"]
    result = await tool.fn()
    assert "cabbage" in result
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in result


@pytest.mark.asyncio
async def test_hikari_observations_empty(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-secret")
    from mcp_external.server import build_server
    server_inst = build_server()
    tool = server_inst._tool_manager._tools["hikari_observations"]
    result = await tool.fn(min_confidence=0.6, limit=3)
    assert "no observations queued" in result.lower()


@pytest.mark.asyncio
async def test_hikari_observations_with_data(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-secret")
    db.observation_record("recurrence", "11pm-quiet",
                          "goes quiet near 11pm", 0.85)
    from mcp_external.server import build_server
    server_inst = build_server()
    tool = server_inst._tool_manager._tools["hikari_observations"]
    result = await tool.fn(min_confidence=0.6, limit=3)
    assert "11pm" in result


# ---------- BearerAuthMiddleware ASGI behavior ----------

@pytest.mark.asyncio
async def test_middleware_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external.launch import BearerAuthMiddleware

    async def fake_app(scope, receive, send):
        # Should never reach here on unauthorized.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})

    sent: list[dict] = []

    async def fake_send(msg):
        sent.append(msg)

    mw = BearerAuthMiddleware(fake_app)
    scope = {"type": "http", "headers": []}  # no Authorization header
    await mw(scope, lambda: None, fake_send)
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_middleware_accepts_valid_bearer(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external.launch import BearerAuthMiddleware

    inner_called = {"v": False}

    async def fake_app(scope, receive, send):
        inner_called["v"] = True

    mw = BearerAuthMiddleware(fake_app)
    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer abc123")],
    }
    await mw(scope, lambda: None, lambda msg: None)
    assert inner_called["v"]


@pytest.mark.asyncio
async def test_middleware_passes_non_http_traffic(monkeypatch):
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external.launch import BearerAuthMiddleware

    inner_called = {"v": False}

    async def fake_app(scope, receive, send):
        inner_called["v"] = True

    mw = BearerAuthMiddleware(fake_app)
    scope = {"type": "lifespan", "headers": []}
    await mw(scope, lambda: None, lambda msg: None)
    assert inner_called["v"]


@pytest.mark.asyncio
async def test_middleware_passes_websocket_scope(monkeypatch):
    """WebSocket traffic isn't used by Streamable HTTP MCP today, but the
    middleware's gate is 'http only checked' — anything else passes through.
    If FastMCP ever adds WebSocket support, we want to know that the gate
    behaves consistently with the documented contract."""
    monkeypatch.setenv("HIKARI_MCP_SECRET", "abc123")
    from mcp_external.launch import BearerAuthMiddleware

    inner_called = {"v": False}

    async def fake_app(scope, receive, send):
        inner_called["v"] = True

    mw = BearerAuthMiddleware(fake_app)
    scope = {"type": "websocket", "headers": []}
    await mw(scope, lambda: None, lambda msg: None)
    assert inner_called["v"]
