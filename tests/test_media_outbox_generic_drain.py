"""_drain_media_outbox: multi-kind drain dispatches correctly.

- Generic drain function handles text, sticker, document, photo kinds.
- _drain_photo_outbox is a legacy alias that drains only photo rows.
- kinds= kwarg filters which rows are processed.
- Returns {kind: sent_count} dict.
- Rows for unknown kinds are skipped with a warning.
"""
from __future__ import annotations

import importlib
import uuid
from pathlib import Path

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
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_pending(kind: str, payload: dict | None = None) -> int:
    payload = payload or {"chat_id": 7, "source": "chat", "text": "test", "photo_path": None}
    ikey = f"ikey-{kind}-{uuid.uuid4().hex}"
    row_id = db.media_outbox_insert(kind, ikey, payload)
    return row_id


class _SentMsg:
    def __init__(self, message_id: int = 99):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.sent_messages: list[str] = []
        self.sent_photos: list[str] = []
        self.sent_stickers: list[str] = []
        self.sent_documents: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> _SentMsg:
        self.sent_messages.append(text)
        return _SentMsg()

    async def send_photo(self, chat_id: int, photo, caption=None) -> _SentMsg:
        self.sent_photos.append(caption or "")
        return _SentMsg()

    async def send_sticker(self, chat_id: int, sticker: str) -> _SentMsg:
        self.sent_stickers.append(sticker)
        return _SentMsg()

    async def send_document(self, chat_id: int, document, caption=None) -> _SentMsg:
        self.sent_documents.append(caption or "")
        return _SentMsg()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_text_kind(tmp_path):
    """Pending text row is sent and count returned."""
    payload = {"chat_id": 7, "source": "chat", "text": "hello from outbox", "photo_path": None}
    _insert_pending("text", payload)

    from agents.telegram_bridge import _drain_media_outbox

    bot = _FakeBot()
    counts = await _drain_media_outbox(bot, 7, kinds=("text",))

    assert counts["text"] == 1
    assert len(bot.sent_messages) == 1
    assert bot.sent_messages[0] == "hello from outbox"


@pytest.mark.asyncio
async def test_drain_returns_per_kind_counts(tmp_path):
    """drain with multiple kinds returns count for each."""
    _insert_pending("text", {"chat_id": 7, "source": "chat", "text": "t1", "photo_path": None})
    _insert_pending("text", {"chat_id": 7, "source": "chat", "text": "t2", "photo_path": None})

    from agents.telegram_bridge import _drain_media_outbox

    bot = _FakeBot()
    counts = await _drain_media_outbox(bot, 7, kinds=("text", "sticker"))

    assert counts["text"] == 2
    assert counts["sticker"] == 0


@pytest.mark.asyncio
async def test_drain_photo_alias_counts_only_photos(tmp_path):
    """_drain_photo_outbox alias drains only photo kind and returns int count."""
    # Insert a text row that must NOT be sent.
    _insert_pending("text", {"chat_id": 7, "source": "chat", "text": "text row", "photo_path": None})

    from agents.telegram_bridge import _drain_photo_outbox

    bot = _FakeBot()
    count = await _drain_photo_outbox(bot, 7)

    assert isinstance(count, int)
    assert count == 0  # no photo rows inserted
    assert bot.sent_messages == []  # text row not touched


@pytest.mark.asyncio
async def test_drain_marks_row_sent():
    """After successful drain, media_outbox row status becomes 'sent'."""
    payload = {"chat_id": 7, "source": "chat", "text": "persisted", "photo_path": None}
    row_id = _insert_pending("text", payload)

    from agents.telegram_bridge import _drain_media_outbox

    bot = _FakeBot()
    await _drain_media_outbox(bot, 7, kinds=("text",))

    with db._conn() as c:
        row = c.execute("SELECT status FROM media_outbox WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "sent"


@pytest.mark.asyncio
async def test_drain_increments_attempts_on_send_error():
    """When send_message raises, row attempts is incremented (not yet failed on first try)."""
    payload = {"chat_id": 7, "source": "chat", "text": "will fail", "photo_path": None}
    row_id = _insert_pending("text", payload)

    class _FailBot:
        async def send_message(self, chat_id, text):
            raise RuntimeError("network error")

    from agents.telegram_bridge import _drain_media_outbox

    bot = _FailBot()
    counts = await _drain_media_outbox(bot, 7, kinds=("text",))

    assert counts["text"] == 0

    with db._conn() as c:
        row = c.execute("SELECT status, attempts, last_error FROM media_outbox WHERE id=?", (row_id,)).fetchone()
    # After 1st failure (max_attempts=3): still pending but attempts incremented.
    assert row["attempts"] == 1
    assert "send_message raised" in row["last_error"]


@pytest.mark.asyncio
async def test_kinds_kwarg_filters_processing():
    """kinds=('text',) does not process sticker rows."""
    _insert_pending("text", {"chat_id": 7, "source": "chat", "text": "text", "photo_path": None})
    row_id = _insert_pending("sticker", {"chat_id": 7, "file_id": "ABC123"})

    from agents.telegram_bridge import _drain_media_outbox

    bot = _FakeBot()
    counts = await _drain_media_outbox(bot, 7, kinds=("text",))

    assert "sticker" not in counts

    # Sticker row must still be pending.
    with db._conn() as c:
        row = c.execute("SELECT status FROM media_outbox WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_drain_sticker_kind(tmp_path):
    """Pending sticker row is sent successfully and count reflects."""
    payload = {"chat_id": 7, "file_id": "STICKER_FILE_ID_ABC"}
    _insert_pending("sticker", payload)

    from agents.telegram_bridge import _drain_media_outbox

    bot = _FakeBot()
    counts = await _drain_media_outbox(bot, 7, kinds=("sticker",))

    assert counts["sticker"] == 1
    assert len(bot.sent_stickers) == 1
    assert bot.sent_stickers[0] == "STICKER_FILE_ID_ABC"
