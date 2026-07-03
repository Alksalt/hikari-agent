"""Window-math regression: db.py time-window queries compare _now() ISO-T
timestamps against a Python-computed cutoff, never SQLite's space-separated
datetime('now',...). The two formats diverge at index 10 ('T' 0x54 > ' ' 0x20),
so a space cutoff silently mis-sorts any same-UTC-day row. These tests insert
rows that are HOURS (not same-instant) from the boundary — the case the old
same-instant tests never exercised."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr("storage.db._DB_PATH", tmp_path / "hikari.db")
    yield


def _insert_event(sent_at: str, *, source: str, dedup_key: str | None = None) -> None:
    from storage import db
    with db._conn() as c:
        c.execute(
            "INSERT INTO proactive_events "
            "(sent_at, source, pattern, payload_json, status, dedup_key) "
            "VALUES (?, ?, 'p', '{}', 'sent', ?)",
            (sent_at, source, dedup_key),
        )


def test_dedup_hit_respects_window_for_hours_old_same_day_row():
    """FIX 1: a dedup row 3h old must NOT count against a 60-min window. The old
    datetime('now','-60 minutes') cutoff returned True for any same-UTC-day row."""
    from storage import db
    now = datetime.now(UTC)
    old = (now - timedelta(hours=3)).isoformat()
    _insert_event(old, source="src_dedup", dedup_key="k_old")
    assert db.proactive_event_dedup_hit("src_dedup", "k_old", 60) is False

    recent = (now - timedelta(minutes=10)).isoformat()
    _insert_event(recent, source="src_dedup", dedup_key="k_recent")
    assert db.proactive_event_dedup_hit("src_dedup", "k_recent", 60) is True


def test_send_count_7d_excludes_row_just_over_seven_days():
    """FIX 2: a send 7d+1h old is outside the 7-day window; a 1d-old send is
    inside. The old cutoff counted the boundary-day row regardless of its time."""
    from storage import db
    now = datetime.now(UTC)
    _insert_event((now - timedelta(days=7, hours=1)).isoformat(), source="src7")
    _insert_event((now - timedelta(days=1)).isoformat(), source="src7")
    assert db.proactive_send_count_7d("src7") == 1


def test_prune_messages_keeps_row_just_inside_and_drops_just_outside():
    """FIX 2: prune(days=1) drops a message 1d+1h old, keeps one 12h old — even
    when both share the boundary UTC day with the cutoff."""
    from storage import db
    now = datetime.now(UTC)
    with db._conn() as c:
        c.execute(
            "INSERT INTO messages (role, content, ts) VALUES ('user', 'stale', ?)",
            ((now - timedelta(days=1, hours=1)).isoformat(),),
        )
        c.execute(
            "INSERT INTO messages (role, content, ts) VALUES ('user', 'fresh', ?)",
            ((now - timedelta(hours=12)).isoformat(),),
        )
    deleted = db.prune_messages_older_than_days(1)
    assert deleted == 1
    with db._conn() as c:
        remaining = [r[0] for r in c.execute("SELECT content FROM messages").fetchall()]
    assert remaining == ["fresh"]


def test_decisions_cooldown_boundary_uses_python_cutoff():
    """FIX 2: a decision asked 14d+1h ago is past a 14-day cooldown (included);
    one asked 1h ago is still cooling (excluded)."""
    from storage import db
    now = datetime.now(UTC)
    did_stale = db.decision_insert("stale", 0.6, "2026-01-01")
    did_recent = db.decision_insert("recent", 0.6, "2026-01-01")
    with db._conn() as c:
        c.execute(
            "UPDATE decisions SET asked_at = ? WHERE id = ?",
            ((now - timedelta(days=14, hours=1)).isoformat(), did_stale),
        )
        c.execute(
            "UPDATE decisions SET asked_at = ? WHERE id = ?",
            ((now - timedelta(hours=1)).isoformat(), did_recent),
        )
    due_ids = {r["id"] for r in db.decisions_unresolved_due(cooldown_days=14, limit=50)}
    assert did_stale in due_ids
    assert did_recent not in due_ids
