"""Phase L — comfort_mode + anger_mode runtime flag detectors."""

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
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


@pytest.fixture(autouse=True)
def _reset_softening_cache():
    """Reset the module-level softening pattern cache between tests."""
    import agents.mode_dispatch as md
    md._SOFTENING_PATTERNS = None
    yield
    md._SOFTENING_PATTERNS = None


# ---------- comfort_mode ----------

def test_activate_comfort_mode_writes_state():
    from agents.mode_dispatch import activate_comfort_mode, current_comfort_mode
    activate_comfort_mode(trigger="i'm crying", kind="quiet")
    state = current_comfort_mode()
    assert state is not None
    assert state["kind"] == "quiet"
    assert "i'm crying" in state["trigger"]
    assert state["turns_remaining"] > 0


def test_comfort_mode_decay_clears_when_zero():
    from agents.mode_dispatch import (
        activate_comfort_mode,
        current_comfort_mode,
        decrement_comfort_turn,
    )
    # Override persist_turns to 1 via monkeypatching runtime_set directly
    activate_comfort_mode(trigger="test", kind="sharp")
    # Force turns_remaining to 1
    raw = db.runtime_get("comfort_mode_state")
    state = json.loads(raw)
    state["turns_remaining"] = 1
    db.runtime_set("comfort_mode_state", json.dumps(state))
    assert current_comfort_mode() is not None
    decrement_comfort_turn()
    assert current_comfort_mode() is None


def test_decrement_comfort_turn_decrements_by_one():
    from agents.mode_dispatch import activate_comfort_mode, decrement_comfort_turn
    activate_comfort_mode(trigger="heavy news", kind="raw")
    raw_before = db.runtime_get("comfort_mode_state")
    turns_before = json.loads(raw_before)["turns_remaining"]
    decrement_comfort_turn()
    raw_after = db.runtime_get("comfort_mode_state")
    if raw_after:
        turns_after = json.loads(raw_after)["turns_remaining"]
        assert turns_after == turns_before - 1
    # else: it was 1 and cleared — also valid


# ---------- anger_mode ----------

def test_activate_anger_mode_with_timeout():
    from agents.mode_dispatch import activate_anger_mode, current_anger_mode
    activate_anger_mode(trigger="shut up")
    state = current_anger_mode()
    assert state is not None
    assert "shut up" in state["trigger"]
    assert "expires_at" in state


def test_anger_mode_expires_after_timeout():
    from agents.mode_dispatch import activate_anger_mode, current_anger_mode
    activate_anger_mode(trigger="rude text")
    # Force expiry by writing a past timestamp
    raw = db.runtime_get("anger_mode_state")
    state = json.loads(raw)
    past = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    state["expires_at"] = past
    db.runtime_set("anger_mode_state", json.dumps(state))
    assert current_anger_mode() is None


# ---------- scan_softening ----------

def test_scan_softening_clears_anger():
    from agents.mode_dispatch import activate_anger_mode, current_anger_mode, scan_softening
    activate_anger_mode(trigger="rude message")
    assert current_anger_mode() is not None
    result = scan_softening("sorry i didn't mean that")
    assert result is True
    assert current_anger_mode() is None


def test_scan_softening_returns_false_on_non_softening():
    from agents.mode_dispatch import activate_anger_mode, current_anger_mode, scan_softening
    activate_anger_mode(trigger="rude")
    result = scan_softening("what time is it")
    assert result is False
    assert current_anger_mode() is not None  # anger persists


def test_scan_softening_empty_text_returns_false():
    from agents.mode_dispatch import scan_softening
    assert scan_softening("") is False
    assert scan_softening(None) is False  # type: ignore[arg-type]


# ---------- clear_on_session_boundary ----------

def test_clear_on_session_boundary_clears_both():
    from agents.mode_dispatch import (
        activate_anger_mode,
        activate_comfort_mode,
        clear_on_session_boundary,
        current_anger_mode,
        current_comfort_mode,
    )
    activate_comfort_mode(trigger="sad news", kind="quiet")
    activate_anger_mode(trigger="rude text")
    assert current_comfort_mode() is not None
    assert current_anger_mode() is not None
    clear_on_session_boundary()
    assert current_comfort_mode() is None
    assert current_anger_mode() is None


def test_clear_on_session_boundary_respects_config(monkeypatch):
    """When clear_on_session_boundary is False in config, mode persists."""
    from agents.mode_dispatch import (
        activate_anger_mode,
        clear_on_session_boundary,
        current_anger_mode,
    )
    # Disable clearing via config override
    monkeypatch.setattr(
        "agents.mode_dispatch.cfg.get",
        lambda key, default=None: (
            False if key in (
                "mode_flags.comfort.clear_on_session_boundary",
                "mode_flags.anger.clear_on_session_boundary",
            ) else default
        ),
    )
    activate_anger_mode(trigger="rude")
    clear_on_session_boundary()
    assert current_anger_mode() is not None
