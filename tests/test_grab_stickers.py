"""Phase 9 Stage B — owner-only sticker-pack install via /grab_stickers.

Covers:
  - /grab_stickers start sets capture mode + acks
  - inbound owner stickers in capture mode get logged with their file_id
  - duplicate file_ids don't get re-appended
  - /grab_stickers stop emits a YAML snippet and clears capture mode
  - /grab_stickers reset wipes the pool
  - non-owner stickers are ignored entirely
  - outside capture mode, owner stickers are silently dropped
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


def _owner_msg(text: str | None = None, sticker_id: str | None = None):
    msg = SimpleNamespace(
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        chat_id=12345,
        text=text,
        sticker=(SimpleNamespace(file_id=sticker_id) if sticker_id else None),
    )
    user = SimpleNamespace(id=12345)
    return SimpleNamespace(effective_user=user, message=msg)


def _ctx(args: list[str] | None = None):
    return SimpleNamespace(
        args=args or [],
        bot=AsyncMock(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))),
    )


@pytest.mark.asyncio
async def test_start_command_turns_capture_on():
    from agents import telegram_bridge
    update = _owner_msg(text="/grab_stickers start")
    await telegram_bridge.cmd_grab_stickers(update, _ctx(["start"]))
    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_MODE_KEY) == "1"
    update.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_inbound_sticker_in_capture_mode_records_file_id():
    from agents import telegram_bridge
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_MODE_KEY, "1")

    update = _owner_msg(sticker_id="abc123")
    await telegram_bridge.handle_inbound_sticker(update, _ctx())

    pool = json.loads(db.runtime_get(telegram_bridge._STICKER_CAPTURE_POOL_KEY) or "[]")
    assert pool == ["abc123"]


@pytest.mark.asyncio
async def test_inbound_sticker_dedupe_does_not_double_log():
    from agents import telegram_bridge
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_MODE_KEY, "1")
    update = _owner_msg(sticker_id="abc123")
    await telegram_bridge.handle_inbound_sticker(update, _ctx())
    await telegram_bridge.handle_inbound_sticker(update, _ctx())
    pool = json.loads(db.runtime_get(telegram_bridge._STICKER_CAPTURE_POOL_KEY) or "[]")
    assert pool == ["abc123"]
    # Second call replies with "already have"
    assert update.message.reply_text.await_count == 2


@pytest.mark.asyncio
async def test_inbound_sticker_outside_capture_mode_ignored():
    from agents import telegram_bridge
    # capture mode NOT set
    update = _owner_msg(sticker_id="xyz")
    await telegram_bridge.handle_inbound_sticker(update, _ctx())
    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_POOL_KEY) in (None, "[]")
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_inbound_sticker_non_owner_ignored():
    from agents import telegram_bridge
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_MODE_KEY, "1")

    msg = SimpleNamespace(
        reply_text=AsyncMock(),
        text=None,
        sticker=SimpleNamespace(file_id="hacker_id"),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=99999),  # not owner
        message=msg,
    )
    await telegram_bridge.handle_inbound_sticker(update, _ctx())
    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_POOL_KEY) in (None, "[]")


@pytest.mark.asyncio
async def test_stop_emits_yaml_snippet_and_turns_off_capture():
    from agents import telegram_bridge
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_MODE_KEY, "1")
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_POOL_KEY,
                   json.dumps(["fid_one", "fid_two"]))

    update = _owner_msg()
    await telegram_bridge.cmd_grab_stickers(update, _ctx(["stop"]))

    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_MODE_KEY) is None
    # Pool retained for further captures if needed.
    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_POOL_KEY)

    sent_text = update.message.reply_text.await_args.args[0]
    assert "stickers:" in sent_text
    assert "fid_one" in sent_text
    assert "fid_two" in sent_text


@pytest.mark.asyncio
async def test_stop_with_no_captures_says_so():
    from agents import telegram_bridge
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_MODE_KEY, "1")
    update = _owner_msg()
    await telegram_bridge.cmd_grab_stickers(update, _ctx(["stop"]))
    sent = update.message.reply_text.await_args.args[0]
    assert "captured nothing" in sent.lower()


@pytest.mark.asyncio
async def test_reset_clears_pool():
    from agents import telegram_bridge
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_MODE_KEY, "1")
    db.runtime_set(telegram_bridge._STICKER_CAPTURE_POOL_KEY, json.dumps(["x"]))

    update = _owner_msg()
    await telegram_bridge.cmd_grab_stickers(update, _ctx(["reset"]))

    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_MODE_KEY) is None
    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_POOL_KEY) is None


@pytest.mark.asyncio
async def test_status_no_arg_shows_state():
    from agents import telegram_bridge
    update = _owner_msg()
    await telegram_bridge.cmd_grab_stickers(update, _ctx([]))
    sent = update.message.reply_text.await_args.args[0]
    assert "off" in sent.lower() or "on" in sent.lower()


@pytest.mark.asyncio
async def test_command_rejects_non_owner():
    from agents import telegram_bridge
    msg = SimpleNamespace(reply_text=AsyncMock(), text="/grab_stickers start")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=99999),
        message=msg,
    )
    await telegram_bridge.cmd_grab_stickers(update, _ctx(["start"]))
    msg.reply_text.assert_not_called()
    assert db.runtime_get(telegram_bridge._STICKER_CAPTURE_MODE_KEY) is None
