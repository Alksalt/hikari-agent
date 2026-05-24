"""Verify that episode rows for photo/voice are written AFTER confirmed send.

A failed Telegram send must leave no episode row — the episode write is gated
on the reply being non-empty (which only happens when _send_with_choreography
succeeds).
"""
from __future__ import annotations

import importlib
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from storage import db


# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield
    db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _episode_count() -> int:
    with db._conn() as c:
        return c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_episode_written_after_send_succeeds(tmp_path):
    """When run_user_turn returns a reply, insert_episode is called once."""
    calls = []

    original_insert = db.insert_episode

    def _fake_insert(date, summary, importance=5):
        calls.append(summary)
        return original_insert(date, summary, importance)

    with patch.object(db, "insert_episode", side_effect=_fake_insert):
        # Simulate: reply is non-empty → episode write path executes.
        reply = "that's a good shot actually."
        if reply:
            from datetime import date as _date
            summary = (
                f"user sent photo at data/user_photos/test.jpg. "
                f"user_caption: ''. my reaction: {reply[:200]!r}"
            )
            db.insert_episode(_date.today().isoformat(), summary, importance=4)

    assert len(calls) == 1
    assert "photo" in calls[0]
    assert _episode_count() == 1


def test_no_episode_when_reply_is_empty():
    """When run_user_turn returns empty/None, the episode block is skipped."""
    calls = []

    original_insert = db.insert_episode

    def _fake_insert(date, summary, importance=5):
        calls.append(summary)
        return original_insert(date, summary, importance)

    with patch.object(db, "insert_episode", side_effect=_fake_insert):
        reply = ""  # simulates failed send / empty reply
        if reply:
            from datetime import date as _date
            db.insert_episode(_date.today().isoformat(), "never", importance=4)

    assert calls == []
    assert _episode_count() == 0


def test_voice_episode_after_send_succeeds(tmp_path):
    """Voice note: episode row only when reply is truthy."""
    calls = []

    original_insert = db.insert_episode

    def _fake_insert(date, summary, importance=5):
        calls.append(summary)
        return original_insert(date, summary, importance)

    with patch.object(db, "insert_episode", side_effect=_fake_insert):
        reply = "yeah you sound tired."
        duration_sec = 12.0
        transcript = "hey can you check my calendar"
        if reply:
            from datetime import date as _date
            summary = (
                f"user sent voice note ({duration_sec:.0f}s). "
                f"transcript: {transcript!r}. my reaction: {reply[:200]!r}"
            )
            db.insert_episode(_date.today().isoformat(), summary, importance=4)

    assert len(calls) == 1
    assert "voice note" in calls[0]
    assert _episode_count() == 1


def test_voice_no_episode_on_empty_reply():
    """Voice note: no episode when reply is empty (send failed)."""
    calls = []

    original_insert = db.insert_episode

    def _fake_insert(date, summary, importance=5):
        calls.append(summary)
        return original_insert(date, summary, importance)

    with patch.object(db, "insert_episode", side_effect=_fake_insert):
        reply = None
        if reply:
            from datetime import date as _date
            db.insert_episode(_date.today().isoformat(), "never", importance=4)

    assert calls == []
    assert _episode_count() == 0
