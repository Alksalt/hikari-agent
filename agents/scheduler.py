"""APScheduler setup for proactive heartbeat + daily reflection + episode consolidation."""

from __future__ import annotations

import logging
import zoneinfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config as cfg

logger = logging.getLogger(__name__)


def build_scheduler(send_text) -> AsyncIOScheduler:
    """Wire up the background jobs. send_text is `async def send_text(s: str)`."""
    from .proactive import (
        maybe_send_calendar_heartbeat,
        maybe_send_heartbeat,
        maybe_send_reengagement,
    )
    from .reflection import maybe_run_session_consolidation, run_daily_reflection

    tz_name = cfg.get("scheduler.timezone", "UTC")
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.warning("scheduler: invalid timezone %r, falling back to UTC", tz_name)
        tz = zoneinfo.ZoneInfo("UTC")
    scheduler = AsyncIOScheduler(timezone=tz)

    # Heartbeat check: every 30 min, the function itself respects min/max interval + quiet hours
    scheduler.add_job(
        lambda: maybe_send_heartbeat(send_text),
        IntervalTrigger(minutes=30),
        id="heartbeat",
        coalesce=True, max_instances=1, misfire_grace_time=300,
    )

    # Calendar-aware heartbeat: polls calendar via the drive_gmail subagent and
    # fires one prep message when an event falls in the lead-window jitter band.
    # Phase 8: a startup health flag in runtime_state lets us skip the job when
    # Google credentials are missing — saves an LLM round-trip every interval
    # against a guaranteed-failing path.
    if _calendar_creds_healthy():
        calendar_interval = int(
            cfg.get("calendar_heartbeat.scheduler_interval_minutes", 5)
        )
        scheduler.add_job(
            lambda: maybe_send_calendar_heartbeat(send_text),
            IntervalTrigger(minutes=calendar_interval),
            id="calendar_heartbeat",
            coalesce=True, max_instances=1, misfire_grace_time=300,
        )
    else:
        logger.info(
            "calendar_heartbeat: skipped — runtime_state.calendar_heartbeat_healthy "
            "is not '1' (Google creds missing / not wired)."
        )

    # Re-engagement nudge: every 15 min, fires only when she had last word + user silent 2-6h
    scheduler.add_job(
        lambda: maybe_send_reengagement(send_text),
        IntervalTrigger(minutes=15),
        id="reengage",
        coalesce=True, max_instances=1, misfire_grace_time=300,
    )

    # Session consolidation: every 15 min
    scheduler.add_job(
        maybe_run_session_consolidation,
        IntervalTrigger(minutes=15),
        id="consolidation",
        coalesce=True, max_instances=1, misfire_grace_time=300,
    )

    from .proactive import fire_due_reminders
    reminder_poll = int(cfg.get("reminders.poll_interval_sec", 60))
    scheduler.add_job(
        lambda: fire_due_reminders(send_text),
        IntervalTrigger(seconds=reminder_poll),
        id="reminders_fire",
        coalesce=True, max_instances=1, misfire_grace_time=120,
    )

    from .proactive import sync_pending_gcal_reminders
    gcal_interval = int(cfg.get("reminders.gcal_sync_interval_sec", 300))
    if _calendar_creds_healthy():
        scheduler.add_job(
            sync_pending_gcal_reminders,
            IntervalTrigger(seconds=gcal_interval),
            id="reminders_gcal_sync",
            coalesce=True, max_instances=1, misfire_grace_time=600,
        )

    # Daily reflection: 09:00 local (use OS-local TZ via cron trigger without tz)
    scheduler.add_job(
        run_daily_reflection,
        CronTrigger(hour=9, minute=0),
        id="daily_reflection",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    # Phase 8: monthly memory prune. Episodes older than the configured
    # retention window get dropped (their embeddings + FTS rows too). Runs
    # at 04:00 on the 1st of each month. Backup launchd (03:00 daily) has
    # already mirrored hikari.db to iCloud by then, so recovery is possible
    # if a prune ever surprises us.
    retention_days = int(cfg.get("memory.episodes_retention_days", 180))
    scheduler.add_job(
        lambda: _run_memory_prune(retention_days),
        CronTrigger(day=1, hour=4, minute=0),
        id="memory_prune",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    return scheduler


def _calendar_creds_healthy() -> bool:
    """Phase 8: cheap startup gate. If the bridge wrote
    ``runtime_state.calendar_heartbeat_healthy = '1'`` after a successful
    probe call, the job runs; otherwise it sits out.

    Default: if the env var ``GOOGLE_SERVICE_ACCOUNT_JSON`` is set, treat as
    healthy unless explicitly disabled. Bridge probes can override.
    """
    import os
    from storage import db

    explicit = db.runtime_get("calendar_heartbeat_healthy")
    if explicit is not None:
        return str(explicit).strip() == "1"
    # Fallback: presence of the service-account env var.
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))


def _run_memory_prune(retention_days: int) -> None:
    """Wrapper that calls db.prune_episodes_older_than_days and logs the count.
    Kept as a module-level helper (not a lambda) so tests can monkeypatch it."""
    from storage import db

    try:
        pruned = db.prune_episodes_older_than_days(retention_days)
        logger.info(
            "memory_prune: removed %d episodes older than %d days",
            pruned, retention_days,
        )
    except Exception:
        logger.exception("memory_prune: prune failed")
