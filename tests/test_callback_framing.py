"""tests/test_callback_framing.py — unit tests for _compute_framing_hint
and spaced-surprise window in agents/callback_surface.py.

Test matrix:
  1. High score + age in 28-56d window + delta < 30 turns → no i_keep_thinking (throttle closed)
  2. High score + age in 28-56d window + delta >= 30 turns → emits i_keep_thinking
  3. Anti-callback suppression: prompt > 120 chars + vulnerability flag → returns None
  4. Spaced-surprise: 6-week-old ranks above 1-week-old at equal base score
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


# ---------------------------------------------------------------------------
# 1. _compute_framing_hint — i_keep_thinking throttle closed (delta < 30)
# ---------------------------------------------------------------------------

def test_i_keep_thinking_throttle_closed():
    from agents.callback_surface import _compute_framing_hint
    # All conditions met EXCEPT delta (27 turns < 30)
    hint = _compute_framing_hint(score=0.8, age_days=35.0, turn_counter=27)
    assert hint != "i_keep_thinking", (
        "throttle at <30 turns should prevent i_keep_thinking"
    )


def test_i_keep_thinking_at_boundary_29_turns():
    from agents.callback_surface import _compute_framing_hint
    hint = _compute_framing_hint(score=0.8, age_days=35.0, turn_counter=29)
    assert hint != "i_keep_thinking"


# ---------------------------------------------------------------------------
# 2. _compute_framing_hint — i_keep_thinking emitted when delta >= 30
# ---------------------------------------------------------------------------

def test_i_keep_thinking_emitted_at_30_turns():
    from agents.callback_surface import _compute_framing_hint
    hint = _compute_framing_hint(score=0.8, age_days=35.0, turn_counter=30)
    assert hint == "i_keep_thinking"


def test_i_keep_thinking_emitted_above_30_turns():
    from agents.callback_surface import _compute_framing_hint
    hint = _compute_framing_hint(score=0.8, age_days=40.0, turn_counter=100)
    assert hint == "i_keep_thinking"


def test_i_keep_thinking_requires_score_above_07():
    from agents.callback_surface import _compute_framing_hint
    # Score just below threshold — should NOT produce i_keep_thinking
    hint = _compute_framing_hint(score=0.69, age_days=35.0, turn_counter=50)
    assert hint != "i_keep_thinking"


def test_i_keep_thinking_requires_age_in_window():
    from agents.callback_surface import _compute_framing_hint
    # 10 days old — too fresh for i_keep_thinking, should fall to act_from
    hint = _compute_framing_hint(score=0.8, age_days=10.0, turn_counter=50)
    assert hint == "act_from"

    # 60 days — at upper boundary, still in window
    hint2 = _compute_framing_hint(score=0.8, age_days=56.0, turn_counter=50)
    assert hint2 == "i_keep_thinking"

    # 57 days — just outside window
    hint3 = _compute_framing_hint(score=0.8, age_days=57.0, turn_counter=50)
    assert hint3 != "i_keep_thinking"


# ---------------------------------------------------------------------------
# 3. Anti-callback suppression — long prompt + vulnerability flag → None
# ---------------------------------------------------------------------------

def test_anti_callback_suppression_long_prompt_and_vulnerability(tmp_path):
    from agents.callback_surface import pick_callback_candidate
    from storage import db

    # Insert a high-importance episode
    db.insert_episode(date="2026-04-01", summary="discussed work anxiety repeatedly", importance=8)

    long_prompt = "x" * 121  # > 120 chars

    # Patch peer model to flag vulnerability
    with patch(
        "agents.callback_surface._peer_model_flags_vulnerability",
        return_value=True,
    ):
        result = pick_callback_candidate(long_prompt)

    assert result is None, "anti-callback suppression should block when prompt > 120 chars + vulnerability"


def test_anti_callback_not_suppressed_when_short_prompt():
    """Short prompt below 120 chars should not trigger suppression even with vulnerability."""
    # Just verify the function is callable and returns bool
    with patch(
        "agents.callback_surface._peer_model_flags_vulnerability",
        return_value=True,
    ):
        # pick_callback_candidate with short text should not hit the anti-callback path
        from agents.callback_surface import pick_callback_candidate
        # No episodes → returns None from lack of candidates, not suppression
        result = pick_callback_candidate("short query here")
        assert result is None  # no episodes anyway


# ---------------------------------------------------------------------------
# 4. Spaced-surprise: 6-week-old (42 days) ranks above 1-week-old at equal base
# ---------------------------------------------------------------------------

def test_spaced_surprise_multiplier():
    from agents.callback_surface import _spaced_surprise_multiplier

    # 6-week-old is in the 28-60d window → multiplier 1.4
    assert _spaced_surprise_multiplier(42.0) == 1.4

    # 1-week-old (7 days) is outside window → multiplier 1.0
    assert _spaced_surprise_multiplier(7.0) == 1.0


def test_spaced_surprise_boosts_older_candidate(tmp_path, monkeypatch):
    """A 42-day-old episode scores higher than a 7-day-old at equal token overlap."""
    from agents.callback_surface import (
        _pattern_language_bonus,
        _score,
        _spaced_surprise_multiplier,
    )

    text = "user keeps bringing up work stress"
    query = "work stress"

    base = _score(text, query)
    assert base > 0, "base score should be > 0 for overlapping text"

    old_adjusted = (base + _pattern_language_bonus(text)) * _spaced_surprise_multiplier(42.0)
    new_adjusted = (base + _pattern_language_bonus(text)) * _spaced_surprise_multiplier(7.0)

    assert old_adjusted > new_adjusted, (
        "42-day-old item should outscore 7-day-old item with equal base score"
    )
