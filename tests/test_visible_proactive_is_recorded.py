"""Codex P1 regression: visible proactive messages must be recorded in
`messages` with source='proactive' AFTER successful delivery.

Phase 13 (Stream C) moved the DB append for proactive messages to AFTER
the send_text call so no phantom rows appear if delivery fails.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


def _seed_heartbeat_conditions(monkeypatch):
    """Force should_send_heartbeat() → True and supply a seed + persona."""
    from agents import cadence, proactive

    # Force should_send_heartbeat to return True.
    monkeypatch.setattr(proactive, "should_send_heartbeat", lambda: True)

    # Provide a fake _pick_seed that returns (idx, seed_text, source).
    monkeypatch.setattr(proactive, "_pick_seed", lambda: (0, "thinking of you", "test"))

    # Force cadence governor to allow.
    monkeypatch.setattr(cadence, "can_send_proactive", lambda source: (True, "ok"))

    # Suppress _record_sent.
    monkeypatch.setattr(proactive, "_record_sent", lambda idx: None)


@pytest.mark.asyncio
async def test_heartbeat_appends_proactive_row_on_success(monkeypatch):
    """maybe_send_heartbeat appends an assistant row with source='proactive'
    when send_text succeeds."""
    from agents import proactive

    _seed_heartbeat_conditions(monkeypatch)

    # Stub run_proactive to return a fixed heartbeat text.
    async def fake_run_proactive(prompt, **kwargs):
        return "hm. you went quiet."
    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    sent: list[str] = []

    async def fake_send_text(text: str):
        sent.append(text)

    result = await proactive.maybe_send_heartbeat(fake_send_text)

    assert result is True
    assert sent == ["hm. you went quiet."]

    # DB must have exactly one assistant row with source='proactive'.
    with db._conn() as c:
        rows = c.execute(
            "SELECT role, content, source FROM messages WHERE role='assistant'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "hm. you went quiet."
    assert rows[0]["source"] == "proactive"


@pytest.mark.asyncio
async def test_heartbeat_no_row_when_send_fails(monkeypatch):
    """If send_text raises, no assistant row is appended (no phantom rows)."""
    from agents import proactive

    _seed_heartbeat_conditions(monkeypatch)

    async def fake_run_proactive(prompt, **kwargs):
        return "hm. you went quiet."
    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    async def failing_send_text(text: str):
        raise RuntimeError("telegram unreachable")

    result = await proactive.maybe_send_heartbeat(failing_send_text)

    assert result is False

    with db._conn() as c:
        rows = c.execute(
            "SELECT content FROM messages WHERE role='assistant'"
        ).fetchall()
    assert len(rows) == 0, (
        f"expected 0 rows after send failure, got {len(rows)}: "
        f"{[r['content'] for r in rows]}"
    )


@pytest.mark.asyncio
async def test_heartbeat_no_row_when_generation_returns_empty(monkeypatch):
    """If run_proactive returns empty or NO_MESSAGE, nothing is sent or recorded."""
    from agents import proactive

    _seed_heartbeat_conditions(monkeypatch)

    async def fake_run_proactive(prompt, **kwargs):
        return "NO_MESSAGE"
    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    sent: list[str] = []

    async def fake_send_text(text: str):
        sent.append(text)

    result = await proactive.maybe_send_heartbeat(fake_send_text)

    assert result is False
    assert len(sent) == 0

    with db._conn() as c:
        rows = c.execute(
            "SELECT content FROM messages WHERE role='assistant'"
        ).fetchall()
    assert len(rows) == 0
