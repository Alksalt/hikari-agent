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

# Guard: external MCP + Cloudflare checks only run when this env var is set.
_HAS_MCP_EXTERNAL = os.environ.get("HIKARI_HAS_MCP_EXTERNAL", "0") == "1"

# Cookie table used by the main process to signal it is alive (db write tracker).
_COOKIE_TABLE = "deadman_cookie"

# State file persisting the Telegram probe failure streak for the 3-strike debounce.
_STRIKE_FILE = Path(os.environ.get(
    "HIKARI_DEADMAN_STRIKE_FILE",
    str(Path.home() / ".config/hikari/deadman_strikes.txt"),
))


def _launchctl_pid_and_exit(label: str) -> tuple[int | None, int | None]:
    """Return (pid, last_exit_code) from `launchctl print system/<label>`.

    Returns (None, None) on any error or when the service is not found.
    Uses `launchctl print` rather than `launchctl list` for reliable PID/exit-code parsing.
    """
    try:
        res = subprocess.run(
            ["launchctl", "print", f"system/{label}"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        # Fall back to the gui domain (user-level services).
        try:
            uid = os.getuid()
            res = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return None, None

    if res.returncode != 0:
        # Try gui domain before giving up.
        try:
            uid = os.getuid()
            res2 = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True, text=True, timeout=5,
            )
            if res2.returncode == 0:
                res = res2
            else:
                return None, None
        except Exception:
            return None, None

    pid: int | None = None
    exit_code: int | None = None
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid ="):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("last exit code ="):
            try:
                exit_code = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    return pid, exit_code


def check_agent_running() -> bool:
    pid, _ = _launchctl_pid_and_exit("com.hikari.agent")
    return pid is not None and pid > 0


def check_db_mtime_fresh() -> bool:
    """Check that the DB has a recent write via the cookie row the main process updates."""
    if not DB_PATH.exists():
        return False
    # Primary: check the cookie row mtime (bounded to writes-this-process).
    try:
        import sqlite3
        with sqlite3.connect(str(DB_PATH), timeout=3) as conn:
            row = conn.execute(
                f"SELECT ts FROM {_COOKIE_TABLE} WHERE key='heartbeat' LIMIT 1"
            ).fetchone()
        if row:
            import datetime
            ts_str = row[0]
            ts = datetime.datetime.fromisoformat(ts_str).timestamp()
            return (time.time() - ts) < 30 * 60
    except Exception:
        pass
    # Fallback: file mtime (less precise — includes reads/vacuums).
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
    if not _HAS_MCP_EXTERNAL:
        return True
    try:
        r = httpx.get(MCP_EXTERNAL_URL, timeout=10.0)
        return r.status_code in (200, 401, 405)
    except Exception:
        return False


def check_cloudflared_running() -> bool:
    if not _HAS_MCP_EXTERNAL:
        return True
    pid, _ = _launchctl_pid_and_exit("com.hikari.tunnel")
    return pid is not None and pid > 0


def _telegram_probe_ok() -> bool:
    """HEAD request to api.telegram.org with a 3-strike debounce.

    Returns True if Telegram is reachable, or if the streak is below 3 consecutive
    failures (avoids spurious alerts on transient network blips).
    """
    try:
        r = httpx.head("https://api.telegram.org", timeout=10.0)
        reachable = r.status_code < 500
    except Exception:
        reachable = False

    if reachable:
        # Reset strike counter on success.
        try:
            _STRIKE_FILE.write_text("0")
        except Exception:
            pass
        return True

    # Increment strike counter.
    try:
        _STRIKE_FILE.parent.mkdir(parents=True, exist_ok=True)
        current = int(_STRIKE_FILE.read_text().strip()) if _STRIKE_FILE.exists() else 0
        current += 1
        _STRIKE_FILE.write_text(str(current))
        if current < 3:
            # Suppress until 3 consecutive failures.
            return True
    except Exception:
        pass
    return False


def restart_agent() -> bool:
    """Kickstart the launchd agent service. Returns True if the command ran.

    The dead-man's whole reason to exist is recovery, not just alerting. When
    the agent process is gone we relaunch it ourselves before nudging the owner,
    so a wedged/stopped bot self-heals within one 5-minute tick even if launchd's
    own KeepAlive somehow didn't fire (e.g. the job was bootout'd, disabled, or
    the host woke from sleep mid-relaunch).
    """
    uid = os.getuid()
    try:
        res = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/com.hikari.agent"],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode != 0:
            print(
                f"deadman: kickstart failed (rc={res.returncode}): "
                f"{res.stderr.strip()}",
                file=sys.stderr,
            )
        return res.returncode == 0
    except Exception as e:
        print(f"deadman: kickstart raised: {e}", file=sys.stderr)
        return False


def post_alert(failed_checks: list[str]) -> None:
    if not DEADMAN_TOKEN or not OWNER_ID:
        print(
            f"deadman: no telegram token/owner configured; failed checks: {failed_checks}",
            file=sys.stderr,
        )
        return
    if not _telegram_probe_ok():
        print(
            f"deadman: telegram unreachable (3-strike debounce); "
            f"failed checks: {failed_checks}",
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
        if "agent" in failed:
            restart_ok = restart_agent()
            rc = 0 if restart_ok else 1
            failed = [
                f"agent down — kickstart rc={rc}" if name == "agent" else name
                for name in failed
            ]
        post_alert(failed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
