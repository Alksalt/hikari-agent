#!/usr/bin/env python3
"""Out-of-process dead-man monitor for Hikari.

Runs every 5 min via com.hikari.deadman.plist. Lives outside the main process
so it survives if the bot wedges. Uses a SEPARATE Telegram bot token so the
nudge channel is independent of the bot's own messaging path."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

OWNER_ID = os.environ.get("OWNER_TELEGRAM_ID")
DEADMAN_TOKEN = os.environ.get("HIKARI_DEADMAN_BOT_TOKEN")
DB_PATH = Path(os.environ.get(
    "HIKARI_DB_PATH",
    str(Path.home() / "agents/hikari-agent/data/hikari.db"),
))
_DEFAULT_BACKUP_DIR = (
    Path.home()
    / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
    / "alt-wiki/projects/hikari-agent/backups"
)
BACKUP_DIR = Path(os.environ.get("HIKARI_BACKUP_DIR", str(_DEFAULT_BACKUP_DIR)))
MCP_EXTERNAL_URL = os.environ.get("HIKARI_MCP_EXTERNAL_URL", "http://127.0.0.1:8765/mcp")


def _launchctl_list_has(label: str) -> bool:
    try:
        out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    return label in out


def check_agent_running() -> bool:
    return _launchctl_list_has("com.hikari.agent")


def check_db_mtime_fresh() -> bool:
    if not DB_PATH.exists():
        return False
    age_sec = time.time() - DB_PATH.stat().st_mtime
    return age_sec < 30 * 60


def check_backup_fresh() -> bool:
    if not BACKUP_DIR.exists():
        return False
    backups = sorted(BACKUP_DIR.glob("hikari-*.tar.age"))
    if not backups:
        # Fall back to legacy plaintext backups
        backups = sorted(BACKUP_DIR.glob("hikari-*.db"))
    if not backups:
        return False
    newest = backups[-1]
    age_hr = (time.time() - newest.stat().st_mtime) / 3600
    return age_hr < 30


def check_mcp_external() -> bool:
    """A 401 means the server is up; only network errors mean it's down."""
    try:
        r = httpx.get(MCP_EXTERNAL_URL, timeout=10.0)
        return r.status_code in (200, 401, 405)
    except Exception:
        return False


def check_cloudflared_running() -> bool:
    return _launchctl_list_has("com.hikari.tunnel")


def post_alert(failed_checks: list[str]) -> None:
    if not DEADMAN_TOKEN or not OWNER_ID:
        print(
            f"deadman: no telegram token/owner configured; failed checks: {failed_checks}",
            file=sys.stderr,
        )
        return
    msg = "⚠ hikari dead-man: " + ", ".join(failed_checks)
    try:
        httpx.post(
            f"https://api.telegram.org/bot{DEADMAN_TOKEN}/sendMessage",
            json={"chat_id": OWNER_ID, "text": msg},
            timeout=10.0,
        )
    except Exception as e:
        print(f"deadman: telegram post failed: {e}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="Hikari dead-man monitor")
    p.add_argument("--dry-run", action="store_true",
                   help="print check results without sending Telegram alerts")
    args = p.parse_args()

    checks = [
        ("agent", check_agent_running),
        ("db_fresh", check_db_mtime_fresh),
        ("backup_fresh", check_backup_fresh),
        ("mcp_external", check_mcp_external),
        ("cloudflared", check_cloudflared_running),
    ]
    failed: list[str] = []
    for name, fn in checks:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            print(f"deadman: check {name} raised: {e}", file=sys.stderr)
        if not ok:
            failed.append(name)
        if args.dry_run:
            print(f"  {name}: {'OK' if ok else 'FAIL'}")

    if args.dry_run:
        print(f"dry-run summary: {len(failed)} failed: {failed}")
        return 0

    if failed:
        post_alert(failed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
