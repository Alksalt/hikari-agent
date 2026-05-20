"""Phase 10: arxiv ML/DL paper search."""
from __future__ import annotations
import importlib
from pathlib import Path
import pytest
from storage import db
from agents import config

@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


@pytest.mark.asyncio
async def test_arxiv_search_mocked(monkeypatch):
    import arxiv
    from tools.arxiv_search import arxiv_search
    from types import SimpleNamespace
    fake_papers = [
        SimpleNamespace(
            title="Attention Is All You Need 2",
            summary="A follow-up exploring scaled attention.",
            authors=[SimpleNamespace(name="A"), SimpleNamespace(name="B")],
            entry_id="http://arxiv.org/abs/9999.99999v1",
            published="2026-05-18",
            categories=["cs.LG"],
        ),
    ]
    class FakeSearch:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def results(self): return iter(fake_papers)
    monkeypatch.setattr(arxiv, "Search", FakeSearch)
    out = await arxiv_search.handler({"query": "attention", "limit": 5})
    assert len(out["data"]["papers"]) == 1
    assert out["data"]["papers"][0]["title"].startswith("Attention")
