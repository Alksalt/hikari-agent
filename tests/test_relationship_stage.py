"""Relationship-stage threshold tests.

Tests ``compute_relationship_stage`` in ``agents/reflection.py``.

Stage thresholds from _STAGE_THRESHOLDS (min_sessions, stage, label):
  (1200, 7), (700, 6), (350, 5), (150, 4), (60, 3), (15, 2), (0, 1)

The function iterates top-down and picks the first entry where
``session_count >= min_sess``. Sessions are counted as DISTINCT calendar
dates with at least one message.

All tests are deterministic — no real LLM, no real network.
"""

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


# ---------------------------------------------------------------------------
# Helper: insert N messages each on a distinct day so session_count == N.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Stage 1: 0–14 sessions (< 15)
# ---------------------------------------------------------------------------

def test_zero_sessions_is_stage_1():
    """No messages → 0 sessions → stage 1."""
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 1


def test_one_session_is_stage_1():
    _seed_sessions(1)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 1


def test_fourteen_sessions_is_stage_1():
    """14 sessions (one below the stage-2 threshold of 15) → stage 1."""
    _seed_sessions(14)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 1


# ---------------------------------------------------------------------------
# Stage 2: 15–59 sessions
# ---------------------------------------------------------------------------

def test_fifteen_sessions_is_stage_2():
    """15 sessions (exactly the stage-2 threshold) → stage 2."""
    _seed_sessions(15)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 2


def test_fifty_nine_sessions_is_stage_2():
    """59 sessions (one below stage-3 threshold) → stage 2."""
    _seed_sessions(59)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 2


# ---------------------------------------------------------------------------
# Stage 3: 60–149 sessions
# ---------------------------------------------------------------------------

def test_sixty_sessions_is_stage_3():
    """60 sessions (exactly the stage-3 threshold) → stage 3."""
    _seed_sessions(60)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 3


def test_one_forty_nine_sessions_is_stage_3():
    _seed_sessions(149)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 3


# ---------------------------------------------------------------------------
# Stage 4: 150–349 sessions
# ---------------------------------------------------------------------------

def test_one_fifty_sessions_is_stage_4():
    """150 sessions (exactly the stage-4 threshold) → stage 4."""
    _seed_sessions(150)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 4


def test_three_forty_nine_sessions_is_stage_4():
    _seed_sessions(349)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 4


# ---------------------------------------------------------------------------
# Stage 5: 350–699 sessions
# ---------------------------------------------------------------------------

def test_three_fifty_sessions_is_stage_5():
    """350 sessions (exactly the stage-5 threshold) → stage 5."""
    _seed_sessions(350)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 5


def test_six_ninety_nine_sessions_is_stage_5():
    _seed_sessions(699)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 5


# ---------------------------------------------------------------------------
# Stage 6: 700–1199 sessions
# ---------------------------------------------------------------------------

def test_seven_hundred_sessions_is_stage_6():
    """700 sessions (exactly the stage-6 threshold) → stage 6."""
    _seed_sessions(700)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 6


def test_one_thousand_sessions_is_stage_6():
    _seed_sessions(1000)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 6


def test_eleven_ninety_nine_sessions_is_stage_6():
    _seed_sessions(1199)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 6


# ---------------------------------------------------------------------------
# Stage 7: 1200+ sessions
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_twelve_hundred_sessions_is_stage_7():
    """1200 sessions (exactly the stage-7 threshold) → stage 7."""
    _seed_sessions(1200)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 7


@pytest.mark.slow
def test_fifteen_hundred_sessions_is_stage_7():
    """1500 sessions (well above the stage-7 threshold) → stage 7."""
    _seed_sessions(1500)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 7


@pytest.mark.slow
def test_two_thousand_sessions_is_stage_7():
    _seed_sessions(2000)
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    assert stage == 7


# ---------------------------------------------------------------------------
# Side-effects: core_block persistence + meta
# ---------------------------------------------------------------------------

def test_stage_persisted_to_core_block():
    """compute_relationship_stage writes the stage number to 'relationship_stage'."""
    _seed_sessions(60)
    from agents.reflection import compute_relationship_stage
    compute_relationship_stage()
    raw = db.get_core_block("relationship_stage")
    assert raw == "3"


def test_stage_meta_persisted():
    """compute_relationship_stage writes JSON meta with 'label' and 'session_count'."""
    import json
    _seed_sessions(350)
    from agents.reflection import compute_relationship_stage
    compute_relationship_stage()
    raw = db.get_core_block("relationship_stage_meta")
    assert raw is not None
    meta = json.loads(raw)
    assert meta["label"] == "close"
    assert meta["session_count"] == 350


def test_multiple_messages_same_day_count_as_one_session():
    """Multiple messages on the same day are counted as a single session."""
    base = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
    with db._conn() as c:
        for hour in range(5):
            ts = base.replace(hour=hour).strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                "INSERT INTO messages (role, content, ts, source) VALUES (?, ?, ?, ?)",
                ("user", f"msg {hour}", ts, "chat"),
            )
    from agents.reflection import compute_relationship_stage
    stage = compute_relationship_stage()
    # 5 messages on 1 day = 1 session → stage 1
    assert stage == 1


def test_return_value_matches_core_block():
    """Return value from compute_relationship_stage equals the persisted core_block."""
    _seed_sessions(150)
    from agents.reflection import compute_relationship_stage
    returned = compute_relationship_stage()
    persisted = int(db.get_core_block("relationship_stage"))
    assert returned == persisted
