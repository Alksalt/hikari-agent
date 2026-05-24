"""9B: set_my_commands matches handler list and descriptions fit Telegram limit."""
from __future__ import annotations

import pytest
from telegram.ext import CommandHandler


def _collect_registered_command_names() -> set[str]:
    """Build the application and collect every CommandHandler's command names."""
    import os
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake:token")
    os.environ.setdefault("OWNER_TELEGRAM_ID", "12345")
    from agents.telegram_bridge import build_application
    app = build_application()
    names: set[str] = set()
    for handler in app.handlers.get(0, []):
        if isinstance(handler, CommandHandler):
            names.update(handler.commands)
    return names


def test_set_my_commands_matches_handler_list():
    """Every command in _COMMANDS must have a real CommandHandler registered."""
    from agents import cockpit
    registered = _collect_registered_command_names()
    for cmd in cockpit._COMMANDS:
        assert cmd in registered, (
            f"/_COMMANDS key {cmd!r} has no CommandHandler in build_application()"
        )


def test_set_my_commands_descriptions_within_telegram_limit():
    """All descriptions must fit the Telegram 256-char limit for BotCommand."""
    from agents import cockpit
    for cmd, desc in cockpit._COMMANDS.items():
        assert len(desc) <= 256, (
            f"/{cmd} description is {len(desc)} chars — Telegram limit is 256"
        )
