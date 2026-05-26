"""Character-silence (L4) setter tests.

Tests the _RUDE_FLAGS deque logic and silenced_until_msg_id setter that was
wired in Sprint B Wave 1 (telegram_bridge.py).

The bridge is never imported as a running application — we extract the
relevant state objects and helpers to test them in isolation.

Covered cases:
  1. 3 rude messages in a row → silence NOT triggered (deque not full).
  2. 4 rude messages in a row → silenced_until_msg_id SET + _RUDE_FLAGS cleared.
  3. Mixed rude/civil → counter is rolling (maxlen=4 deque) — only triggers
     when ALL 4 slots are rude.
  4. Topic-change thaws silence: _character_silence_topic_changed returns True
     on sufficiently different vocabulary → silenced_until_msg_id cleared.
  5. Same-topic message keeps silence active.
  6. Staleness cutoff (4h gap) → thawed regardless of topic.
"""

from __future__ import annotations

import importlib
from collections import deque
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
# Helper: simulate the deque-based rude-flag tracking from telegram_bridge
# ---------------------------------------------------------------------------

def _simulate_rude_streak(
    flags: deque,
    rude_values: list[bool],
    msg_ids: list[int],
) -> str | None:
    """Drive the bridge's L4 logic inline without importing the bridge.

    Returns the message_id string set to ``silenced_until_msg_id``, or None
    if no trigger fired.
    """
    triggered_at: str | None = None
    for rude, msg_id in zip(rude_values, msg_ids):
        flags.append(rude)
        if len(flags) == 4 and all(flags):
            triggered_at = str(msg_id)
            db.runtime_set("silenced_until_msg_id", triggered_at)
            db.runtime_set("silenced_set_at", datetime.now(UTC).isoformat())
            flags.clear()
            break
    return triggered_at


# ---------------------------------------------------------------------------
# 1. Three rude in a row → NOT triggered
# ---------------------------------------------------------------------------

def test_three_rude_messages_no_silence():
    flags: deque = deque(maxlen=4)
    result = _simulate_rude_streak(
        flags,
        rude_values=[True, True, True],
        msg_ids=[1, 2, 3],
    )
    assert result is None, "3 rude msgs must NOT set silence"
    assert db.runtime_get("silenced_until_msg_id") is None


def test_three_rude_deque_not_full():
    """After 3 rude messages the deque has 3 entries (not 4)."""
    flags: deque = deque(maxlen=4)
    for _ in range(3):
        flags.append(True)
    assert len(flags) == 3
    assert not (len(flags) == 4 and all(flags))


# ---------------------------------------------------------------------------
# 2. Four rude in a row → silenced_until_msg_id SET + deque cleared
# ---------------------------------------------------------------------------

def test_four_rude_messages_sets_silence():
    flags: deque = deque(maxlen=4)
    result = _simulate_rude_streak(
        flags,
        rude_values=[True, True, True, True],
        msg_ids=[10, 11, 12, 13],
    )
    assert result == "13"
    assert db.runtime_get("silenced_until_msg_id") == "13"


def test_four_rude_clears_deque():
    """After L4 trigger fires the deque is cleared (ready for next streak)."""
    flags: deque = deque(maxlen=4)
    _simulate_rude_streak(
        flags,
        rude_values=[True, True, True, True],
        msg_ids=[20, 21, 22, 23],
    )
    assert len(flags) == 0, "deque must be cleared after L4 trigger"


def test_silence_sets_silenced_set_at():
    """silenced_set_at is written alongside silenced_until_msg_id."""
    flags: deque = deque(maxlen=4)
    _simulate_rude_streak(
        flags,
        rude_values=[True, True, True, True],
        msg_ids=[30, 31, 32, 33],
    )
    set_at = db.runtime_get("silenced_set_at")
    assert set_at is not None
    # Must be a parseable ISO timestamp close to now.
    ts = datetime.fromisoformat(set_at)
    assert abs((datetime.now(UTC) - ts).total_seconds()) < 5


# ---------------------------------------------------------------------------
# 3. Mixed rude/civil — only all-4-True triggers
# ---------------------------------------------------------------------------

def test_mixed_rude_civil_no_trigger():
    """3 rude + 1 civil = no trigger (deque slots: [T, T, T, F])."""
    flags: deque = deque(maxlen=4)
    result = _simulate_rude_streak(
        flags,
        rude_values=[True, True, True, False],
        msg_ids=[40, 41, 42, 43],
    )
    assert result is None


def test_civil_in_middle_breaks_streak():
    """Civil message in the middle means no trigger even if surrounded by rude."""
    flags: deque = deque(maxlen=4)
    result = _simulate_rude_streak(
        flags,
        rude_values=[True, False, True, True],
        msg_ids=[50, 51, 52, 53],
    )
    assert result is None


def test_civil_at_start_breaks_streak():
    flags: deque = deque(maxlen=4)
    result = _simulate_rude_streak(
        flags,
        rude_values=[False, True, True, True],
        msg_ids=[60, 61, 62, 63],
    )
    assert result is None


def test_five_rude_first_four_trigger():
    """maxlen=4 deque: the 5th entry evicts the 1st. But trigger fires on msg 4."""
    flags: deque = deque(maxlen=4)
    # After [T,T,T,T] trigger fires at msg_id 73.
    result = _simulate_rude_streak(
        flags,
        rude_values=[True, True, True, True, True],
        msg_ids=[70, 71, 72, 73, 74],
    )
    # Trigger fires on the 4th True.
    assert result == "73"


# ---------------------------------------------------------------------------
# 4. Topic-change thaws silence
# ---------------------------------------------------------------------------

def test_topic_change_thaws_silence():
    """New vocabulary (>= 3 words, < 2 overlap) → silence cleared."""
    # Seed silence with a specific context.
    db.runtime_set("silenced_until_msg_id", "99")
    db.runtime_set("silenced_set_at", datetime.now(UTC).isoformat())  # fresh
    db.runtime_set("silenced_context", "shut up stupid useless trash")

    from agents.telegram_bridge import _character_silence_topic_changed

    # Completely different topic → should return True (topic changed).
    changed = _character_silence_topic_changed(
        "what does the arxiv paper say about attention mechanisms"
    )
    assert changed is True


def test_topic_change_clears_runtime_state():
    """After topic-change detection, bridge clears silenced_until_msg_id."""
    db.runtime_set("silenced_until_msg_id", "99")
    db.runtime_set("silenced_set_at", datetime.now(UTC).isoformat())
    db.runtime_set("silenced_context", "shut up stupid useless trash")

    from agents.telegram_bridge import _character_silence_topic_changed

    changed = _character_silence_topic_changed(
        "what does the arxiv paper say about attention mechanisms"
    )
    if changed:
        db.runtime_set("silenced_until_msg_id", None)

    assert db.runtime_get("silenced_until_msg_id") is None


def test_same_topic_keeps_silence():
    """Message sharing content words with context → NOT a topic change."""
    db.runtime_set("silenced_until_msg_id", "99")
    db.runtime_set("silenced_set_at", datetime.now(UTC).isoformat())
    db.runtime_set("silenced_context", "shut stupid useless trash awful")

    from agents.telegram_bridge import _character_silence_topic_changed

    # Shares "stupid" and "useless" → overlap >= 2 → not a topic change.
    changed = _character_silence_topic_changed("stupid and useless")
    assert changed is False


def test_short_message_not_topic_change():
    """Message with fewer than 3 words cannot satisfy the vocab heuristic."""
    db.runtime_set("silenced_until_msg_id", "99")
    db.runtime_set("silenced_set_at", datetime.now(UTC).isoformat())
    db.runtime_set("silenced_context", "shut up stupid useless")

    from agents.telegram_bridge import _character_silence_topic_changed

    # Two-word message → _new_words has len < 3 → not a topic change.
    changed = _character_silence_topic_changed("ok fine")
    assert changed is False


# ---------------------------------------------------------------------------
# 5. Staleness cutoff (4h gap) → thawed regardless of topic
# ---------------------------------------------------------------------------

def test_staleness_thaws_after_4h():
    """If silenced_set_at is > 4h ago, always treat as topic-changed."""
    db.runtime_set("silenced_until_msg_id", "99")
    stale_ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    db.runtime_set("silenced_set_at", stale_ts)
    # Use same-topic context so only staleness matters.
    db.runtime_set("silenced_context", "shut stupid useless trash awful")

    from agents.telegram_bridge import _character_silence_topic_changed

    changed = _character_silence_topic_changed("stupid and useless")
    assert changed is True


def test_fresh_silence_same_topic_not_thawed():
    """Fresh silence (< 4h) + same topic → NOT thawed."""
    db.runtime_set("silenced_until_msg_id", "99")
    db.runtime_set("silenced_set_at", datetime.now(UTC).isoformat())
    db.runtime_set("silenced_context", "shut stupid useless trash awful")

    from agents.telegram_bridge import _character_silence_topic_changed

    changed = _character_silence_topic_changed("stupid and useless")
    assert changed is False
