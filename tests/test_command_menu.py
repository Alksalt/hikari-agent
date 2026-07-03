"""Command menu — conversational shortcuts, zero CommandHandlers."""
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
