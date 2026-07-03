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
    # Tests below replace agents.post_filter / agents.messaging in sys.modules
    # with stubs and never restore them; snapshot + restore so a stub module
    # does not leak into other test files (it broke test_post_filter_voice_enforce
    # under non-default ordering).
    import sys as _sys
    _orig_mods = {k: _sys.modules.get(k) for k in ("agents.post_filter", "agents.messaging")}
    yield
    for _k, _v in _orig_mods.items():
        if _v is not None:
            _sys.modules[_k] = _v
        else:
            _sys.modules.pop(_k, None)


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

    def filter_outgoing(text: str, *, source=None):
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
    import sys
    import types

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

    def filter_outgoing(text: str, *, source=None):
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


@pytest.mark.asyncio
async def test_idempotency_key_collision_is_insert_or_ignore(monkeypatch, tmp_path):
    """Two sends with the same idempotency_key (same ms, same text) should not
    raise — the second media_outbox INSERT OR IGNORE silently deduplicates."""
    _patch_filter(monkeypatch)
    from unittest.mock import patch

    # Force the same timestamp so the idempotency key collides.
    fixed_ms = 1_700_000_000_000
    with patch("agents.messaging.time") as mock_time:
        mock_time.time.return_value = fixed_ms / 1000

        from agents.messaging import send_and_persist

        bot = _FakeBot()
        r1 = await send_and_persist(
            bot=bot, chat_id=7, text="same text", source="chat",
            skip_choreography=True,
        )
        r2 = await send_and_persist(
            bot=bot, chat_id=7, text="same text", source="chat",
            skip_choreography=True,
        )

    assert r1.ok is True
    assert r2.ok is True
    # Two messages were sent to Telegram.
    assert len(bot.sent_messages) == 2
    # media_outbox has at most 2 rows (second may be deduped to 1 if key truly identical)
    with db._conn() as c:
        count = c.execute("SELECT COUNT(*) FROM media_outbox").fetchone()[0]
    assert count >= 1


@pytest.mark.asyncio
async def test_already_filtered_skips_internal_filter(monkeypatch, tmp_path):
    """already_filtered=True: filter_outgoing must NOT be called; text persisted verbatim."""
    import sys
    import types

    # Patch agents.post_filter so filter_outgoing raises if called.
    mod = types.ModuleType("agents.post_filter")

    def filter_outgoing(text: str, *, source=None):
        raise AssertionError("filter_outgoing must not be called when already_filtered=True")

    async def rewrite_or_fallback(original, filtered, mood=None, where="bridge"):
        raise AssertionError("rewrite_or_fallback must not be called when already_filtered=True")

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
        text="hello",
        source="chat",
        skip_choreography=True,
        already_filtered=True,
    )

    assert result.ok is True
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_long_reply_is_chunked(monkeypatch, tmp_path):
    """A reply over the Telegram limit is split; each chunk is sent + persisted
    and only the LAST chunk carries the reply-quote (FIX 5)."""
    _patch_filter(monkeypatch)
    from agents.messaging import _TG_CHUNK_MAX, send_and_persist

    bot = _FakeBot()
    msg = _FakeMessage()
    para = "x" * (_TG_CHUNK_MAX - 100)
    long_text = "\n\n".join([para, para, para])  # → 3 chunks

    result = await send_and_persist(
        bot=bot, chat_id=7, text=long_text, source="chat",
        reply_to=msg, skip_choreography=True,
    )

    assert result.ok is True
    # First two chunks are plain sends; only the final one is a reply.
    assert len(bot.sent_messages) == 2
    assert len(msg.replied) == 1
    rows = _rows()
    assert len(rows) == 3, "every delivered chunk must be persisted"
    for r in rows:
        assert len(r["content"]) <= _TG_CHUNK_MAX


@pytest.mark.asyncio
async def test_send_failure_sends_fallback_ack(monkeypatch, tmp_path):
    """When a chat send fails outright, a short in-voice fallback ack goes out
    so the turn is never fully silent (FIX 5)."""
    _patch_filter(monkeypatch)
    from agents import messaging
    from agents.messaging import send_and_persist

    class _RecordingFailBot:
        def __init__(self):
            self.attempts: list[str] = []

        async def send_message(self, chat_id: int, text: str):
            self.attempts.append(text)
            raise RuntimeError("boom")

        async def send_chat_action(self, chat_id: int, action: str):
            pass

    bot = _RecordingFailBot()
    result = await send_and_persist(
        bot=bot, chat_id=7, text="hi there", source="chat",
        skip_choreography=True,
    )

    assert result.ok is False
    # Two send attempts: the real reply, then the fallback ack.
    assert messaging._SEND_FAIL_ACK in bot.attempts
    # Fallback is ephemeral — no persisted rows.
    assert len(_rows()) == 0


@pytest.mark.asyncio
async def test_crash_mid_send_leaves_recoverable_row(monkeypatch, tmp_path):
    """When the Telegram send raises, a RECOVERABLE media_outbox row is left.

    Since the 2026-06-03 fix the inline failure path passes max_attempts=3, so
    the FIRST failure leaves the row in 'sending' (not terminalized to 'failed') —
    the stale-sending reaper requeues it and the drain re-sends. The row must
    never be silently lost.
    """
    _patch_filter(monkeypatch)
    from agents.messaging import send_and_persist

    bot = _FakeBot(fail=True)
    result = await send_and_persist(
        bot=bot, chat_id=7, text="will crash", source="chat",
        skip_choreography=True,
    )

    assert result.ok is False
    # DB must have a row (the pre-send INSERT) in a recoverable/terminal state.
    with db._conn() as c:
        rows = c.execute(
            "SELECT status FROM media_outbox"
        ).fetchall()
    assert len(rows) == 1
    # 'sending' is the new first-failure state (retryable); not terminalized.
    assert rows[0]["status"] in ("pending", "sending", "failed")
    assert rows[0]["status"] != "failed", "must not terminalize on the first failure"
