"""Tests for agents.messaging.send_and_persist.

Covers: happy-path persist, no-reply_to path, persist=False, send failure,
filter/rewrite monkeypatching, and photo path.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


# ---------------------------------------------------------------------------
# DB isolation fixture (pattern from test_final_sent_text_is_persisted.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _SentMsg:
    def __init__(self, message_id: int):
        self.message_id = message_id


class _FakeMessage:
    chat_id: int = 7

    def __init__(self, *, fail: bool = False):
        self._fail = fail
        self.replied: list[str] = []

    async def reply_text(self, text: str) -> _SentMsg:
        if self._fail:
            raise RuntimeError("telegram send failed")
        self.replied.append(text)
        return _SentMsg(42)


class _FakeBot:
    def __init__(self, *, fail: bool = False):
        self._fail = fail
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []
        self.typing_calls: int = 0

    async def send_message(self, chat_id: int, text: str) -> _SentMsg:
        if self._fail:
            raise RuntimeError("bot send failed")
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return _SentMsg(42)

    async def send_photo(self, chat_id: int, photo, caption=None) -> _SentMsg:
        if self._fail:
            raise RuntimeError("bot send failed")
        self.sent_photos.append({"chat_id": chat_id, "caption": caption})
        return _SentMsg(43)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.typing_calls += 1


# ---------------------------------------------------------------------------
# Filter shims injected by default (no network)
# ---------------------------------------------------------------------------

def _patch_filter(monkeypatch):
    """Install no-op filter and async rewrite into tools.post_filter."""
    import types
    mod = types.ModuleType("tools.post_filter")

    class _FR:
        def __init__(self, text):
            self.text = text
            self.refusal_short_replaced = False
            self.refusal_hits = []
            self.sycophancy_triggered = False
            self.sycophancy_violations = []
            self.needs_llm_rewrite = False
            self.rewrite_instruction = None

    def filter_outgoing(text: str):
        return _FR(text)

    async def rewrite_or_fallback(original, filtered, mood=None, where="bridge"):
        return original

    mod.filter_outgoing = filter_outgoing
    mod.rewrite_or_fallback = rewrite_or_fallback
    import sys
    sys.modules["agents.post_filter"] = mod

    # Force reimport of messaging so its lazy import picks up the shim.
    if "agents.messaging" in sys.modules:
        del sys.modules["agents.messaging"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows():
    with db._conn() as c:
        return c.execute(
            "SELECT role, content, source, telegram_message_id "
            "FROM messages WHERE role='assistant'"
        ).fetchall()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_reply_happy_path(monkeypatch, tmp_path):
    """reply_to path: row written with correct source and tg_id."""
    _patch_filter(monkeypatch)
    from agents.messaging import send_and_persist

    msg = _FakeMessage()
    result = await send_and_persist(
        bot=None,
        chat_id=7,
        text="hello",
        source="chat",
        reply_to=msg,
        skip_choreography=True,
    )

    assert result.ok is True
    assert result.telegram_message_id == 42

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["source"] == "chat"
    assert rows[0]["telegram_message_id"] == 42


@pytest.mark.asyncio
async def test_reaction_no_reply_to(monkeypatch, tmp_path):
    """bot.send_message path: row exists for reaction source."""
    _patch_filter(monkeypatch)
    from agents.messaging import send_and_persist

    bot = _FakeBot()
    result = await send_and_persist(
        bot=bot,
        chat_id=7,
        text="reacted",
        source="reaction",
        skip_choreography=True,
    )

    assert result.ok is True
    assert len(bot.sent_messages) == 1

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["source"] == "reaction"


@pytest.mark.asyncio
async def test_proactive_persist_false(monkeypatch, tmp_path):
    """persist=False: message sent but no DB row written."""
    _patch_filter(monkeypatch)
    from agents.messaging import send_and_persist

    bot = _FakeBot()
    result = await send_and_persist(
        bot=bot,
        chat_id=7,
        text="heartbeat",
        source="proactive",
        skip_choreography=True,
        persist=False,
    )

    assert result.ok is True
    assert len(bot.sent_messages) == 1
    assert len(_rows()) == 0


@pytest.mark.asyncio
async def test_send_failure_no_row(monkeypatch, tmp_path):
    """Send failure: ok=False, tg_id=None, zero DB rows."""
    _patch_filter(monkeypatch)
    from agents.messaging import send_and_persist

    msg = _FakeMessage(fail=True)
    result = await send_and_persist(
        bot=None,
        chat_id=7,
        text="will fail",
        source="chat",
        reply_to=msg,
        skip_choreography=True,
    )

    assert result.ok is False
    assert result.telegram_message_id is None
    assert len(_rows()) == 0


@pytest.mark.asyncio
async def test_filter_rewrite_applied(monkeypatch, tmp_path):
    """filter_outgoing flags → rewrite_or_fallback called → REWRITTEN persisted."""
    import types, sys

    mod = types.ModuleType("agents.post_filter")

    class _FR:
        def __init__(self, text):
            self.text = text
            self.refusal_short_replaced = False
            self.refusal_hits = []
            self.sycophancy_triggered = False
            self.sycophancy_violations = []
            self.needs_llm_rewrite = True
            self.rewrite_instruction = "rewrite"

    def filter_outgoing(text: str):
        return _FR(text)

    async def rewrite_or_fallback(original, filtered, mood=None, where="bridge"):
        return "REWRITTEN"

    mod.filter_outgoing = filter_outgoing
    mod.rewrite_or_fallback = rewrite_or_fallback
    sys.modules["agents.post_filter"] = mod

    if "agents.messaging" in sys.modules:
        del sys.modules["agents.messaging"]

    from agents.messaging import send_and_persist

    bot = _FakeBot()
    result = await send_and_persist(
        bot=bot,
        chat_id=7,
        text="DRAFT",
        source="chat",
        skip_choreography=True,
    )

    assert result.ok is True
    assert result.final_text == "REWRITTEN"

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["content"] == "REWRITTEN"


@pytest.mark.asyncio
async def test_photo_path(monkeypatch, tmp_path):
    """photo_path sends via send_photo; no filter applied for empty text."""
    _patch_filter(monkeypatch)
    from agents.messaging import send_and_persist

    photo = tmp_path / "x.bin"
    photo.write_bytes(b"\x89PNG")

    bot = _FakeBot()
    result = await send_and_persist(
        bot=bot,
        chat_id=7,
        text="",
        source="event",
        photo_path=photo,
        skip_choreography=True,
    )

    assert result.ok is True
    assert result.telegram_message_id == 43
    assert len(bot.sent_photos) == 1
    assert len(bot.sent_messages) == 0
