"""Cross-session emotional half-life: arm, inject, decay, and no-signal guard."""
from __future__ import annotations

import importlib
import json
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


# ---------------------------------------------------------------------------
# arm: silence trigger
# ---------------------------------------------------------------------------

def test_arm_writes_prior_session_heavy_on_silence():
    """Setting silenced_until_msg_id arms prior_session_heavy with l4_silence."""
    db.runtime_set("silenced_until_msg_id", "42")
    from agents import cross_session
    cross_session.arm_if_heavy()
    raw = db.runtime_get("prior_session_heavy")
    assert raw is not None
    state = json.loads(raw)
    assert state["trigger"] == "l4_silence"
    assert "ts" in state


def test_arm_writes_prior_session_heavy_on_anger():
    """Activating anger mode arms prior_session_heavy with l3_refusal."""
    from agents import mode_dispatch, cross_session
    mode_dispatch.activate_anger_mode("rude test trigger")
    cross_session.arm_if_heavy()
    raw = db.runtime_get("prior_session_heavy")
    assert raw is not None
    state = json.loads(raw)
    assert state["trigger"] == "l3_refusal"


def test_arm_does_nothing_with_no_heavy_signal():
    """No heavy signal → arm_if_heavy writes nothing."""
    from agents import cross_session
    cross_session.arm_if_heavy()
    assert db.runtime_get("prior_session_heavy") is None


def test_repair_move_does_not_arm():
    """repair_move in the triggers config does not arm (detector is None this phase)."""
    from agents import cross_session
    # Verify _check_trigger directly returns False for repair_move.
    assert cross_session._check_trigger("repair_move") is False
    assert db.runtime_get("prior_session_heavy") is None


def test_overt_warmth_event_does_not_arm():
    """overt_warmth_event does not arm (detector is None this phase)."""
    from agents import cross_session
    assert cross_session._check_trigger("overt_warmth_event") is False


# ---------------------------------------------------------------------------
# inject: armed + within decay_turns → format block
# ---------------------------------------------------------------------------

def test_format_block_when_armed_and_within_decay():
    """Armed + session_turn_count within decay_turns → inject returns a softness block."""
    from agents import cross_session
    db.runtime_set(
        "prior_session_heavy",
        json.dumps({"trigger": "l4_silence", "ts": datetime.now(UTC).isoformat()}),
    )
    decay_turns = int(config.get("emotional_half_life.cross_session.decay_turns", 5))
    db.runtime_set("session_turn_count", decay_turns - 1)

    state = cross_session.consume_softer_opener()
    assert state is not None
    assert state["trigger"] == "l4_silence"


def test_hooks_format_prior_session_heavy_returns_string():
    """_format_prior_session_heavy returns a non-empty string when armed within decay."""
    from agents import cross_session
    db.runtime_set(
        "prior_session_heavy",
        json.dumps({"trigger": "l3_refusal", "ts": datetime.now(UTC).isoformat()}),
    )
    decay_turns = int(config.get("emotional_half_life.cross_session.decay_turns", 5))
    db.runtime_set("session_turn_count", 1)

    softness_factor = float(config.get("emotional_half_life.cross_session.softness_factor", 0.18))
    expected_pct = int(softness_factor * 100)

    from agents.hooks import _format_prior_session_heavy
    result = _format_prior_session_heavy()
    assert result is not None
    assert "opening softer" in result
    assert "l3_refusal" in result
    assert str(expected_pct) in result


# ---------------------------------------------------------------------------
# decay: past decay_turns → clears the flag
# ---------------------------------------------------------------------------

def test_decay_clears_flag_after_decay_turns():
    """session_turn_count > decay_turns → consume_softer_opener returns None and clears."""
    from agents import cross_session
    db.runtime_set(
        "prior_session_heavy",
        json.dumps({"trigger": "l4_silence", "ts": datetime.now(UTC).isoformat()}),
    )
    decay_turns = int(config.get("emotional_half_life.cross_session.decay_turns", 5))
    db.runtime_set("session_turn_count", decay_turns + 1)

    result = cross_session.consume_softer_opener()
    assert result is None
    assert db.runtime_get("prior_session_heavy") is None


def test_not_armed_consume_returns_none():
    """If prior_session_heavy is not set, consume_softer_opener returns None."""
    from agents import cross_session
    result = cross_session.consume_softer_opener()
    assert result is None


# ---------------------------------------------------------------------------
# leak guards: a calm rotation clears a stale flag; old flags expire by wall clock
# ---------------------------------------------------------------------------

def test_arm_clears_stale_flag_when_no_heavy_signal():
    """A rotation with no heavy signal clears a previous session's flag, so the
    softer opener cannot leak into a later, non-heavy session."""
    from agents import cross_session
    db.runtime_set(
        "prior_session_heavy",
        json.dumps({"trigger": "l4_silence", "ts": datetime.now(UTC).isoformat()}),
    )
    # Fresh DB: no silence/anger/affect signal active.
    cross_session.arm_if_heavy()
    assert db.runtime_get("prior_session_heavy") is None


def test_wall_clock_expiry_clears_old_flag_within_decay():
    """An armed flag older than max_arm_age_hours expires even within decay_turns
    (guards an intervening short session that never advanced the turn counter)."""
    from agents import cross_session
    max_age_h = float(config.get("emotional_half_life.cross_session.max_arm_age_hours", 36))
    old_ts = (datetime.now(UTC) - timedelta(hours=max_age_h + 1)).isoformat()
    db.runtime_set(
        "prior_session_heavy",
        json.dumps({"trigger": "l4_silence", "ts": old_ts}),
    )
    db.runtime_set("session_turn_count", 1)  # within decay window
    result = cross_session.consume_softer_opener()
    assert result is None
    assert db.runtime_get("prior_session_heavy") is None
