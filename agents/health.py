"""Sprint 6D — startup health probe.

Runs a cheap battery of health checks at boot, logs the full report at INFO,
and (per HIKARI_STARTUP_DIGEST env) optionally DMs the owner a one-line
voice-style digest if anything is degraded.

Checks:
  - db_integrity         (SQLite ``PRAGMA quick_check``)
  - scheduler_jobs       (APScheduler job count from bot_data)
  - mcp_warm_pool        (MANAGER.warm_servers() size)
  - oauth_google         (probe_google_token() — reused; cached if already run)
  - graph_outbox_pending (Sprint 5D outbox row count; degrades if > 50)
  - last_backup_age_h    (iCloud backup mtime; degrades if > 30h since)
  - log_recent_errors    (count of ERROR lines in data/logs/hikari.log in
                          the last hour; degrades if > 5)

The collector never raises — each check is wrapped and reports
``ok=False, reason="exception:<Type>"`` on failure. This module must not
block post_init even when every check fails.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import storage.db as db

logger = logging.getLogger(__name__)

# Thresholds — values above these flip ok=False without raising.
_OUTBOX_PENDING_WARN = 50
_BACKUP_AGE_WARN_HOURS = 30
_LOG_RECENT_ERRORS_WARN = 5
_MEDIA_OUTBOX_PENDING_WARN = 20

# Where the daily backup writes (scripts/backup.sh:15).
_BACKUP_DIR = Path.home() / (
    "Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki/"
    "projects/hikari-agent/backups"
)

# Rotating log path (telegram_bridge.py:2227).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH = _REPO_ROOT / "data" / "logs" / "hikari.log"

_ERROR_LINE_RE = re.compile(r"\bERROR\b")
_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    value: Any
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "value": self.value, "reason": self.reason}


def _check_db_integrity() -> CheckResult:
    try:
        with db._conn() as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
        verdict = row[0] if row else "no_result"
        return CheckResult(ok=verdict == "ok", value=verdict)
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


def _check_scheduler(scheduler: Any) -> CheckResult:
    if scheduler is None:
        return CheckResult(ok=False, value=0, reason="scheduler_not_in_bot_data")
    try:
        jobs = scheduler.get_jobs()
        return CheckResult(ok=len(jobs) > 0, value=len(jobs))
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


def _check_mcp_warm_pool() -> CheckResult:
    try:
        from agents.mcp_manager import MANAGER  # noqa: PLC0415
        warm = MANAGER.warm_servers()
        # Empty pool is fine on a fresh boot; we only flag if the import or
        # call fails. The pool fills lazily on first tool use.
        return CheckResult(ok=True, value=len(warm))
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


async def _check_oauth_google(prefetched: tuple[bool, str] | None = None) -> CheckResult:
    """OAuth check. If `prefetched=(healthy, reason)` is passed (e.g. from
    post_init's existing probe), reuse it instead of re-hitting Google."""
    try:
        if prefetched is not None:
            healthy, reason = prefetched
        else:
            from agents.google_health import probe_google_token  # noqa: PLC0415
            healthy, reason = await probe_google_token()
        return CheckResult(
            ok=healthy,
            value="ok" if healthy else "unhealthy",
            reason=reason or None,
        )
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


def _check_graph_outbox() -> CheckResult:
    try:
        pending = db.graph_outbox_pending(limit=_OUTBOX_PENDING_WARN + 1)
        count = len(pending)
        failed_stats = db.graph_outbox_failed_stats()
        failed_count = failed_stats.get("count", 0)
        last_error = failed_stats.get("last_error")
        reason = None
        if count > _OUTBOX_PENDING_WARN:
            reason = f"backlog>{_OUTBOX_PENDING_WARN}"
        if failed_count > 0:
            err_snippet = (last_error or "")[:80]
            reason = (f"{reason}; " if reason else "") + f"failed={failed_count} last_error={err_snippet!r}"
        return CheckResult(
            ok=count <= _OUTBOX_PENDING_WARN and failed_count == 0,
            value={"pending": count, "failed": failed_count},
            reason=reason,
        )
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


def _check_media_outbox() -> CheckResult:
    try:
        stats = db.media_outbox_stats()
        pending = stats.get("pending", 0)
        ok = pending <= _MEDIA_OUTBOX_PENDING_WARN
        return CheckResult(
            ok=ok,
            value=pending,
            reason=None if ok else f"backlog>{_MEDIA_OUTBOX_PENDING_WARN}",
        )
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


def _check_last_backup() -> CheckResult:
    try:
        if not _BACKUP_DIR.exists():
            return CheckResult(ok=False, value=None, reason="backup_dir_missing")
        latest_mtime: float | None = None
        patterns = ("hikari-*.tar.age", "hikari-*.db")
        for pattern in patterns:
            for p in _BACKUP_DIR.glob(pattern):
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if latest_mtime is None or mt > latest_mtime:
                    latest_mtime = mt
            if latest_mtime is not None:
                break  # found .tar.age — don't fall back to legacy
        if latest_mtime is None:
            return CheckResult(ok=False, value=None, reason="no_backups_found")
        age_hours = round((time.time() - latest_mtime) / 3600, 1)
        return CheckResult(
            ok=age_hours <= _BACKUP_AGE_WARN_HOURS,
            value=age_hours,
            reason=(
                None
                if age_hours <= _BACKUP_AGE_WARN_HOURS
                else f"stale>{_BACKUP_AGE_WARN_HOURS}h"
            ),
        )
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


def _check_recent_log_errors(log_path: Path = _LOG_PATH, window_sec: int = 3600) -> CheckResult:
    try:
        if not log_path.exists():
            # No log file yet is fine on first boot.
            return CheckResult(ok=True, value=0, reason="log_missing")
        cutoff = time.time() - window_sec
        # Read just the tail — read last ~256KB to keep this cheap.
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            if size > 256_000:
                f.seek(size - 256_000)
                # Drop the partial first line.
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
        count = 0
        for line in raw.splitlines():
            if not _ERROR_LINE_RE.search(line):
                continue
            m = _TIMESTAMP_RE.match(line)
            if not m:
                # No timestamp — count it conservatively (it's recent enough
                # to be in the tail anyway).
                count += 1
                continue
            try:
                # Both 'YYYY-MM-DD HH:MM:SS' and 'YYYY-MM-DDTHH:MM:SS' parse with this.
                import datetime as _dt  # noqa: PLC0415
                ts = _dt.datetime.fromisoformat(m.group(1).replace("T", " "))
                if ts.timestamp() >= cutoff:
                    count += 1
            except ValueError:
                count += 1
        return CheckResult(
            ok=count <= _LOG_RECENT_ERRORS_WARN,
            value=count,
            reason=(
                None
                if count <= _LOG_RECENT_ERRORS_WARN
                else f"errors>{_LOG_RECENT_ERRORS_WARN}/hr"
            ),
        )
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


async def collect_startup_report(
    scheduler: Any = None,
    oauth_google_prefetched: tuple[bool, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run every health check and return a dict of check_name → CheckResult dict.

    ``scheduler`` should be the APScheduler instance from ``app.bot_data['scheduler']``.
    Pass None if unavailable — that check will report degraded.

    ``oauth_google_prefetched`` lets the caller skip the second
    ``probe_google_token`` call when post_init has already run it once at boot
    (the canonical wiring path).
    """
    report: dict[str, dict[str, Any]] = {
        "db_integrity": _check_db_integrity().to_dict(),
        "scheduler_jobs": _check_scheduler(scheduler).to_dict(),
        "mcp_warm_pool": _check_mcp_warm_pool().to_dict(),
        "oauth_google": (await _check_oauth_google(oauth_google_prefetched)).to_dict(),
        "graph_outbox_pending": _check_graph_outbox().to_dict(),
        "media_outbox_pending": _check_media_outbox().to_dict(),
        "last_backup_age_h": _check_last_backup().to_dict(),
        "log_recent_errors": _check_recent_log_errors().to_dict(),
    }
    return report


def is_degraded(report: dict[str, dict[str, Any]]) -> bool:
    """True if any check returned ok=False."""
    return any(not check.get("ok", False) for check in report.values())


def format_startup_digest(report: dict[str, dict[str, Any]]) -> str:
    """Format a short voice-style digest (≤300 chars) for owner DM.

    Only lists degraded checks. If everything is OK, returns a single-line
    "all green" string.
    """
    bad = [
        (name, check)
        for name, check in report.items()
        if not check.get("ok", False)
    ]
    if not bad:
        return "startup: all green."
    parts = []
    for name, check in bad:
        reason = check.get("reason") or check.get("value") or "?"
        parts.append(f"{name}={reason}")
    body = "startup degraded: " + ", ".join(parts)
    if len(body) > 300:
        body = body[:297] + "…"
    return body


def should_send_digest(report: dict[str, dict[str, Any]], mode: str | None = None) -> bool:
    """Apply HIKARI_STARTUP_DIGEST gating. Default 'on_degrade'."""
    mode = (mode or os.environ.get("HIKARI_STARTUP_DIGEST") or "on_degrade").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    # on_degrade (default) and anything else
    return is_degraded(report)
