"""Annual review ceremony tests.

Mirrors isolation pattern from test_evening_diary.py — hikari.db isolated
per test via HIKARI_DB_PATH env + importlib.reload on storage.db.
"""
from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("DAY_RECEIPT_DB", str(tmp_path / "receipt.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")

    import storage.db as _db_mod
    importlib.reload(_db_mod)

    from tools.day_receipt import _db as _receipt_db
    _receipt_db._reset_schema_sentinel()

    yield tmp_path

    _receipt_db._reset_schema_sentinel()


# ---------- _is_review_window ----------

def test_is_review_window_dec_26_to_31():
    from agents.annual_review import _is_review_window

    assert _is_review_window(date(2025, 12, 25)) is False
    assert _is_review_window(date(2025, 12, 26)) is True
    assert _is_review_window(date(2025, 12, 31)) is True
    assert _is_review_window(date(2026, 1, 1)) is False


# ---------- _already_run_this_year ----------

def test_already_run_this_year_false_on_empty():
    from agents.annual_review import _already_run_this_year

    assert _already_run_this_year(2025) is False


def test_already_run_this_year_true_on_match():
    from agents.annual_review import _already_run_this_year
    from storage import db

    db.runtime_set("annual_review_last_year", "2025")
    assert _already_run_this_year(2025) is True


def test_already_run_this_year_false_on_different_year():
    from agents.annual_review import _already_run_this_year
    from storage import db

    db.runtime_set("annual_review_last_year", "2024")
    assert _already_run_this_year(2025) is False


# ---------- _gather_year_data ----------

def test_gather_year_data_returns_year_key():
    from agents.annual_review import _gather_year_data

    data = _gather_year_data(2025)
    assert data["year"] == 2025


def test_gather_episodes_picks_high_importance_first():
    from agents.annual_review import _gather_year_data
    from storage import db

    # Seed three episodes with different importance in 2025.
    db.insert_episode("2025-06-01", "low importance event", 1)
    db.insert_episode("2025-06-02", "high importance event", 9)
    db.insert_episode("2025-06-03", "medium importance event", 5)

    data = _gather_year_data(2025)
    episodes = data["top_episodes"]
    assert len(episodes) == 3
    # First result should be the highest importance.
    assert episodes[0]["importance"] == 9
    assert "high importance" in episodes[0]["summary"]


def test_gather_year_data_empty_when_no_episodes():
    from agents.annual_review import _gather_year_data

    data = _gather_year_data(2025)
    assert data["top_episodes"] == []
    assert data["receipts_by_category"] == {}
    assert data["decisions_resolved_count"] == 0
    assert data["drift_class_counts"] == {}


def test_gather_year_data_reads_receipts_from_day_receipt_db():
    """Receipts live in the separate day_receipt sqlite, not a `receipts`
    table in hikari.db. Aggregation must read entries by category for the
    target year and ignore entries outside it."""
    from datetime import date as _date

    from agents.annual_review import _gather_year_data
    from tools.day_receipt._db import add_entry

    add_entry("made", "shipped the thing", _date(2025, 3, 1))
    add_entry("made", "shipped another thing", _date(2025, 6, 1))
    add_entry("learned", "picked up a new pattern", _date(2025, 9, 1))
    # Outside the target year — must not be counted.
    add_entry("avoided", "skipped the gym", _date(2024, 12, 31))
    add_entry("avoided", "skipped again", _date(2026, 1, 1))

    data = _gather_year_data(2025)
    assert data["receipts_by_category"] == {"made": 2, "learned": 1}


def test_gather_year_data_reads_drift_by_sampled_at_column():
    """persona_drift_scores has no `ts` column — the real column is
    `sampled_at`. A query against `ts` silently returns nothing (wrapped in
    except: pass), so this must aggregate using `sampled_at`."""
    from agents.annual_review import _gather_year_data
    from storage import db

    with db._conn() as c:
        c.execute(
            "INSERT INTO persona_drift_scores "
            "(text_snippet, score, class_label, sampled_at) "
            "VALUES (?, ?, ?, ?)",
            ("in year", 0.9, "hikari", "2025-05-01T00:00:00+00:00"),
        )
        c.execute(
            "INSERT INTO persona_drift_scores "
            "(text_snippet, score, class_label, sampled_at) "
            "VALUES (?, ?, ?, ?)",
            ("in year drift", 0.2, "drifting", "2025-08-01T00:00:00+00:00"),
        )
        c.execute(
            "INSERT INTO persona_drift_scores "
            "(text_snippet, score, class_label, sampled_at) "
            "VALUES (?, ?, ?, ?)",
            ("out of year", 0.9, "hikari", "2024-01-01T00:00:00+00:00"),
        )

    data = _gather_year_data(2025)
    assert data["drift_class_counts"] == {"hikari": 1, "drifting": 1}


# ---------- _build_review_prompt ----------

def test_build_review_prompt_includes_episodes():
    from agents.annual_review import _build_review_prompt

    data = {
        "year": 2025,
        "top_episodes": [
            {"date": "2025-03-15", "summary": "shipped the big feature finally", "importance": 8},
            {"date": "2025-07-20", "summary": "got feedback that changed everything", "importance": 7},
        ],
        "receipts_by_category": {"made": 42, "learned": 18},
        "decisions_resolved_count": 5,
        "decisions_brier": {"brier": 0.12, "n": 5, "mean_predicted": 0.65, "mean_outcome": 0.60},
        "drift_class_counts": {"aligned": 48, "drift": 2},
    }

    prompt = _build_review_prompt(data)

    assert "2025" in prompt
    assert "shipped the big feature finally" in prompt
    assert "got feedback that changed everything" in prompt
    assert "made: 42" in prompt
    assert "learned: 18" in prompt
    assert "Decisions resolved: 5" in prompt
    assert "Brier rolling score: 0.120" in prompt
    assert "aligned: 48" in prompt
    assert "drift: 2" in prompt


def test_build_review_prompt_skips_brier_when_none():
    from agents.annual_review import _build_review_prompt

    data = {
        "year": 2025,
        "top_episodes": [],
        "receipts_by_category": {},
        "decisions_resolved_count": 0,
        "decisions_brier": None,
        "drift_class_counts": {},
    }

    prompt = _build_review_prompt(data)
    assert "Brier" not in prompt


# ---------- run_annual_review ----------

@pytest.mark.asyncio
async def test_run_annual_review_skips_when_disabled():
    """Config disabled → returns False without calling LLM or sending."""
    # Patch cfg inside agents.config so run_annual_review sees it disabled.
    with patch("agents.config.get", side_effect=lambda key, default=None: (
        False if key == "annual_review.enabled" else default
    )):
        from agents import annual_review
        result = await annual_review.run_annual_review(force=False)

    assert result is False


@pytest.mark.asyncio
async def test_run_annual_review_force_bypasses_window():
    """force=True skips the window + already-run checks and composes."""
    canned = "things worth more of\n- shipped things\n\nthings worth less of\n- overthought things\n\nstay honest."

    from agents import annual_review

    sender = AsyncMock(return_value=None)
    with (
        patch("agents.runtime.run_internal_text", new=AsyncMock(return_value=canned)),
    ):
        # Patch _is_review_window to False to confirm force bypasses it.
        with patch.object(annual_review, "_is_review_window", return_value=False):
            result = await annual_review.run_annual_review(send_text=sender, force=True)

    assert result is True
    sender.assert_called_once()


@pytest.mark.asyncio
async def test_run_annual_review_marks_idempotent():
    """After a successful run, runtime_state key is set to the review year."""
    from agents import annual_review
    from storage import db

    canned = "things worth more of\n- things\n\nthings worth less of\n- other things\n\nok."

    sender = AsyncMock(return_value=None)
    with (
        patch("agents.runtime.run_internal_text", new=AsyncMock(return_value=canned)),
    ):
        with patch.object(annual_review, "_is_review_window", return_value=True):
            with patch.object(annual_review, "_already_run_this_year", return_value=False):
                result = await annual_review.run_annual_review(send_text=sender, force=False)

    assert result is True
    stored = db.runtime_get("annual_review_last_year")
    assert stored is not None
    assert int(stored) > 0
