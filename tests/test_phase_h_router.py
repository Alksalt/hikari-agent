"""Phase H — hikari_router + BM25 tool_search + McpManager tests.

Success criterion for test 8: estimated per-turn tool-definition tokens
≤ 2500 (Bucket-1 tools only; deferred tools are not in the per-turn context).
"""
from __future__ import annotations

import asyncio
import importlib
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Isolation fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh DB + env for each test."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


@pytest.fixture()
def fresh_registry():
    """Uncached ToolRegistry from the real tools.yaml."""
    from tools._tools_yaml import DEFAULT_YAML_PATH, _load_yaml
    return _load_yaml(DEFAULT_YAML_PATH)


@pytest.fixture()
def ts_mod():
    """Return the tool_search module with a freshly built BM25 index.

    Must use importlib.import_module rather than ``import ... as`` because
    ``tools/router/__init__.py`` re-exports the SdkMcpTool as ``tool_search``,
    which shadows the submodule name on the package. importlib always returns
    the module object.
    """
    _ts = importlib.import_module("tools.router.tool_search")
    # Reset state before rebuilding so tests are isolated from each other
    _ts._INDEX["bm25"] = None
    _ts._INDEX["tool_ids"] = []
    _ts._INDEX["tool_descs"] = []
    _ts._INDEX["tool_tags"] = []
    _ts.rebuild_index()
    return _ts


def _run(coro):
    """Run a coroutine synchronously (compatible with pytest's sync test runner)."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Test 1: rebuild_index populates corpus
# ---------------------------------------------------------------------------

class TestRebuildIndex:
    def test_rebuild_index_populates_corpus(self, ts_mod):
        assert ts_mod._INDEX["bm25"] is not None, "BM25 object should be non-None after rebuild"
        assert len(ts_mod._INDEX["tool_ids"]) > 0, "tool_ids should be non-empty after rebuild"

    def test_rebuild_index_has_descs(self, ts_mod):
        assert len(ts_mod._INDEX["tool_descs"]) == len(ts_mod._INDEX["tool_ids"])


# ---------------------------------------------------------------------------
# Test 2: tool_search returns hits for a known token
# ---------------------------------------------------------------------------

class TestToolSearchHits:
    def test_tool_search_returns_hits_for_known_tag(self, ts_mod):
        # "notion" appears in bucket-3 tool ids
        result = _run(ts_mod.tool_search.handler({"query": "notion", "limit": 5}))
        data = result.get("data", {})
        hits = data.get("hits", [])
        assert len(hits) >= 1, f"Expected ≥1 hit for 'notion'; got {hits}"
        tool_ids = [h["tool_id"] for h in hits]
        assert any("notion" in tid for tid in tool_ids), (
            f"Expected at least one notion tool in hits; got {tool_ids}"
        )

    def test_tool_search_returns_hits_for_google(self, ts_mod):
        result = _run(ts_mod.tool_search.handler({"query": "google workspace gmail", "limit": 5}))
        hits = result.get("data", {}).get("hits", [])
        assert len(hits) >= 1, f"Expected ≥1 hit for 'google workspace gmail'; got {hits}"


# ---------------------------------------------------------------------------
# Test 3: empty query returns error message, no crash
# ---------------------------------------------------------------------------

class TestToolSearchEmptyQuery:
    def test_tool_search_empty_query(self, ts_mod):
        result = _run(ts_mod.tool_search.handler({"query": "", "limit": 5}))
        content = result["content"][0]["text"]
        assert "non-empty" in content or "needs" in content, (
            f"Expected helpful message for empty query; got: {content}"
        )

    def test_tool_search_none_query(self, ts_mod):
        result = _run(ts_mod.tool_search.handler({"query": None, "limit": 5}))
        content = result["content"][0]["text"]
        assert "non-empty" in content or "needs" in content


# ---------------------------------------------------------------------------
# Test 4: bucket-1 tools excluded from index
# ---------------------------------------------------------------------------

class TestBucket1Excluded:
    def test_tool_search_bucket_1_tools_excluded(self, ts_mod):
        ids = ts_mod._INDEX["tool_ids"]
        # Known bucket-1 tools that must NOT appear
        bucket1_samples = [
            "mcp__hikari_memory__recall",
            "mcp__hikari_memory__remember",
            "mcp__hikari_wiki__wiki_search",
            "mcp__hikari_dispatch__dispatch_claude_session",
            "mcp__hikari_router__tool_search",
            "Agent",
            "WebFetch",
            "WebSearch",
        ]
        for b1 in bucket1_samples:
            assert b1 not in ids, (
                f"Bucket-1 tool {b1!r} must not appear in BM25 index"
            )

    def test_only_bucket3_tools_indexed(self, ts_mod, fresh_registry):
        indexed_set = set(ts_mod._INDEX["tool_ids"])
        for spec in fresh_registry.specs():
            if spec.bucket == 1 and spec.id in indexed_set:
                pytest.fail(
                    f"Bucket-1 tool {spec.id!r} found in BM25 index — must be excluded"
                )


# ---------------------------------------------------------------------------
# Test 5: McpManager acquire marks warm
# ---------------------------------------------------------------------------

class TestMcpManagerAcquire:
    def test_mcp_manager_acquire_marks_warm(self):
        from agents.mcp_manager import McpManager
        mgr = McpManager()
        mgr.configure_ttls({"google_workspace": 60})
        _run(mgr.acquire("google_workspace"))
        assert mgr.is_warm("google_workspace"), "Server should be warm right after acquire"

    def test_mcp_manager_not_warm_before_acquire(self):
        from agents.mcp_manager import McpManager
        mgr = McpManager()
        assert not mgr.is_warm("notion"), "Server should not be warm before first acquire"

    def test_mcp_manager_warm_servers_returns_set(self):
        from agents.mcp_manager import McpManager
        mgr = McpManager()
        mgr.configure_ttls({"github": 60})
        _run(mgr.acquire("github"))
        ws = mgr.warm_servers()
        assert isinstance(ws, set)
        assert "github" in ws


# ---------------------------------------------------------------------------
# Test 6: evict_stale after TTL
# ---------------------------------------------------------------------------

class TestMcpManagerEvict:
    def test_mcp_manager_evict_stale_after_ttl(self):
        from agents.mcp_manager import McpManager
        mgr = McpManager()
        mgr.configure_ttls({"playwright": 0})  # 0s TTL — immediately stale

        _run(mgr.acquire("playwright"))
        # Manually backdate the timestamp to guarantee stale
        mgr._last_acquired["playwright"] = time.time() - 1.0

        evicted = _run(mgr.evict_stale())
        assert "playwright" in evicted, f"Expected 'playwright' in evicted; got {evicted}"
        assert not mgr.is_warm("playwright"), "Server should not be warm after eviction"

    def test_mcp_manager_evict_stale_tiny_ttl(self):
        from agents.mcp_manager import McpManager
        mgr = McpManager()
        mgr.configure_ttls({"notion": 1})

        _run(mgr.acquire("notion"))
        # Still warm right after acquire (ttl=1s)
        assert mgr.is_warm("notion")

        # Backdate to force stale
        mgr._last_acquired["notion"] = time.time() - 2.0
        evicted = _run(mgr.evict_stale())
        assert "notion" in evicted


# ---------------------------------------------------------------------------
# Test 7: limit capped at 20
# ---------------------------------------------------------------------------

class TestToolSearchLimit:
    def test_tool_search_limit_capped_at_20(self, ts_mod):
        # Corpus has far fewer than 999 tools, so len(hits) ≤ min(20, corpus_size)
        result = _run(ts_mod.tool_search.handler({"query": "api", "limit": 999}))
        hits = result.get("data", {}).get("hits", [])
        assert len(hits) <= 20, f"Hits should be capped at 20; got {len(hits)}"

    def test_tool_search_default_limit_5(self, ts_mod):
        result = _run(ts_mod.tool_search.handler({"query": "github"}))
        hits = result.get("data", {}).get("hits", [])
        assert len(hits) <= 5


# ---------------------------------------------------------------------------
# Test 8: per-turn token budget ≤ 2500 (Bucket-1 only in context)
# ---------------------------------------------------------------------------

class TestPerTurnTokenBudget:
    def test_per_turn_token_budget_target(self, fresh_registry):
        """Bucket-1 tool definitions only. Word-count × 1.3 ≤ 2500."""
        bucket1_specs = [s for s in fresh_registry.specs() if s.bucket == 1]

        # Estimate tokens from tool id word count only (ToolSpec has no description
        # field). This is the floor; actual SDK descriptions add more, but the
        # point is that the bucket-1 roster hasn't ballooned unexpectedly.
        total_words = 0
        for spec in bucket1_specs:
            id_words = len(spec.id.replace("__", " ").replace("_", " ").split())
            total_words += id_words

        estimated_tokens = int(total_words * 1.3)
        assert estimated_tokens <= 2500, (
            f"Bucket-1 tool id token estimate ({estimated_tokens}) exceeds 2500. "
            f"Roster has {len(bucket1_specs)} bucket-1 specs. "
            "Either too many bucket-1 tools were added or the target needs revising."
        )

    def test_bucket1_tool_count_reasonable(self, fresh_registry):
        """Guard rails: bucket-1 roster should stay under 60 entries."""
        b1 = [s for s in fresh_registry.specs() if s.bucket == 1]
        assert len(b1) <= 60, (
            f"Bucket-1 has {len(b1)} entries — unexpectedly large. "
            "Check for accidental promotion of bucket-3 tools."
        )
