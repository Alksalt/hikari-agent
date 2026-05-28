"""Sprint 5A — recall() surfaces provenance fields on fact-kind hits.

Two test cases:
  1. legacy-path fact hit with attribution='user_stated' surfaces all four
     provenance fields (attribution, source_message_id, source_span_hash,
     recorded_at).
  2. A fact with NULL source_message_id yields attribution populated but
     source_message_id == None.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh per-test DB — mirrors test_facts_attribution.py."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _make_hit(kind: str, ref_id, text: str, score: float = 0.8) -> MagicMock:
    h = MagicMock()
    h.kind = kind
    h.ref_id = ref_id
    h.text = text
    h.score = score
    h.recency = 0.9
    h.importance = 5
    h.relevance = 0.8
    return h


async def _call_recall_legacy(query: str, monkeypatch, hits) -> dict:
    """Drive _legacy_fallback directly by patching at the module level."""
    # Block the graph so recall falls through to legacy.
    from storage import graph as _graph_mod

    async def _no_graph(*_a, **_kw):
        raise RuntimeError("graph blocked in test")

    monkeypatch.setattr(_graph_mod, "search", _no_graph)

    # Inject the synthetic hits into retrieval.
    from storage import retrieval as _retrieval_mod

    def _fake_legacy(_q, _lim):
        return hits

    monkeypatch.setattr(_retrieval_mod, "legacy_retrieve", _fake_legacy)

    # Also patch in the recall module's own storage.db reference so
    # fact_provenance calls hit the test DB.
    #
    # IMPORTANT: `import tools.memory.recall as x` resolves to the SdkMcpTool
    # attribute on the package namespace (not the file module) once __init__.py
    # has run. Use sys.modules directly to reach the real file module.
    import importlib
    import sys

    importlib.import_module("tools.memory.recall")  # ensure registered
    _recall_module = sys.modules["tools.memory.recall"]
    monkeypatch.setattr(_recall_module, "_db", db)

    # recall is an SdkMcpTool — call the underlying async handler directly.
    result = await _recall_module.recall.handler({"query": query, "limit": 8})
    return result


@pytest.mark.asyncio
async def test_legacy_fact_hit_surfaces_provenance(monkeypatch):
    """legacy-path hit on a fact with attribution='user_stated' surfaces the
    four provenance fields in the hit dict."""
    mid = db.append_message("user", "i love cold rice")
    fid = db.insert_fact(
        "user", "loves", "cold rice",
        source_message_id=mid,
        source_span_hash=db.span_hash("user loves cold rice"),
        recorded_at=999,
        attribution="user_stated",
        source="user",
    )

    hit = _make_hit("fact", fid, "user loves cold rice", score=0.9)
    result = await _call_recall_legacy("cold rice", monkeypatch, [hit])

    hits_out = result.get("data", {}).get("hits", [])
    assert hits_out, "expected at least one hit"
    h = hits_out[0]
    assert h["attribution"] == "user_stated", f"expected 'user_stated', got {h['attribution']!r}"
    assert h["source_message_id"] == mid
    assert h["source_span_hash"] is not None
    assert h["recorded_at"] == 999


@pytest.mark.asyncio
async def test_legacy_fact_hit_null_source_message_id(monkeypatch):
    """A fact with NULL source_message_id yields attribution populated but
    source_message_id == None."""
    fid = db.insert_fact(
        "user", "uses", "Python",
        attribution="hikari_inferred",
        source_span_hash=db.span_hash("user uses Python"),
    )

    hit = _make_hit("fact", fid, "user uses Python", score=0.85)
    result = await _call_recall_legacy("Python", monkeypatch, [hit])

    hits_out = result.get("data", {}).get("hits", [])
    assert hits_out, "expected at least one hit"
    h = hits_out[0]
    assert h["attribution"] == "hikari_inferred"
    assert h["source_message_id"] is None
