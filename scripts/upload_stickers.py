"""Operator checklist for uploading Hikari's sticker pack to Telegram.

This script does NOT upload anything itself — Telegram sticker uploads happen
through the live bot's ``/grab_stickers`` interactive flow (because each
sticker must be sent FROM your Telegram client to harvest a ``file_id``).

What this script does:
  - Reads ``assets/stickers/hikari_telegram_pack/manifest.json``.
  - Prints a numbered checklist with the exact stickers to send, one by one,
    so you can work through the pack without losing your place.

After ``/grab_stickers stop``, the bot prints a YAML block of file_ids that
you paste into ``config/engagement.yaml`` under ``stickers.pool``. The pool
ships empty by default — an empty pool IS the disabled state, so nothing
breaks if you skip this; you just get no stickers.

Usage:
    uv run python scripts/upload_stickers.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "assets" / "stickers" / "hikari_telegram_pack" / "manifest.json"


def main() -> int:
    if not MANIFEST.exists():
        print(f"manifest not found: {MANIFEST}")
        return 1

    try:
        entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"manifest is not valid JSON: {exc}")
        return 1

    print("Sticker upload checklist (operator):")
    print()
    print("1. Make sure the bot is running and your Telegram channel is paired.")
    print("2. In Telegram, send to the bot: /grab_stickers start")
    print(f"3. Send each of these {len(entries)} stickers one by one")
    print("   (drag the .webp from the path below into Telegram, send as a sticker):")
    print()
    for entry in entries:
        idx = entry.get("index", "?")
        slug = entry.get("slug", "?")
        caption = entry.get("caption", "")
        webp = entry.get("webp", "")
        # Two-line block per sticker: human label + filesystem path.
        print(f"   [{idx:>2}] {slug}  —  {caption!r}")
        print(f"        {webp}")
    print()
    print("4. When done, send: /grab_stickers stop")
    print("5. The bot will print a YAML block of file_ids — paste it into")
    print("   config/engagement.yaml under stickers.pool (replacing the empty list).")
    print()
    print("Note: the pool stays empty until you complete this flow. That's fine —")
    print("an empty pool is the disabled state. Image-gen-down fallback will log")
    print("a warning and skip the sticker until the pool is populated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
