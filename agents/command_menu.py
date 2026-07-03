"""Curated Telegram command menu — discoverability UI, not a control plane.

There are NO CommandHandlers (DECISIONS.md 2026-07-03). A menu tap or typed
/cmd is rewritten to its canonical phrase by ``resolve_command_phrase`` and
enters the normal stateful agent turn in ``handle_message``. The agent stays
the only control plane; the menu exists so the owner can SEE capabilities.

Config: ``telegram.command_menu`` / ``telegram.bot_description`` /
``telegram.bot_short_description`` in config/engagement.yaml.
"""
from __future__ import annotations

import logging

from . import config as cfg

logger = logging.getLogger(__name__)


def _menu() -> list[dict]:
    entries = cfg.get("telegram.command_menu") or []
    return [e for e in entries if isinstance(e, dict) and e.get("command") and e.get("phrase")]


def resolve_command_phrase(text: str) -> str | None:
    """Map '/cmd' / '/cmd@Bot' / '/cmd args' to its canonical phrase.

    Returns None for unknown commands and non-command text — callers pass
    those through untouched (unknown /foo reaches the agent as plain text).
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    head, _, args = t.partition(" ")
    head = head.split("@", 1)[0].lower()
    args = args.strip()
    for entry in _menu():
        if head != "/" + str(entry["command"]).lower():
            continue
        if args and entry.get("phrase_with_args"):
            return str(entry["phrase_with_args"]).replace("{args}", args)
        if args:
            return f"{entry['phrase']} — {args}"
        return str(entry["phrase"])
    return None


async def push_command_menu(bot) -> None:
    """Push menu + bot descriptions at boot. Each surface best-effort —
    a Telegram error on one must not skip the others. Never raises."""
    from telegram import BotCommand

    try:
        await bot.set_my_commands(
            [BotCommand(str(e["command"]), str(e.get("menu_text") or e["phrase"])) for e in _menu()]
        )
        logger.info("command_menu: pushed %d commands", len(_menu()))
    except Exception:
        logger.exception("command_menu: set_my_commands failed (non-fatal)")
    try:
        desc = str(cfg.get("telegram.bot_description") or "").strip()
        if desc:
            await bot.set_my_description(description=desc)
    except Exception:
        logger.exception("command_menu: set_my_description failed (non-fatal)")
    try:
        short = str(cfg.get("telegram.bot_short_description") or "").strip()
        if short:
            await bot.set_my_short_description(short_description=short)
    except Exception:
        logger.exception("command_menu: set_my_short_description failed (non-fatal)")
