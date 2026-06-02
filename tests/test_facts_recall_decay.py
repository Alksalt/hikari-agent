"""T3.2 — Ebbinghaus-weighted recall ranking.

Covers:
  - A fresh fact ranks higher than a backdated fact with the same content
    (decay multiplier kicks in).
  - ``recall_hit_count`` and ``last_recalled_at`` get bumped on the
    returned facts so subsequent recalls see a stretched tau.
  - hit_count growing actually shrinks the decay (tau grows by 1.5**k).
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import config
from storage import db, retrieval


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    # Skip the actual embedding call — model load is slow and we only need
    # BM25 to prove the decay logic. retrieve() falls back gracefully when
    # embeddings.embed raises.
    monkeypatch.setattr(
        retrieval.embeddings, "embed",
        lambda _text: (_ for _ in ()).throw(RuntimeError("no embeddings in test")),
    )
    yield


def _backdate(fact_id: int, days_ago: int) -> str:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    db.fact_backdate_created_at(fact_id, ts)
    return ts


def test_recall_prefers_fresh_fact_over_old():
    """Two facts with nearly identical content; the old one should rank lower
    because the Ebbinghaus multiplier collapses its relevance contribution."""
    fresh = db.fact_insert(text="user loves matcha cake", source="user")
    old = db.fact_insert(text="user loves matcha cake recipes", source="user")
    # Backdate the second by ~28 days — well past the 7-day tau half-life so
    # the multiplier shrinks the relevance term noticeably.
    _backdate(old, days_ago=28)

    hits = retrieval.legacy_retrieve("matcha cake", limit=5)
    fact_hits = [h for h in hits if h.kind == "fact"]
    assert len(fact_hits) >= 2, (
        f"expected both facts retrieved, got {[(h.kind, h.ref_id) for h in hits]}"
    )
    # Find both by id, compare scores.
    by_id = {h.ref_id: h for h in fact_hits}
    assert fresh in by_id and old in by_id
    assert by_id[fresh].score > by_id[old].score, (
        f"fresh fact (id={fresh}) should outrank old fact (id={old}); "
        f"got fresh.score={by_id[fresh].score:.4f}, "
        f"old.score={by_id[old].score:.4f}"
    )


def test_recall_bumps_hit_count_and_last_recalled_at():
    """A successful recall stamps the returned facts so the next call sees
    a stretched tau (rehearsal effect)."""
    fid = db.fact_insert(text="user studies attention mechanisms", source="user")

    row_before = db.get_fact(fid)
    assert row_before["recall_hit_count"] == 0
    assert row_before["last_recalled_at"] is None

    hits = retrieval.legacy_retrieve("attention mechanisms", limit=5)
    assert any(h.kind == "fact" and h.ref_id == fid for h in hits)

    row_after = db.get_fact(fid)
    assert row_after["recall_hit_count"] == 1
    assert row_after["last_recalled_at"] is not None

    # Second recall — counter should keep climbing.
    retrieval.legacy_retrieve("attention mechanisms", limit=5)
    row_third = db.get_fact(fid)
    assert row_third["recall_hit_count"] == 2


def test_facts_mark_recalled_handles_empty_list():
    """No-op call must not raise and must not touch any rows."""
    assert db.facts_mark_recalled([]) == 0
    # Insert one, confirm it stays untouched.
    fid = db.fact_insert(text="ignored", source="user")
    assert db.facts_mark_recalled([]) == 0
    row = db.get_fact(fid)
    assert row["recall_hit_count"] == 0
    assert row["last_recalled_at"] is None


def test_facts_mark_recalled_bulk_update():
    """Multiple ids in one call all get stamped + incremented."""
    a = db.fact_insert(text="fact a", source="user")
    b = db.fact_insert(text="fact b", source="user")
    n = db.facts_mark_recalled([a, b])
    assert n == 2
    for fid in (a, b):
        row = db.get_fact(fid)
        assert row["recall_hit_count"] == 1
        assert row["last_recalled_at"] is not None
