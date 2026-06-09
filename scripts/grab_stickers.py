"""One-shot sticker file_id harvester (moved out of the Telegram bridge).

Phase 5b removed the /grab_stickers command — the bridge has zero
slash-commands. Harvesting Telegram ``file_id``s for the sticker pool is a
one-time setup task, so it lives here as a standalone script now.

Usage:
  1. STOP the live bridge first (one getUpdates poller per token, or both
     get HTTP 409)::

         launchctl stop com.hikari.agent

  2. Run the script::

         uv run python scripts/grab_stickers.py

  3. From the owner Telegram account, send (or forward) stickers to the
     bot. Each new file_id is captured and acknowledged in chat.

  4. Press Ctrl-C (or send the text ``done`` in chat) to finish. The
     script prints a YAML snippet to stdout — paste it into
     ``config/engagement.yaml`` replacing the existing ``stickers.pool:``
     block, and FILL IN the descriptions: situational sticker selection
     depends on the description text. A flat pool with empty descriptions
     degrades the LLM picker to random choice.

  5. Restart the bridge::

         launchctl start com.hikari.agent

Reads ``TELEGRAM_BOT_TOKEN`` and ``OWNER_TELEGRAM_ID`` from the
environment / ``.env`` (same vars as the bridge).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)


def add_to_pool(pool: list[str], file_id: str) -> bool:
    """Append ``file_id`` to ``pool`` if new. Returns True when added."""
    if file_id in pool:
        return False
    pool.append(file_id)
    return True


def yaml_snippet(pool: list[str]) -> str:
    """Render the captured pool as a ``stickers.pool`` YAML block.

    Emits dict format so descriptions can be filled in — situational
    selection depends on the description text. Telegram file_ids today are
    alphanumeric + ``_`` + ``-``, but double quotes and backslashes are
    escaped defensively in case a future source emits anything weirder.
    """
    lines = ["stickers:", "  pool:"]
    for fid in pool:
        fid_safe = str(fid).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    - file_id: "{fid_safe}"')
        lines.append('      description: ""  # fill in or LLM picks at random')
    return "\n".join(lines)


def _print_result(pool: list[str]) -> None:
    if not pool:
        print("\ncaptured nothing. send stickers to the bot while this runs.")
        return
    print(
        f"\ncaptured {len(pool)} sticker(s). paste this into "
        f"config/engagement.yaml (replace the existing `stickers.pool:`). "
        f"FILL IN the descriptions or situational selection won't work:\n"
    )
    print(yaml_snippet(pool))


async def _run(token: str, owner_id: int, pool: list[str]) -> None:
    """Poll for stickers, appending captured file_ids to ``pool`` in place.

    ``pool`` is caller-owned so a Ctrl-C that unwinds asyncio.run() doesn't
    lose what was already captured.
    """
    from telegram import Update
    from telegram.ext import (
        Application,
        ContextTypes,
        MessageHandler,
        filters,
    )

    done = asyncio.Event()

    async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        user = update.effective_user
        if not user or not message or not message.sticker:
            return
        if user.id != owner_id:
            return
        if add_to_pool(pool, message.sticker.file_id):
            await message.reply_text(f"captured ({len(pool)}). send more or type done.")
        else:
            await message.reply_text(f"already have that one ({len(pool)} total).")

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        user = update.effective_user
        if not user or not message or not message.text:
            return
        if user.id != owner_id:
            return
        if message.text.strip().lower() in ("done", "stop", "finish"):
            await message.reply_text(f"done. {len(pool)} captured — check the terminal.")
            done.set()

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
    app.add_handler(MessageHandler(filters.TEXT, on_text))

    print("listening — send stickers to the bot now. Ctrl-C or 'done' in chat to finish.")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            await done.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await app.updater.stop()
            await app.stop()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_raw = os.environ.get("OWNER_TELEGRAM_ID")
    if not token or not owner_raw:
        print("TELEGRAM_BOT_TOKEN and OWNER_TELEGRAM_ID must be set (env or .env).")
        return 1
    try:
        owner_id = int(owner_raw)
    except ValueError:
        print(f"OWNER_TELEGRAM_ID must be an integer, got {owner_raw!r}.")
        return 1

    pool: list[str] = []
    try:
        asyncio.run(_run(token, owner_id, pool))
    except KeyboardInterrupt:
        pass
    _print_result(pool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
