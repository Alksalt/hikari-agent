"""Slow-burn tell gating: session-count threshold, cooldown, and enabled flag."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def _seed_sessions(n: int) -> None:
    """Insert one message per day for N days, each on a distinct calendar date."""
    base = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
    with db._conn() as c:
        for i in range(n):
            ts = (base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                "INSERT INTO messages (role, content, ts, source) VALUES (?, ?, ?, ?)",
                ("user", f"msg {i}", ts, "chat"),
            )


def test_no_sessions_returns_none():
    """With zero sessions, pick_slow_burn_tell returns None."""
    from agents.callback_surface import pick_slow_burn_tell
    assert pick_slow_burn_tell() is None


def test_below_lowest_threshold_returns_none():
    """Session count below the lowest min_session_count (80) returns None."""
    _seed_sessions(50)
    from agents.callback_surface import pick_slow_burn_tell
    assert pick_slow_burn_tell() is None


def test_at_threshold_returns_tell():
    """Session count at exactly 80 returns the first eligible tell."""
    _seed_sessions(80)
    from agents.callback_surface import pick_slow_burn_tell
    result = pick_slow_burn_tell()
    assert result is not None
    assert "text" in result
    assert "framing_hint" in result
    assert "min_session_count" in result
    assert result["min_session_count"] <= 80


def test_returns_highest_unlocked_tell():
    """With 150 sessions, returns the highest unlocked tell (min_session_count 150)."""
    _seed_sessions(150)
    from agents.callback_surface import pick_slow_burn_tell
    result = pick_slow_burn_tell()
    assert result is not None
    assert result["min_session_count"] == 150


def test_cooldown_blocks_second_call():
    """Cooldown only engages after mark_slow_burn_surfaced, not at pick time.

    pick_slow_burn_tell is a pure read — it does NOT write the cooldown.
    A second pick call with the same counter should still return the tell
    because mark_slow_burn_surfaced has not been called yet.  After calling
    mark_slow_burn_surfaced the cooldown IS written, so a subsequent pick
    (with the same counter) returns None.
    """
    _seed_sessions(80)
    db.runtime_set(db.INBOUND_MSG_COUNTER_KEY, 100)
    from agents.callback_surface import mark_slow_burn_surfaced, pick_slow_burn_tell
    first = pick_slow_burn_tell()
    assert first is not None
    # Pure-read: second pick still succeeds (cooldown not yet written).
    second = pick_slow_burn_tell()
    assert second is not None
    assert second["text"] == first["text"]
    # Now commit the cooldown as postsend would do.
    mark_slow_burn_surfaced(first["text"])
    # Counter unchanged → delta 0 < min_turns_between → blocked.
    blocked = pick_slow_burn_tell()
    assert blocked is None


def test_cooldown_clears_after_min_turns_between():
    """After min_turns_between turns, the tell is eligible again."""
    _seed_sessions(80)
    from agents.callback_surface import _LAST_SLOW_BURN_TELL_KEY, pick_slow_burn_tell
    min_between = int(config.get("slow_burn_tells.min_turns_between", 40))
    # Simulate last surface 'min_between' turns ago.
    db.runtime_set(_LAST_SLOW_BURN_TELL_KEY, 0)
    db.runtime_set(db.INBOUND_MSG_COUNTER_KEY, min_between)
    result = pick_slow_burn_tell()
    assert result is not None


def test_enabled_false_returns_none(monkeypatch):
    """When slow_burn_tells.enabled is false, returns None regardless of session count."""
    _seed_sessions(200)
    from agents import callback_surface
    _real_get = config.get

    def _patched_get(key, default=None):
        if key == "slow_burn_tells.enabled":
            return False
        return _real_get(key, default)

    monkeypatch.setattr(config, "get", _patched_get)
    result = callback_surface.pick_slow_burn_tell()
    assert result is None
