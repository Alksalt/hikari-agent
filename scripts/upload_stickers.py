"""Operator checklist for uploading Hikari's sticker pack to Telegram.

This script does NOT upload anything itself — file_id harvesting happens
through ``scripts/grab_stickers.py`` (because each sticker must be sent FROM
your Telegram client to harvest a ``file_id``).

What this script does:
  - Reads ``assets/stickers/hikari_telegram_pack/manifest.json``.
  - Prints a numbered checklist with the exact stickers to send, one by one,
    so you can work through the pack without losing your place.

When ``grab_stickers.py`` finishes, it prints a YAML block of file_ids that
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
    print("1. Stop the live bridge: launchctl stop com.hikari.agent")
    print("2. Run: uv run python scripts/grab_stickers.py")
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
    print("4. When done, type 'done' in chat (or Ctrl-C the script).")
    print("5. The script prints a YAML block with `file_id:` + `description:` fields.")
    print("   Paste it into config/engagement.yaml under stickers.pool.")
    print("   FILL IN each description — the situational LLM picker uses description")
    print("   text to choose contextually. Empty descriptions degrade to random.")
    print("6. Restart the bridge: launchctl start com.hikari.agent")
    print()
    print("Note: the pool stays empty until you complete this flow. That's fine —")
    print("an empty pool is the disabled state. Image-gen-down fallback will log")
    print("a warning and skip the sticker until the pool is populated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
