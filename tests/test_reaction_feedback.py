"""Tests for reaction → proactive_source_scores EMA feedback loop.

Sprint B Wave 3 — tests-engagement-policy agent.

Coverage:
  1. Thumbs-down on a source lowers its EMA in proactive_source_scores.
  2. Thumbs-up on a source raises its EMA.
  3. Multiple consecutive reactions compound in the right direction.
  4. Score for a different source is unaffected by a reaction on another source.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture — isolated DB per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _get_score(source: str) -> float | None:
    """Return the current EMA for source, or None if no row exists."""
    from storage import db
    rows = db.proactive_source_scores_all()
    for row in rows:
        if row["source"] == source:
            return float(row["ema"])
    return None


def _seed_score(source: str, ema: float) -> None:
    """Seed a known EMA for a source."""
    from storage import db
    db.proactive_source_score_upsert(source, ema=ema, thumbs_up=0, thumbs_down=0)


# ---------------------------------------------------------------------------
# 1. Thumbs-down lowers the EMA
# ---------------------------------------------------------------------------

class TestThumbsDownLowersScore:
    def test_thumbs_down_moves_ema_toward_zero(self):
        """on_reaction('down') must produce a lower EMA than the starting value."""
        from agents.engagement.sender import on_reaction

        _seed_score("calendar_event_prep", 0.5)
        on_reaction("calendar_event_prep", "down")

        after = _get_score("calendar_event_prep")
        assert after is not None
        assert after < 0.5, f"EMA should decrease after thumbs-down, got {after}"

    def test_thumbs_down_increments_counter(self):
        """thumbs_down counter increments on a down reaction."""
        from agents.engagement.sender import on_reaction
        from storage import db

        on_reaction("calendar_event_prep", "down")

        rows = db.proactive_source_scores_all()
        row = next((r for r in rows if r["source"] == "calendar_event_prep"), None)
        assert row is not None
        assert row["n_thumbs_down"] >= 1

    def test_repeated_thumbs_down_compounds(self):
        """Three consecutive thumbs-down reactions keep moving EMA lower."""
        from agents.engagement.sender import on_reaction

        _seed_score("gmail_unread_threshold", 0.8)
        on_reaction("gmail_unread_threshold", "down")
        after_1 = _get_score("gmail_unread_threshold")
        on_reaction("gmail_unread_threshold", "down")
        after_2 = _get_score("gmail_unread_threshold")
        on_reaction("gmail_unread_threshold", "down")
        after_3 = _get_score("gmail_unread_threshold")

        assert after_1 < 0.8, "First down should lower score"
        assert after_2 < after_1, "Second down should lower score further"
        assert after_3 < after_2, "Third down should lower score even further"

    def test_thumbs_down_ema_lower_than_initial_default(self):
        """on_reaction('down') when no row exists starts from default 0.5 and goes lower."""
        from agents.engagement.sender import on_reaction

        # No seed — default starts at 0.5
        on_reaction("notion_recent_edit", "down")
        after = _get_score("notion_recent_edit")
        assert after is not None
        assert after < 0.5, f"Expected below 0.5 after thumbs-down, got {after}"


# ---------------------------------------------------------------------------
# 2. Thumbs-up raises the EMA
# ---------------------------------------------------------------------------

class TestThumbsUpRaisesScore:
    def test_thumbs_up_moves_ema_toward_one(self):
        """on_reaction('up') must produce a higher EMA than the starting value."""
        from agents.engagement.sender import on_reaction

        _seed_score("reminder_fire", 0.5)
        on_reaction("reminder_fire", "up")

        after = _get_score("reminder_fire")
        assert after is not None
        assert after > 0.5, f"EMA should increase after thumbs-up, got {after}"

    def test_thumbs_up_increments_counter(self):
        """thumbs_up counter increments on an up reaction."""
        from agents.engagement.sender import on_reaction
        from storage import db

        on_reaction("wiki_new_file", "up")

        rows = db.proactive_source_scores_all()
        row = next((r for r in rows if r["source"] == "wiki_new_file"), None)
        assert row is not None
        assert row["n_thumbs_up"] >= 1

    def test_repeated_thumbs_up_compounds(self):
        """Three consecutive thumbs-up reactions keep increasing EMA."""
        from agents.engagement.sender import on_reaction

        _seed_score("callback_episode", 0.2)
        on_reaction("callback_episode", "up")
        after_1 = _get_score("callback_episode")
        on_reaction("callback_episode", "up")
        after_2 = _get_score("callback_episode")
        on_reaction("callback_episode", "up")
        after_3 = _get_score("callback_episode")

        assert after_1 > 0.2, "First up should raise score"
        assert after_2 > after_1, "Second up should raise further"
        assert after_3 > after_2, "Third up should raise further still"

    def test_thumbs_up_ema_higher_than_initial_default(self):
        """on_reaction('up') when no row exists starts from default 0.5 and goes higher."""
        from agents.engagement.sender import on_reaction

        on_reaction("weather_alert", "up")
        after = _get_score("weather_alert")
        assert after is not None
        assert after > 0.5, f"Expected above 0.5 after thumbs-up, got {after}"


# ---------------------------------------------------------------------------
# 3. EMA formula — alpha = 0.3, target=1.0 for up, 0.0 for down
# ---------------------------------------------------------------------------

class TestEMAFormula:
    def test_ema_formula_down_exact(self):
        """EMA after one thumbs-down from 0.5: 0.5 + 0.3*(0.0 - 0.5) = 0.35."""
        from agents.engagement.sender import on_reaction, _EMA_ALPHA

        _seed_score("test_source_formula", 0.5)
        on_reaction("test_source_formula", "down")

        expected = 0.5 + _EMA_ALPHA * (0.0 - 0.5)
        after = _get_score("test_source_formula")
        assert after is not None
        assert abs(after - expected) < 1e-6, f"Expected {expected}, got {after}"

    def test_ema_formula_up_exact(self):
        """EMA after one thumbs-up from 0.5: 0.5 + 0.3*(1.0 - 0.5) = 0.65."""
        from agents.engagement.sender import on_reaction, _EMA_ALPHA

        _seed_score("test_source_formula_up", 0.5)
        on_reaction("test_source_formula_up", "up")

        expected = 0.5 + _EMA_ALPHA * (1.0 - 0.5)
        after = _get_score("test_source_formula_up")
        assert after is not None
        assert abs(after - expected) < 1e-6, f"Expected {expected}, got {after}"


# ---------------------------------------------------------------------------
# 4. Reaction on one source does not affect another source
# ---------------------------------------------------------------------------

class TestReactionIsolation:
    def test_down_on_source_a_does_not_change_source_b(self):
        """A thumbs-down on source A leaves source B's score unchanged."""
        from agents.engagement.sender import on_reaction

        _seed_score("wiki_new_file", 0.7)
        _seed_score("drive_starred_new", 0.6)

        on_reaction("wiki_new_file", "down")

        wiki_score = _get_score("wiki_new_file")
        drive_score = _get_score("drive_starred_new")

        assert wiki_score is not None and wiki_score < 0.7, "wiki should have decreased"
        assert drive_score is not None and abs(drive_score - 0.6) < 1e-6, (
            f"drive score should be unchanged at 0.6, got {drive_score}"
        )

    def test_up_on_source_a_does_not_change_source_b(self):
        """A thumbs-up on source A leaves source B's score unchanged."""
        from agents.engagement.sender import on_reaction

        _seed_score("reminder_fire", 0.4)
        _seed_score("reengage_silence", 0.3)

        on_reaction("reminder_fire", "up")

        reminder_score = _get_score("reminder_fire")
        reengage_score = _get_score("reengage_silence")

        assert reminder_score is not None and reminder_score > 0.4, "reminder should have increased"
        assert reengage_score is not None and abs(reengage_score - 0.3) < 1e-6, (
            f"reengage score should be unchanged at 0.3, got {reengage_score}"
        )
