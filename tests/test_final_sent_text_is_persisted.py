"""Codex P0 regression: the row appended to `messages` must contain the
post-filter text the user actually saw, not the pre-filter draft that the
LLM produced.

Phase 13 (Stream C) moved the DB append AFTER the Telegram send so:
  - the draft is NEVER written to DB (no phantom rows)
  - on send failure, NO row is appended at all
  - on success, the FINAL text (post-filter) is appended
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


class _FakeMessage:
    """Minimal telegram Message stub."""
    def __init__(self, *, fail: bool = False):
        self.chat_id = 12345
        self._fail = fail
        self._replied: list[str] = []

    async def reply_text(self, text: str):
        if self._fail:
            raise RuntimeError("telegram send failed")
        self._replied.append(text)

        class _Sent:
            message_id = 42
        return _Sent()


class _FakeBot:
    async def send_chat_action(self, *, chat_id, action):
        pass


def _noop_handoff():
    pass


def _noop_postsend():
    pass


def _noop_drift_judge(*a, **kw):
    pass


def _noop_stickers(*a, **kw):
    pass


def _noop_affect(*a, **kw):
    pass


def _noop_belief(*a, **kw):
    pass


async def _run_send(reply_text: str, *, fail_send: bool = False, monkeypatch) -> None:
    """Import _send_with_choreography and call it with monkeypatched side effects."""
    from agents import post_filter
    from agents import telegram_bridge as tb

    # Patch filter_outgoing to short-replace DRAFT_TEXT → FINAL_TEXT.
    from agents.post_filter import FilterResult

    def fake_filter_outgoing(text: str) -> FilterResult:
        return FilterResult(
            text="FINAL_TEXT",
            refusal_short_replaced=True,
            refusal_hits=["test_trigger"],
            sycophancy_triggered=False,
            sycophancy_violations=[],
            needs_llm_rewrite=False,
            rewrite_instruction=None,
        )

    monkeypatch.setattr(post_filter, "filter_outgoing", fake_filter_outgoing)
    # Also patch the bridge's reference (it imports at module level via
    # "from .post_filter import filter_outgoing").
    monkeypatch.setattr(tb, "filter_outgoing", fake_filter_outgoing)

    # Suppress side effects: handoff, postsend, drift_judge, stickers.
    import agents.handoff as handoff_mod
    import agents.postsend as postsend_mod
    monkeypatch.setattr(handoff_mod, "write_handoff", _noop_handoff)
    monkeypatch.setattr(postsend_mod, "mark_pending_surfaced", _noop_postsend)
    monkeypatch.setattr(tb, "handoff_mod", handoff_mod)
    monkeypatch.setattr(tb, "postsend_mod", postsend_mod)

    msg = _FakeMessage(fail=fail_send)
    bot = _FakeBot()
    await tb._send_with_choreography(bot, msg, reply_text, elapsed_real=999.0)


@pytest.mark.asyncio
async def test_final_text_appended_not_draft(monkeypatch, tmp_path):
    """On successful send, the DB row holds FINAL_TEXT, NOT DRAFT_TEXT."""
    await _run_send("DRAFT_TEXT", monkeypatch=monkeypatch)
    with db._conn() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE role='assistant'"
        ).fetchall()
    assert len(rows) == 1, f"expected 1 assistant row, got {len(rows)}"
    assert rows[0]["content"] == "FINAL_TEXT", (
        f"expected 'FINAL_TEXT' but got {rows[0]['content']!r} — "
        "the draft was persisted instead of the post-filter text"
    )


@pytest.mark.asyncio
async def test_draft_never_appended(monkeypatch, tmp_path):
    """The pre-filter draft is never written to the DB under any circumstances."""
    await _run_send("DRAFT_TEXT", monkeypatch=monkeypatch)
    with db._conn() as c:
        rows = c.execute(
            "SELECT content FROM messages WHERE role='assistant'"
        ).fetchall()
    assert all(r["content"] != "DRAFT_TEXT" for r in rows), (
        "DRAFT_TEXT found in messages — pre-filter text was persisted"
    )


@pytest.mark.asyncio
async def test_no_row_on_send_failure(monkeypatch, tmp_path):
    """If Telegram send raises, NO assistant row is appended to messages."""
    await _run_send("DRAFT_TEXT", fail_send=True, monkeypatch=monkeypatch)
    with db._conn() as c:
        rows = c.execute(
            "SELECT content FROM messages WHERE role='assistant'"
        ).fetchall()
    assert len(rows) == 0, (
        f"expected 0 assistant rows after send failure, got {len(rows)}: "
        f"{[r['content'] for r in rows]}"
    )
