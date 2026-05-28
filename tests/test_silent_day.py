"""tests/test_silent_day.py — Phase F: "The Daily Nothing" silent-day gate.

Tests:
  1. test_picker_writes_to_runtime_state       — picker writes a YYYY-MM-DD value
  2. test_picker_picks_from_pool               — chosen day is in the configured pool
  3. test_picker_disabled_clears_state         — disabled config clears the key
  4. test_gate_returns_false_when_no_silent_day_set  — empty state → not gated
  5. test_gate_returns_true_when_today_is_silent     — today set → gated
  6. test_gate_returns_false_when_silent_day_is_past — yesterday set → not gated
  7. test_proactive_eligibility_short_circuits_on_silent_day — full reserve_and_send
     returns aborted with reason="silent_day" when today is the silent day.
"""
from __future__ import annotations

import importlib
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Fresh DB + runtime_state isolated to this test."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as db_mod
    importlib.reload(db_mod)
    db_mod._reset_schema_sentinel()
    db_mod.get_session_id()
    yield db_mod


@pytest.fixture()
def cfg_silent_day_enabled(tmp_path, monkeypatch):
    """Config with silent day enabled, pool = mon–fri."""
    cfg_text = (
        "engagement:\n"
        "  weekly_silent_day_enabled: true\n"
        "  weekly_silent_day_pool: [mon, tue, wed, thu, fri]\n"
    )
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    from agents import config
    config.reload()
    yield config


@pytest.fixture()
def cfg_silent_day_disabled(tmp_path, monkeypatch):
    """Config with silent day disabled."""
    cfg_text = (
        "engagement:\n"
        "  weekly_silent_day_enabled: false\n"
        "  weekly_silent_day_pool: [mon, tue, wed, thu, fri]\n"
    )
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    from agents import config
    config.reload()
    yield config


# ---------------------------------------------------------------------------
# 1. Picker writes a valid ISO date to runtime_state
# ---------------------------------------------------------------------------

def test_picker_writes_to_runtime_state(isolated_db, cfg_silent_day_enabled):
    from agents.scheduler import _pick_silent_day_this_week

    _pick_silent_day_this_week()

    raw = isolated_db.runtime_get("silent_day_this_week")
    assert raw is not None, "expected silent_day_this_week to be set"
    # Must be parseable as a date
    parsed = date.fromisoformat(raw)
    assert parsed > date.today(), "silent day must be in the coming week, not today"


# ---------------------------------------------------------------------------
# 2. Picker only picks days from the configured pool
# ---------------------------------------------------------------------------

def test_picker_picks_from_pool(isolated_db, cfg_silent_day_enabled):
    from agents.scheduler import _pick_silent_day_this_week

    day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4}
    pool_nums = set(day_map.values())  # 0..4 = Mon–Fri

    results = set()
    for _ in range(40):          # enough draws to see coverage
        _pick_silent_day_this_week()
        raw = isolated_db.runtime_get("silent_day_this_week")
        assert raw is not None
        d = date.fromisoformat(raw)
        results.add(d.weekday())  # 0=Mon … 4=Fri

    # Every picked weekday must be in the pool
    assert results <= pool_nums, f"picked days outside pool: {results - pool_nums}"


# ---------------------------------------------------------------------------
# 3. Picker with feature disabled clears the key
# ---------------------------------------------------------------------------

def test_picker_disabled_clears_state(isolated_db, cfg_silent_day_disabled):
    # Seed a stale value first
    isolated_db.runtime_set("silent_day_this_week", date.today().isoformat())

    from agents.scheduler import _pick_silent_day_this_week
    _pick_silent_day_this_week()

    raw = isolated_db.runtime_get("silent_day_this_week")
    assert raw is None, "disabled picker must clear the runtime key"


# ---------------------------------------------------------------------------
# 4. Gate returns False when no silent day is set
# ---------------------------------------------------------------------------

def test_gate_returns_false_when_no_silent_day_set(isolated_db, monkeypatch):
    # Ensure key is absent
    isolated_db.runtime_set("silent_day_this_week", None)

    import agents.proactive_gate as pg
    importlib.reload(pg)

    # Patch db import inside the function
    with patch("agents.proactive_gate._is_silent_day_today", wraps=lambda: _call_with_db(isolated_db)):
        pass  # just verify the direct call works

    # Call the real function (which uses the DB path we've overridden via env)
    assert pg._is_silent_day_today() is False


def _call_with_db(db):
    """Helper: call _is_silent_day_today() after ensuring the env var is set."""
    from agents.proactive_gate import _is_silent_day_today
    return _is_silent_day_today()


# ---------------------------------------------------------------------------
# 5. Gate returns True when today is the silent day
# ---------------------------------------------------------------------------

def test_gate_returns_true_when_today_is_silent(isolated_db):
    today_iso = date.today().isoformat()
    isolated_db.runtime_set("silent_day_this_week", today_iso)

    import agents.proactive_gate as pg
    importlib.reload(pg)

    assert pg._is_silent_day_today() is True


# ---------------------------------------------------------------------------
# 6. Gate returns False when the silent day is in the past
# ---------------------------------------------------------------------------

def test_gate_returns_false_when_silent_day_is_past(isolated_db):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    isolated_db.runtime_set("silent_day_this_week", yesterday)

    import agents.proactive_gate as pg
    importlib.reload(pg)

    assert pg._is_silent_day_today() is False


# ---------------------------------------------------------------------------
# 7. Full reserve_and_send returns aborted with reason="silent_day"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_eligibility_short_circuits_on_silent_day(isolated_db, monkeypatch):
    """reserve_and_send must abort with reason='silent_day' when today is the silent day."""
    today_iso = date.today().isoformat()
    isolated_db.runtime_set("silent_day_this_week", today_iso)

    import agents.proactive_gate as pg
    importlib.reload(pg)

    # Patch _is_quiet_now so we know silent_day fires FIRST (not quiet_hours)
    monkeypatch.setattr(pg, "_is_quiet_now", lambda _db=None: False)

    send_called = []

    async def _mock_send(text):
        send_called.append(text)
        return (text, 42, True)

    result = await pg.reserve_and_send(
        send_text_fn=_mock_send,
        producer_id="wiki_new_file",
        pattern="observation",
        text="new file in wiki: something.md",
        db=isolated_db,
    )

    assert result.status == "aborted"
    assert result.reason == "silent_day"
    assert send_called == [], "send_text_fn must not be called on silent day"
