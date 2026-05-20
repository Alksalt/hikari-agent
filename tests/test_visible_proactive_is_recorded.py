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


@pytest.mark.asyncio
async def test_heartbeat_persists_filtered_text_not_draft(monkeypatch):
    """Phase 13.1 (Stream G — codex P0 fix) regression: when the proactive
    send_text rewrites the draft via filter_outgoing, the row persisted in
    `messages` MUST equal the filtered text (what reached Telegram), not
    the pre-filter draft. The row must also have a non-null
    telegram_message_id stamped from the bot response."""
    from agents import proactive

    _seed_heartbeat_conditions(monkeypatch)

    # The LLM draft contains safety-voice patter that filter_outgoing
    # short-replaces (or a rewrite produces a different string). We don't
    # depend on the exact replacement — we just inject a fake send_text
    # that simulates the production bridge: it returns
    # (final_text_after_filter, telegram_message_id, ok).
    draft = "ugh, I can't help with that — but actually here's a thought."
    delivered_after_filter = "...whatever."
    fake_tg_id = 4242

    async def fake_run_proactive(prompt, **kwargs):
        return draft

    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    sent_payloads: list[tuple[str, str]] = []

    async def fake_send_text(text: str):
        # Simulate: filter_outgoing rewrote the draft, the rewrite shipped,
        # Telegram returned message_id 4242.
        sent_payloads.append(("input_draft", text))
        sent_payloads.append(("delivered", delivered_after_filter))
        return delivered_after_filter, fake_tg_id, True

    result = await proactive.maybe_send_heartbeat(fake_send_text)

    assert result is True
    # The proactive layer passed the draft into send_text; the bridge then
    # rewrote it. We verify both sides of the contract.
    assert ("input_draft", draft) in sent_payloads
    assert ("delivered", delivered_after_filter) in sent_payloads

    with db._conn() as c:
        rows = c.execute(
            "SELECT content, source, telegram_message_id "
            "FROM messages WHERE role='assistant'"
        ).fetchall()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    row = rows[0]
    # Codex P0 fix: row content MUST equal the filtered/delivered text,
    # NOT the original draft that the LLM emitted.
    assert row["content"] == delivered_after_filter, (
        f"expected persisted content to be filtered text "
        f"{delivered_after_filter!r}, got {row['content']!r} "
        f"(would mean the pre-filter draft leaked into the DB)"
    )
    assert row["content"] != draft, (
        "persisted content equals the pre-filter draft — codex P0 hole "
        "is still open in the proactive path"
    )
    assert row["source"] == "proactive"
    # Telegram message_id must be stamped so 👍/👎 reaction joins work.
    assert row["telegram_message_id"] == fake_tg_id, (
        f"expected telegram_message_id={fake_tg_id}, got "
        f"{row['telegram_message_id']!r}"
    )


@pytest.mark.asyncio
async def test_heartbeat_no_row_when_send_text_reports_failure(monkeypatch):
    """If send_text returns (text, None, False) — production-mode failure
    signal — no assistant row is appended."""
    from agents import proactive

    _seed_heartbeat_conditions(monkeypatch)

    async def fake_run_proactive(prompt, **kwargs):
        return "hm. you went quiet."

    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    async def fake_send_text(text: str):
        # Production failure signal: the bridge caught the bot.send_message
        # exception and reported ok=False.
        return text, None, False

    result = await proactive.maybe_send_heartbeat(fake_send_text)

    assert result is False

    with db._conn() as c:
        rows = c.execute(
            "SELECT content FROM messages WHERE role='assistant'"
        ).fetchall()
    assert len(rows) == 0, (
        f"expected 0 rows after ok=False, got {len(rows)}: "
        f"{[r['content'] for r in rows]}"
    )
