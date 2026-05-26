"""Seeded cycle-state composition test.

Scenario: cycle_start_date = 2026-05-01, test instant = 2026-05-21 23:00 local.

Hand-computed expected values:
  day_of_cycle = ((2026-05-21 - 2026-05-01).days % 28) + 1 = (20 % 28) + 1 = 21
  cycle_phase   = inward (days 17-24, warmth mult 1.0)
  season        = spring (May, mult 1.1)
  weekly_label  = friction (Thursday = weekday 3)
  daily_phase   = night-mode (23:00)
  warmth_multiplier = round(1.0 * 1.1, 3) = 1.1
  composite_label = "inward / spring / friction / night-mode"
  mood_today    = "tired"  (inward + friction → see _MOOD_TABLE)

Uses monkeypatching of datetime.now() and date.today() — no freezegun dependency
required (falls back cleanly to the same monkeypatch pattern used by test_cycle_state.py).
"""
from __future__ import annotations

import importlib
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield
    db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# Shared fixtures for the 2026-05-21 23:00 scenario
# ---------------------------------------------------------------------------

_CYCLE_START = "2026-05-01"
_TEST_DATE = date(2026, 5, 21)      # Thursday, May → spring, day-21 of cycle


def _seed_and_compute():
    """Seed cycle_start_date and run compute_cycle_state frozen at 23:00 on 2026-05-21."""
    import datetime as _dt
    from agents.reflection import compute_cycle_state

    db.upsert_core_block("cycle_start_date", _CYCLE_START)

    frozen = _dt.datetime(2026, 5, 21, 23, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = _TEST_DATE
        mock_date.fromisoformat.side_effect = date.fromisoformat

        return compute_cycle_state()


# ---------------------------------------------------------------------------
# 1. cycle_phase is inward (day 21 falls in 17-24 window)
# ---------------------------------------------------------------------------

def test_seeded_cycle_phase_is_inward():
    result = _seed_and_compute()
    assert result["cycle_phase"] == "inward"


# ---------------------------------------------------------------------------
# 2. season is spring (May)
# ---------------------------------------------------------------------------

def test_seeded_season_is_spring():
    result = _seed_and_compute()
    assert result["season"] == "spring"


# ---------------------------------------------------------------------------
# 3. weekly_label is friction (Thursday = weekday 3)
# ---------------------------------------------------------------------------

def test_seeded_weekly_label_is_friction():
    result = _seed_and_compute()
    assert result["weekly_label"] == "friction"


# ---------------------------------------------------------------------------
# 4. daily_phase is night-mode (23:00 → night-mode per _circadian_phase)
# ---------------------------------------------------------------------------

def test_seeded_daily_phase_is_night_mode():
    result = _seed_and_compute()
    assert result["daily_phase"] == "night-mode"


# ---------------------------------------------------------------------------
# 5. composite_label matches the full hand-computed string
# ---------------------------------------------------------------------------

def test_seeded_composite_label():
    result = _seed_and_compute()
    assert result["composite_label"] == "inward / spring / friction / night-mode"


# ---------------------------------------------------------------------------
# 6. warmth_multiplier = round(inward_mult * spring_mult, 3) = round(1.0 * 1.1, 3) = 1.1
# ---------------------------------------------------------------------------

def test_seeded_warmth_multiplier():
    result = _seed_and_compute()
    assert abs(result["warmth_multiplier"] - 1.1) < 0.001


# ---------------------------------------------------------------------------
# 7. mood_today = "tired" (inward + friction maps to "tired" in _MOOD_TABLE)
# ---------------------------------------------------------------------------

def test_seeded_mood_today_is_tired():
    _seed_and_compute()
    mood = db.get_core_block("mood_today")
    assert mood == "tired"


# ---------------------------------------------------------------------------
# 8. cycle_state core_block is written with all expected fields
# ---------------------------------------------------------------------------

def test_seeded_cycle_state_core_block_written():
    _seed_and_compute()
    raw = db.get_core_block("cycle_state")
    assert raw is not None
    state = json.loads(raw)
    assert state["cycle_phase"] == "inward"
    assert state["season"] == "spring"
    assert state["weekly_label"] == "friction"
    assert state["daily_phase"] == "night-mode"
    assert state["composite_label"] == "inward / spring / friction / night-mode"
    assert abs(state["warmth_multiplier"] - 1.1) < 0.001


# ---------------------------------------------------------------------------
# 9. Different cycle_start within the same 28-day window gives correct day
#    (regression: ensure modulo arithmetic doesn't off-by-one on day boundaries)
# ---------------------------------------------------------------------------

def test_day_boundary_day_17_is_inward_start():
    """Day 17 = first day of inward phase.  cycle_start = 2026-05-05 places
    2026-05-21 on day 17 of the cycle."""
    import datetime as _dt
    from agents.reflection import compute_cycle_state

    # cycle_start = 2026-05-05 → day = ((21-5).days % 28) + 1 = 17
    db.upsert_core_block("cycle_start_date", "2026-05-05")

    frozen = _dt.datetime(2026, 5, 21, 15, 0, 0)  # peak hour, not night

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = _TEST_DATE
        mock_date.fromisoformat.side_effect = date.fromisoformat

        result = compute_cycle_state()

    assert result["cycle_phase"] == "inward"


def test_day_boundary_day_24_is_still_inward():
    """Day 24 = last day of inward phase.  cycle_start = 2026-04-28 places
    2026-05-21 on day 24 of the cycle."""
    import datetime as _dt
    from agents.reflection import compute_cycle_state

    # cycle_start = 2026-04-28 → day = ((23).days % 28) + 1 = 24
    db.upsert_core_block("cycle_start_date", "2026-04-28")

    frozen = _dt.datetime(2026, 5, 21, 15, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = _TEST_DATE
        mock_date.fromisoformat.side_effect = date.fromisoformat

        result = compute_cycle_state()

    assert result["cycle_phase"] == "inward"


def test_day_boundary_day_25_is_low_tolerance():
    """Day 25 = first day of low-tolerance phase.  cycle_start = 2026-04-27 places
    2026-05-21 on day 25 of the cycle."""
    import datetime as _dt
    from agents.reflection import compute_cycle_state

    # cycle_start = 2026-04-27 → day = ((24).days % 28) + 1 = 25
    db.upsert_core_block("cycle_start_date", "2026-04-27")

    frozen = _dt.datetime(2026, 5, 21, 15, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = _TEST_DATE
        mock_date.fromisoformat.side_effect = date.fromisoformat

        result = compute_cycle_state()

    assert result["cycle_phase"] == "low-tolerance"
    # low-tolerance warmth mult = 0.5 * spring 1.1 = 0.55
    assert abs(result["warmth_multiplier"] - 0.55) < 0.001
