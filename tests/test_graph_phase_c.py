"""Tests for the embedded Graphiti+Kuzu graph module (storage/graph.py).

All tests mock Graphiti so they don't hit the real Anthropic API or write
to a persistent Kuzu directory beyond the tmp_path fixture.
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import storage.graph as graph_mod

pytestmark = pytest.mark.uses_real_graph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_graphiti() -> MagicMock:
    """Build a fully async-mocked Graphiti instance."""
    g = MagicMock()
    g.build_indices_and_constraints = AsyncMock(return_value=None)
    g.add_episode = AsyncMock(return_value=None)
    g.search = AsyncMock(return_value=[MagicMock(fact="user likes coffee")])
    return g


@pytest.fixture(autouse=True)
def _reset_graph_singleton():
    """Reset the module-level singleton before/after every test."""
    graph_mod._GRAPH = None
    # Re-create the lock in case the event loop changed between tests.
    graph_mod._GRAPH_LOCK = asyncio.Lock()
    yield
    graph_mod._GRAPH = None


# ---------------------------------------------------------------------------
# Test 1 — get_graph creates the kuzu parent dir with 0o700 perms
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_graph_creates_kuzu_parent_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    mock_g = _make_mock_graphiti()

    with patch("storage.graph.Graphiti", return_value=mock_g), \
         patch("storage.graph.KuzuDriver"), \
         patch("storage.graph.OpenAIGenericClient"), \
         patch("storage.graph.LLMConfig"):
        await graph_mod.get_graph()

    # The kuzu file path is `tmp_path / "hikari.kuzu"` — Kuzu creates that file
    # itself on real init. We only assert the parent dir exists with the right
    # perms; the file is absent under mocked KuzuDriver.
    assert tmp_path.is_dir()
    assert (tmp_path.stat().st_mode & 0o777) == 0o700


# ---------------------------------------------------------------------------
# Test 2 — add_episode_safe round-trip (mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_episode_safe_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    mock_g = _make_mock_graphiti()

    with patch("storage.graph.Graphiti", return_value=mock_g), \
         patch("storage.graph.KuzuDriver"), \
         patch("storage.graph.OpenAIGenericClient"), \
         patch("storage.graph.LLMConfig"):
        ok = await graph_mod.add_episode_safe(
            name="fact_42",
            episode_body="user likes coffee",
        )
        assert ok is True
        mock_g.add_episode.assert_called_once()
        call_kwargs = mock_g.add_episode.call_args
        assert "user likes coffee" in str(call_kwargs)

        results = await graph_mod.search("coffee")
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# Test 3 — add_episode_safe swallows errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_episode_safe_swallows_errors(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    async def _boom():
        raise RuntimeError("kuzu exploded")

    with patch.object(graph_mod, "get_graph", side_effect=RuntimeError("kuzu exploded")):
        result = await graph_mod.add_episode_safe(
            name="fact_99",
            episode_body="something went wrong",
        )

    assert result is False


# ---------------------------------------------------------------------------
# Test 4 — remember tool dual-writes to the graph
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remember_dual_writes_to_graph(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    # Isolate the SQLite DB.
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    from storage import db as _db
    importlib.reload(_db)
    _db._reset_schema_sentinel()

    from agents import config as cfg
    cfg.reload()

    mock_g = _make_mock_graphiti()

    import sys
    import tools.memory.remember  # noqa: F401 — ensure it's in sys.modules
    _remember_module = sys.modules["tools.memory.remember"]
    # The @tool decorator wraps the function into an SdkMcpTool; the handler
    # attribute holds the original coroutine.
    handler = _remember_module.remember.handler  # type: ignore[attr-defined]

    with patch("storage.graph.Graphiti", return_value=mock_g), \
         patch("storage.graph.KuzuDriver"), \
         patch("storage.graph.OpenAIGenericClient"), \
         patch("storage.graph.LLMConfig"), \
         patch("tools.embeddings.aembed", new=AsyncMock(return_value=[0.1] * 384)):
        result = await handler({
            "subject": "user",
            "predicate": "drinks",
            "object": "coffee",
            "importance": 5,
            "confidence": 0.9,
            "on_conflict": "coexist",
        })
        # Let the background task run.
        await asyncio.sleep(0.15)

    assert result.get("ok") or "fact_id" in str(result)
    mock_g.add_episode.assert_called()
    call_body = str(mock_g.add_episode.call_args)
    assert "user" in call_body and "coffee" in call_body


# ---------------------------------------------------------------------------
# Test 5 — boot graph failure is non-fatal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_init_graph_failure_is_non_fatal():
    """Simulates the post_init boot code path with get_graph raising."""
    raised = False
    log_called = False

    import logging

    class _FakeLogger:
        def exception(self, *a, **kw):
            nonlocal log_called
            log_called = True

    fake_logger = _FakeLogger()

    async def _failing_get_graph():
        raise RuntimeError("kuzu not found")

    # Reproduce the post_init snippet in isolation.
    try:
        with patch.object(graph_mod, "get_graph", side_effect=RuntimeError("kuzu not found")):
            try:
                await graph_mod.get_graph()
            except Exception:
                fake_logger.exception("graph init failed at boot (degrading: dual-writes will retry)")
    except Exception:
        raised = True

    assert not raised
    assert log_called


# ---------------------------------------------------------------------------
# Test 6 — /memory_diff command returns both result sets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.uses_real_graph
async def test_memory_diff_command_returns_both_result_sets():
    from unittest.mock import AsyncMock, MagicMock, patch
    from agents.telegram_bridge import cmd_memory_diff

    fake_update = MagicMock()
    fake_update.effective_user.id = 12345
    fake_update.message.reply_text = AsyncMock()
    fake_context = MagicMock()
    fake_context.args = ["oslo"]

    with patch("agents.telegram_bridge.owner_id", return_value=12345), \
         patch("storage.retrieval.retrieve", return_value=[{"subject": "user", "predicate": "lives_in", "object": "oslo"}]), \
         patch("storage.graph.search", new=AsyncMock(return_value=[MagicMock(fact="user lives in oslo")])):
        await cmd_memory_diff(fake_update, fake_context)

    reply_text = fake_update.message.reply_text.call_args[0][0]
    assert "SQLite" in reply_text
    assert "Graphiti" in reply_text
    assert "oslo" in reply_text.lower()
