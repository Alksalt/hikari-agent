"""Command menu — conversational shortcuts, zero CommandHandlers."""
from __future__ import annotations

import datetime
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import command_menu


def test_known_command_resolves_to_phrase():
    assert command_menu.resolve_command_phrase("/help") == "what can you do?"


def test_botname_suffix_stripped():
    assert command_menu.resolve_command_phrase("/help@SomeBot") == "what can you do?"


def test_args_use_template_when_defined():
    out = command_menu.resolve_command_phrase("/remind call mom at 17:00")
    assert out == "set a reminder: call mom at 17:00"


def test_args_appended_when_no_template():
    out = command_menu.resolve_command_phrase("/jobs gjøvik")
    assert out == "job hunt status — what's due and what's next? — gjøvik"


def test_unknown_command_returns_none():
    assert command_menu.resolve_command_phrase("/start") is None


def test_non_command_returns_none():
    assert command_menu.resolve_command_phrase("what can you do?") is None
    assert command_menu.resolve_command_phrase("") is None


@pytest.mark.asyncio
async def test_push_command_menu_sets_all_three_surfaces():
    bot = AsyncMock()
    await command_menu.push_command_menu(bot)
    bot.set_my_commands.assert_awaited_once()
    cmds = bot.set_my_commands.await_args.args[0]
    assert [c.command for c in cmds][:2] == ["brief", "email"]
    bot.set_my_description.assert_awaited_once()
    bot.set_my_short_description.assert_awaited_once()


@pytest.mark.asyncio
async def test_push_command_menu_never_raises():
    bot = AsyncMock()
    bot.set_my_commands.side_effect = RuntimeError("telegram down")
    await command_menu.push_command_menu(bot)  # must not raise
    bot.set_my_description.assert_awaited_once()  # later surfaces still pushed


def test_configured_commands_dont_collide_with_approval_keyword():
    """Sanity check on telegram.command_menu config, not a behavioral guard:
    no configured command literally starts with CONFIRM (which would read as
    an approval-resolution keyword if the rewrite ever ran before the
    approval pre-router), and every configured command has a non-empty
    phrase to rewrite to."""
    for entry in command_menu._menu():
        assert not str(entry["command"]).upper().startswith("CONFIRM")
        assert str(entry["phrase"]).strip()  # phrase is non-empty for every cmd


@pytest.fixture
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


@pytest.mark.asyncio
async def test_pre_routers_see_raw_text_not_phrase(_isolated_db, monkeypatch):
    """The /cmd → canonical-phrase rewrite happens in handle_message AFTER
    the daily-checkin and approval pre-routers run (agents/telegram_bridge.py
    ~:751 reads raw message.text for the approval check; the rewrite at
    ~:830 only affects user_text, which is what reaches respond()). Guard
    this end-to-end: send "/help" through handle_message and assert the
    approval resolver saw the raw "/help" while respond() saw the rewritten
    "what can you do?"."""
    from telegram import Chat, Message, Update, User

    from agents import telegram_bridge

    owner = 12345
    user = User(id=owner, first_name="test", is_bot=False)
    chat = Chat(id=owner, type="private")
    message = Message(
        message_id=1,
        date=datetime.datetime.now(datetime.UTC),
        chat=chat,
        from_user=user,
        text="/help",
    )
    update = Update(update_id=1, message=message)

    bot = SimpleNamespace(send_chat_action=AsyncMock())
    ctx = SimpleNamespace(bot=bot)

    resolve_mock = AsyncMock(return_value=False)
    respond_mock = AsyncMock(return_value="")

    monkeypatch.setattr(
        telegram_bridge.daily_checkin_mod, "handle_message",
        AsyncMock(return_value=(False, None)),
    )
    monkeypatch.setattr(
        telegram_bridge.approval_tools, "resolve_pending_approval", resolve_mock,
    )
    monkeypatch.setattr(telegram_bridge, "respond", respond_mock)
    monkeypatch.setattr(
        telegram_bridge.reactions_mod, "maybe_react", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(telegram_bridge.affect_mod, "scan_inbound", lambda _: None)

    await telegram_bridge.handle_message(update, ctx)

    resolve_mock.assert_awaited_once_with(chat.id, "/help")
    respond_mock.assert_awaited_once()
    called_with = respond_mock.call_args.args[0]
    assert called_with == "what can you do?"
