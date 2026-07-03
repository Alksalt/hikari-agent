"""Cycle-state composition tests.

Covers:
  1. compute_cycle_state with fully controlled inputs — day 21 of 28-day
     cycle + Sunday + winter + 23:00 local → expected composite_label and
     warmth_multiplier.
  2. mood_today derivation for peak-social + lift and emergence + lift → weirdly good.
  3. time_texture late-night transitions: hours 22 → 01 → 02 map correctly
     through the scheduler's _hour_to_time_texture helper.

All tests are deterministic — no real LLM, no real network, no real DB writes
(except via the isolated_db fixture).
"""

from __future__ import annotations

import importlib
import json
from datetime import date, timedelta
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


# ---------------------------------------------------------------------------
# Helper: build a cycle_start_date that places today at a given day-of-cycle.
# ---------------------------------------------------------------------------

def _cycle_start_for_day(day_of_cycle: int, today: date) -> date:
    """Return the date that makes ``today`` fall on ``day_of_cycle`` (1-indexed)."""
    # day_of_cycle = ((today - cycle_start).days % 28) + 1
    # → (today - cycle_start).days = day_of_cycle - 1  (modulo 28 exact)
    return today - timedelta(days=day_of_cycle - 1)


# ---------------------------------------------------------------------------
# 1. Day 21 of cycle + Sunday + winter + 23:00 → expected label + warmth
# ---------------------------------------------------------------------------

def test_day21_sunday_winter_night_composite_label(monkeypatch):
    """Day 21 → inward (17-24), Sunday → low, winter → 0.9, 23:00 → night-mode.

    Expected composite_label: 'inward / winter / low / night-mode'
    Expected warmth_multiplier: 1.0 * 0.9 = 0.9
    """
    from agents.reflection import compute_cycle_state

    # Pin today to a Sunday in January (winter).
    # 2026-01-04 is a Sunday.
    today = date(2026, 1, 4)
    cycle_start = _cycle_start_for_day(21, today)
    db.upsert_core_block("cycle_start_date", cycle_start.isoformat())

    # Freeze datetime.now() to 23:00 on that Sunday.
    import datetime as _dt
    frozen = _dt.datetime(2026, 1, 4, 23, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = today
        mock_date.fromisoformat.side_effect = date.fromisoformat

        result = compute_cycle_state()

    assert result["cycle_phase"] == "inward"
    assert result["season"] == "winter"
    assert result["weekly_label"] == "low"
    assert result["daily_phase"] == "night-mode"
    assert result["composite_label"] == "inward / winter / low / night-mode"
    assert abs(result["warmth_multiplier"] - 0.9) < 0.001


def test_day21_sunday_winter_night_mood_is_tired(monkeypatch):
    """(inward, low) → mood_today = 'tired'."""
    from agents.reflection import compute_cycle_state

    today = date(2026, 1, 4)  # Sunday, January
    cycle_start = _cycle_start_for_day(21, today)
    db.upsert_core_block("cycle_start_date", cycle_start.isoformat())

    import datetime as _dt
    frozen = _dt.datetime(2026, 1, 4, 23, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = today
        mock_date.fromisoformat.side_effect = date.fromisoformat

        compute_cycle_state()

    mood = db.get_core_block("mood_today")
    assert mood == "tired"


def test_day21_sunday_winter_writes_cycle_state_core_block(monkeypatch):
    """compute_cycle_state persists the JSON dict to 'cycle_state' core_block."""
    from agents.reflection import compute_cycle_state

    today = date(2026, 1, 4)
    cycle_start = _cycle_start_for_day(21, today)
    db.upsert_core_block("cycle_start_date", cycle_start.isoformat())

    import datetime as _dt
    frozen = _dt.datetime(2026, 1, 4, 23, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = today
        mock_date.fromisoformat.side_effect = date.fromisoformat

        compute_cycle_state()

    raw = db.get_core_block("cycle_state")
    assert raw is not None
    state = json.loads(raw)
    assert state["cycle_phase"] == "inward"
    assert state["warmth_multiplier"] == pytest.approx(0.9, abs=0.001)


# ---------------------------------------------------------------------------
# 2. mood_today derivation — peak-social + lift → weirdly good
# ---------------------------------------------------------------------------

def test_peak_social_friday_mood_is_weirdly_good(monkeypatch):
    """Day 14 (peak-social) + Friday (lift) → mood_today = 'weirdly good'."""
    from agents.reflection import compute_cycle_state

    # 2026-01-09 is a Friday.
    today = date(2026, 1, 9)
    cycle_start = _cycle_start_for_day(14, today)
    db.upsert_core_block("cycle_start_date", cycle_start.isoformat())

    import datetime as _dt
    frozen = _dt.datetime(2026, 1, 9, 15, 0, 0)  # peak circadian

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = today
        mock_date.fromisoformat.side_effect = date.fromisoformat

        result = compute_cycle_state()

    assert result["cycle_phase"] == "peak-social"
    assert result["weekly_label"] == "lift"
    mood = db.get_core_block("mood_today")
    assert mood == "weirdly good"


def test_emergence_friday_mood_is_weirdly_good(monkeypatch):
    """Day 5 (emergence) + Friday (lift) → mood_today = 'weirdly good'."""
    from agents.reflection import compute_cycle_state

    # 2026-01-09 is a Friday.
    today = date(2026, 1, 9)
    cycle_start = _cycle_start_for_day(5, today)
    db.upsert_core_block("cycle_start_date", cycle_start.isoformat())

    import datetime as _dt
    frozen = _dt.datetime(2026, 1, 9, 15, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = today
        mock_date.fromisoformat.side_effect = date.fromisoformat

        result = compute_cycle_state()

    assert result["cycle_phase"] == "emergence"
    assert result["weekly_label"] == "lift"
    mood = db.get_core_block("mood_today")
    assert mood == "weirdly good"


def test_mood_fallback_is_focused(monkeypatch):
    """Combination not in _MOOD_TABLE → defaults to 'focused'."""
    from agents.reflection import compute_cycle_state

    # Day 10 (emergence) + Monday (reset) → not in table → focused.
    today = date(2026, 1, 5)  # Monday
    cycle_start = _cycle_start_for_day(10, today)
    db.upsert_core_block("cycle_start_date", cycle_start.isoformat())

    import datetime as _dt
    frozen = _dt.datetime(2026, 1, 5, 15, 0, 0)

    with (
        patch("agents.reflection.datetime") as mock_dt,
        patch("agents.reflection.date") as mock_date,
    ):
        mock_dt.now.return_value = frozen
        mock_date.today.return_value = today
        mock_date.fromisoformat.side_effect = date.fromisoformat

        result = compute_cycle_state()

    assert result["cycle_phase"] == "emergence"
    assert result["weekly_label"] == "reset"
    mood = db.get_core_block("mood_today")
    assert mood == "focused"


# ---------------------------------------------------------------------------
# 3. time_texture transitions via scheduler._hour_to_time_texture
# ---------------------------------------------------------------------------

def test_time_texture_22_is_late_night():
    from agents.scheduler import _hour_to_time_texture
    assert _hour_to_time_texture(22) == "late_night"


def test_time_texture_23_is_late_night():
    from agents.scheduler import _hour_to_time_texture
    assert _hour_to_time_texture(23) == "late_night"


def test_time_texture_0_is_late_night():
    """Midnight (0) wraps to virtual 24, which is inside late_night (22-26)."""
    from agents.scheduler import _hour_to_time_texture
    assert _hour_to_time_texture(0) == "late_night"


def test_time_texture_1_is_late_night():
    """01:00 wraps to virtual 25, still inside late_night (22-26)."""
    from agents.scheduler import _hour_to_time_texture
    assert _hour_to_time_texture(1) == "late_night"


def test_time_texture_2_is_deep_night():
    """02:00 wraps to virtual 26, entering deep_night (26-28)."""
    from agents.scheduler import _hour_to_time_texture
    assert _hour_to_time_texture(2) == "deep_night"


def test_time_texture_3_is_deep_night():
    """03:00 wraps to virtual 27, still deep_night."""
    from agents.scheduler import _hour_to_time_texture
    assert _hour_to_time_texture(3) == "deep_night"


def test_time_texture_4_is_early_morning():
    """04:00 is the first non-night phase."""
    from agents.scheduler import _hour_to_time_texture
    assert _hour_to_time_texture(4) == "early_morning"


def test_time_texture_job_writes_runtime_state(monkeypatch):
    """_time_texture_job writes the correct phase to db.runtime_state.

    The job imports datetime inside its body, so we patch via the module
    that is actually used at call time: ``datetime.datetime``.
    """
    import asyncio
    import datetime as _dt
    import zoneinfo

    from agents import config as _cfg
    from agents.scheduler import _time_texture_job

    # Freeze at 23:00 Oslo time (late_night).
    tz = zoneinfo.ZoneInfo("Europe/Oslo")
    frozen = _dt.datetime(2026, 1, 4, 23, 0, 0, tzinfo=tz)

    monkeypatch.setattr(_cfg, "get", lambda key, default=None: (
        "Europe/Oslo" if key == "scheduler.timezone" else default
    ))

    # _time_texture_job does `import datetime as _dt` locally; we patch the
    # now() factory on the real class so any import sees the same result.
    with patch("datetime.datetime") as mock_dt_class:
        mock_dt_class.now.return_value = frozen

        asyncio.run(_time_texture_job())

    val = db.runtime_get("time_texture")
    assert val == "late_night"
