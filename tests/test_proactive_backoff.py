"""Tests for agents/proactive_backoff.py — ignore-driven backoff (Sprint 1).

Uses a local ``fresh_db`` fixture (the repo has no shared fixture of that
name in conftest.py; this mirrors the reload + _reset_schema_sentinel
pattern used by tests/test_schema_constraints.py's ``_isolated_db``).
"""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from agents import proactive_backoff
from storage import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield db
    db._reset_schema_sentinel()


def _insert_sent(source: str, sent_at: datetime) -> None:
    db.proactive_event_insert(
        source=source, pattern="nudge", payload_json="{}",
        chat_id=None, status="sent", dedup_key=None,
    )
    # normalize sent_at for the test (insert stamps now)
    with db._conn() as conn:
        conn.execute(
            "UPDATE proactive_events SET sent_at = ? WHERE id = "
            "(SELECT MAX(id) FROM proactive_events)",
            (sent_at.isoformat(),),
        )


def _insert_user_msg(ts: datetime) -> None:
    db.append_message("user", "ok", source="chat")
    with db._conn() as conn:
        conn.execute(
            "UPDATE messages SET ts = ? WHERE id = (SELECT MAX(id) FROM messages)",
            (ts.isoformat(),),
        )


def test_consecutive_ignores_counts_unanswered_sends(fresh_db):
    now = datetime.now(UTC)
    for days_ago in (3, 2, 1):
        _insert_sent("wiki_new_file", now - timedelta(days=days_ago))
    assert proactive_backoff.consecutive_ignores("wiki_new_file") == 3


def test_reply_within_window_resets_count(fresh_db):
    now = datetime.now(UTC)
    _insert_sent("wiki_new_file", now - timedelta(days=3))
    _insert_user_msg(now - timedelta(days=3) + timedelta(hours=1))
    _insert_sent("wiki_new_file", now - timedelta(days=1))
    assert proactive_backoff.consecutive_ignores("wiki_new_file") == 1


@pytest.mark.asyncio
async def test_sweep_snoozes_and_notifies_once(fresh_db, monkeypatch):
    from agents import proactive_gate
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)

    now = datetime.now(UTC)
    for days_ago in (3, 2, 1):
        _insert_sent("wiki_new_file", now - timedelta(days=days_ago))
    sent: list[str] = []

    async def fake_send(text):
        sent.append(text)
        return (text, 123, True)

    suppressed = await proactive_backoff.run_backoff_sweep(fake_send)
    assert suppressed == ["wiki_new_file"]
    snooze = json.loads(db.runtime_get("proactive_snooze_until"))
    assert "wiki_new_file" in snooze
    assert len(sent) == 1 and "wiki new file" in sent[0]
    # second sweep: no duplicate notice, no re-suppress
    suppressed2 = await proactive_backoff.run_backoff_sweep(fake_send)
    assert suppressed2 == []
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_exempt_sources_never_suppressed(fresh_db):
    now = datetime.now(UTC)
    for days_ago in (3, 2, 1):
        _insert_sent("reminder", now - timedelta(days=days_ago))

    async def fake_send(text):
        return (text, 1, True)

    assert await proactive_backoff.run_backoff_sweep(fake_send) == []
