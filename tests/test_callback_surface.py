"""Callback surfacer: pick a rememberable moment that's topically adjacent
to the user's recent message. Once-per-session dedup via session_scratch."""
from __future__ import annotations

import importlib
import json
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_pick_callback_candidate_returns_none_when_no_episodes():
    from agents.callback_surface import pick_callback_candidate
    out = pick_callback_candidate("anything")
    assert out is None


def test_pick_callback_candidate_returns_none_for_empty_text():
    from agents.callback_surface import pick_callback_candidate
    assert pick_callback_candidate("") is None
    assert pick_callback_candidate("hi") is None  # too short, < 4 chars


def test_pick_callback_candidate_finds_topical_high_importance_episode():
    from agents import callback_surface
    from storage import db
    today = _date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    db.insert_episode(week_ago,
                      "burned the pasta. set off the smoke alarm.", 7)
    db.insert_episode(week_ago, "wrote tests for the migration.", 4)
    out = callback_surface.pick_callback_candidate("burned my hand on the pan")
    assert out is not None
    assert "pasta" in out["text"]
    assert "score" in out
    assert out["source"] == "episode"


def test_pick_callback_candidate_skips_low_importance_episodes():
    """Importance must clear the configured floor (default 6)."""
    from agents import callback_surface
    from storage import db
    today = _date.today()
    db.insert_episode((today - timedelta(days=3)).isoformat(),
                      "burned the pasta loudly.", 3)  # low importance
    out = callback_surface.pick_callback_candidate("burned my pasta")
    assert out is None


def test_pick_callback_candidate_skips_too_old_episodes():
    """Window enforced by config (default 90 days)."""
    from agents import callback_surface
    from storage import db
    long_ago = (_date.today() - timedelta(days=200)).isoformat()
    db.insert_episode(long_ago, "burned the pasta in another era.", 9)
    out = callback_surface.pick_callback_candidate("burned my pasta")
    assert out is None


def test_pick_callback_candidate_respects_session_dedup():
    """Same session, same candidate → once committed, second call returns None.

    The dedup write is deferred to mark_callback_surfaced() (called by
    inject_memory only when the block survives the budget), so the commit is
    explicit here — pick alone no longer suppresses.
    """
    from agents import callback_surface
    from storage import db
    today = _date.today()
    db.insert_episode((today - timedelta(days=3)).isoformat(),
                      "lost the kyiv keys.", 8)
    db.set_session_id("test-session-1")

    first = callback_surface.pick_callback_candidate("where are my keys")
    assert first is not None
    # Commit the surfaced candidate (deferred from pick to inject_memory).
    callback_surface.mark_callback_surfaced(first)
    # Same session, related topic → must dedup the already-surfaced candidate.
    second = callback_surface.pick_callback_candidate("found the keys")
    assert second is None


def test_pick_callback_candidate_not_deduped_until_committed():
    """A picked-but-not-committed candidate (e.g. budget-dropped) must stay
    eligible for a later turn — the whole point of deferring the dedup write."""
    from agents import callback_surface
    from storage import db
    today = _date.today()
    db.insert_episode((today - timedelta(days=3)).isoformat(),
                      "lost the kyiv keys.", 8)
    db.set_session_id("test-session-deferred")

    first = callback_surface.pick_callback_candidate("where are my keys")
    assert first is not None
    # No mark_callback_surfaced() → the candidate was never injected, so a
    # subsequent pick must NOT be suppressed.
    second = callback_surface.pick_callback_candidate("found the keys")
    assert second is not None


def test_pick_callback_candidate_respects_min_score():
    """If overlap score is below threshold, no candidate."""
    from agents import callback_surface
    from storage import db
    today = _date.today()
    db.insert_episode((today - timedelta(days=3)).isoformat(),
                      "burned the pasta last week.", 7)
    # No token overlap at all.
    out = callback_surface.pick_callback_candidate(
        "completely unrelated topic about astronomy"
    )
    assert out is None


@pytest.mark.asyncio
async def test_inject_memory_includes_callback_block_when_candidate_exists():
    from agents.hooks import inject_memory
    from storage import db
    today_ago = (_date.today() - timedelta(days=5)).isoformat()
    db.insert_episode(today_ago, "burned the pasta again.", 7)

    out = await inject_memory(
        {"prompt": "burned myself making lunch"}, None, None,
    )
    blob = json.dumps(out)
    assert "callback candidate" in blob
    assert "pasta" in blob
