"""Sprint 6D — startup health probe.

Runs a cheap battery of health checks at boot, logs the full report at INFO,
and (per HIKARI_STARTUP_DIGEST env) optionally DMs the owner a one-line
voice-style digest if anything is degraded.

Checks:
  - db_integrity         (SQLite ``PRAGMA quick_check``)
  - scheduler_jobs       (APScheduler job count from bot_data)
  - mcp_warm_pool        (MANAGER.warm_servers() size)
  - oauth_google         (probe_google_token() — reused; cached if already run)
  - graphiti_reachable   (lightweight graph read to confirm Kuzu+graphiti OK)
  - graph_outbox_pending (outbox stats via graph_outbox_stats(); degrades if > 10)
  - media_outbox_pending (media outbox pending count; degrades if > 10)
  - last_backup_age_h    (iCloud backup mtime; degrades if > 30h since)
  - log_recent_errors    (count of ERROR lines in data/logs/hikari.log in
                          the last hour; degrades if > 10)

The collector never raises — each check is wrapped and reports
``ok=False, reason="exception:<Type>"`` on failure. This module must not
block post_init even when every check fails.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import storage.db as db
from agents import config as _cfg

logger = logging.getLogger(__name__)

# Thresholds — values above these flip ok=False without raising.
_OUTBOX_PENDING_WARN = 10
_OUTBOX_FAILED_WARN = 5
_BACKUP_AGE_WARN_HOURS = 30
_LOG_RECENT_ERRORS_WARN = 10
_MEDIA_OUTBOX_PENDING_WARN = 10
_GRAPH_RECALL_HIT_WARN = 0.5  # hit/(hit+fallback) must be >= this; 1.0 when no lookups yet

# Where the daily backup writes (scripts/backup.sh:15).
# Override via HIKARI_BACKUP_DIR env or backups.dir config key.
_BACKUP_DIR = Path(
    os.environ.get("HIKARI_BACKUP_DIR")
    or str(_cfg.get("backups.dir") or "")
    or str(
        Path.home()
        / "Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki/"
          "projects/hikari-agent/backups"
    )
)

# Rotating log path (telegram_bridge.py:2227).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH = _REPO_ROOT / "data" / "logs" / "hikari.log"

_ERROR_LINE_RE = re.compile(r"\b(ERROR|CRITICAL)\b")
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


async def _check_google_scopes() -> CheckResult:
    """Granted Google scopes cover every Google tool's required scopes? A gap
    here is why a delete/bulk op 403s mid-conversation — surface it at boot."""
    try:
        from agents.google_health import probe_google_scopes  # noqa: PLC0415
        status, missing = await probe_google_scopes()
        if status == "under_scoped":
            scopes = " ".join(missing)
            return CheckResult(
                ok=False,
                value="under_scoped",
                reason=(f"missing {scopes} — run: "
                        f"uv run python -m scripts.auth google grant --add {scopes}"),
            )
        # ok / unknown → not degraded (unknown = indeterminate probe, no alarm).
        return CheckResult(ok=True, value=status)
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


async def _check_google_account() -> CheckResult:
    """Refresh-token account matches the expected email? Catches the silent
    wrong-account class (olealt25 vs altukaleksandr2020)."""
    try:
        from agents.google_health import probe_google_account  # noqa: PLC0415
        status, detail = await probe_google_account()
        if status == "mismatch":
            return CheckResult(ok=False, value="mismatch", reason=detail)
        return CheckResult(ok=True, value=status)
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


async def _check_graphiti_reachable() -> CheckResult:
    """Lightweight canary: initialise the graph singleton and run a 1-result search."""
    try:
        from storage import graph as _graph  # noqa: PLC0415
        g = await asyncio.wait_for(_graph.get_graph(), timeout=10)
        await asyncio.wait_for(g.search("test", num_results=1), timeout=10)
        return CheckResult(ok=True, value="ok")
    except TimeoutError:
        return CheckResult(ok=False, value="unreachable", reason="timeout")
    except Exception as e:
        return CheckResult(ok=False, value="unreachable", reason=f"exception:{type(e).__name__}")


def _check_graph_outbox() -> CheckResult:
    try:
        stats = db.graph_outbox_stats()
        pending = stats["pending"]
        # drained rows are manual-drain; excluded from failed-count math.
        failed_real = stats["failed"]
        reason = None
        if pending > _OUTBOX_PENDING_WARN:
            reason = f"backlog>{_OUTBOX_PENDING_WARN}"
        if failed_real > _OUTBOX_FAILED_WARN:
            reason = (f"{reason}; " if reason else "") + f"failed={failed_real}"
        return CheckResult(
            ok=pending <= _OUTBOX_PENDING_WARN and failed_real <= _OUTBOX_FAILED_WARN,
            value={"pending": pending, "failed": failed_real},
            reason=reason,
        )
    except Exception as e:
        return CheckResult(ok=False, value=None, reason=f"exception:{type(e).__name__}")


def _check_media_outbox() -> CheckResult:
    try:
        counts = db.status_counts()
        pending = counts.get("media_outbox", {}).get("pending", 0)
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
    import datetime as _dt  # noqa: PLC0415
    try:
        if not log_path.exists():
            # No log file yet is fine on first boot.
            return CheckResult(ok=True, value=0, reason="log_missing")
        cutoff = time.time() - window_sec
        # Read just the tail — last ~256KB keeps this cheap.
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
                # No parseable timestamp — skip rather than count conservatively;
                # these are continuation lines or non-standard formatters that
                # cannot be placed in the window.
                continue
            try:
                # Both 'YYYY-MM-DD HH:MM:SS' and 'YYYY-MM-DDTHH:MM:SS' are accepted.
                # Contract #4: log timestamps are UTC because telegram_bridge.main()
                # sets logging.Formatter.converter = time.gmtime (Sprint 3 Phase 3B).
                # We attach UTC here so .timestamp() math is host-TZ-independent.
                ts = _dt.datetime.fromisoformat(
                    m.group(1).replace("T", " ")
                ).replace(tzinfo=_dt.UTC)
                if ts.timestamp() >= cutoff:
                    count += 1
            except ValueError:
                # Unparseable — skip; don't inflate error counts with bad data.
                continue
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


def _check_graph_recall() -> CheckResult:
    """Check graph-recall counters written by Phase 3A (runtime_state keys).

    Reads ``recall_graph_hit``, ``recall_graph_fallback``, and
    ``graph_search_error`` from runtime_state (all default to 0 if absent).
    hit_ratio = hit / (hit + fallback); defaults to 1.0 when both are zero
    (no lookups have happened yet — healthy by definition).
    ok = error == 0 and hit_ratio >= _GRAPH_RECALL_HIT_WARN.
    """
    try:
        hit = db.runtime_get_int("recall_graph_hit", 0)
        fallback = db.runtime_get_int("recall_graph_fallback", 0)
        err = db.runtime_get_int("graph_search_error", 0)
        total = hit + fallback
        ratio = hit / total if total > 0 else 1.0
        ok = err == 0 and ratio >= _GRAPH_RECALL_HIT_WARN
        reason = None
        if err > 0:
            reason = f"graph_search_error={err}"
        if ratio < _GRAPH_RECALL_HIT_WARN:
            reason = (f"{reason}; " if reason else "") + f"hit_ratio={round(ratio, 3)}<{_GRAPH_RECALL_HIT_WARN}"
        return CheckResult(
            ok=ok,
            value={"hit": hit, "fallback": fallback, "error": err, "hit_ratio": round(ratio, 3)},
            reason=reason,
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
        "google_scopes": (await _check_google_scopes()).to_dict(),
        "google_account": (await _check_google_account()).to_dict(),
        "graphiti_reachable": (await _check_graphiti_reachable()).to_dict(),
        "graph_outbox_pending": _check_graph_outbox().to_dict(),
        "media_outbox_pending": _check_media_outbox().to_dict(),
        "last_backup_age_h": _check_last_backup().to_dict(),
        "log_recent_errors": _check_recent_log_errors().to_dict(),
        "graph_recall": _check_graph_recall().to_dict(),
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


def chat_worthy_failures(report: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Failed checks the OWNER can act on — the only ones worth a chat ping.

    Everything else still lands in the log via format_startup_digest.
    """
    from agents import config as cfg  # noqa: PLC0415
    chat_checks = set(cfg.get("health.startup_digest_chat_checks",
                              ["oauth_google", "google_scopes", "google_account"]) or [])
    return {
        name: check for name, check in report.items()
        if name in chat_checks and not check.get("ok", False)
    }


def should_send_digest(report: dict[str, dict[str, Any]], mode: str | None = None) -> bool:
    """Apply HIKARI_STARTUP_DIGEST gating. Default 'on_degrade'."""
    mode = (mode or os.environ.get("HIKARI_STARTUP_DIGEST") or "on_degrade").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    # on_degrade (default) and anything else
    return is_degraded(report)
