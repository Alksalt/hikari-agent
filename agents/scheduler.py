"""APScheduler setup for proactive heartbeat + daily reflection + episode consolidation."""

from __future__ import annotations

import logging
import zoneinfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config as cfg

logger = logging.getLogger(__name__)

_DEFAULT_MISFIRE_GRACE_SEC = cfg.get("scheduler.default_misfire_grace_sec") or 300


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

    # Heartbeat check: every 30 min, the function itself respects min/max interval + quiet hours.
    # APScheduler's iscoroutinefunction() doesn't recognize a lambda wrapping an async fn,
    # so we use an `async def` wrapper for every async job — otherwise the executor runs the
    # lambda in a thread pool, gets back an unawaited coroutine, Python warns and the work
    # never actually happens.
    async def _heartbeat_job(): return await maybe_send_heartbeat(send_text)
    scheduler.add_job(
        _heartbeat_job,
        IntervalTrigger(minutes=30),
        id="heartbeat",
        coalesce=True, max_instances=1, misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SEC,
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
        # APScheduler dispatches sync vs async via inspect.iscoroutinefunction.
        # A lambda wrapping an async fn is not detected as async -> the
        # executor calls it sync, gets back an un-awaited coroutine,
        # Python logs "coroutine ... was never awaited". Wrap with an
        # `async def` so the executor awaits it properly.
        async def _calendar_job():
            return await maybe_send_calendar_heartbeat(send_text)
        scheduler.add_job(
            _calendar_job,
            IntervalTrigger(minutes=calendar_interval),
            id="calendar_heartbeat",
            coalesce=True, max_instances=1, misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SEC,
        )
    else:
        logger.info(
            "calendar_heartbeat: skipped — runtime_state.calendar_heartbeat_healthy "
            "is not '1' (Google creds missing / not wired)."
        )

    # Re-engagement nudge: every 15 min, fires only when she had last word + user silent 2-6h
    async def _reengage_job(): return await maybe_send_reengagement(send_text)
    scheduler.add_job(
        _reengage_job,
        IntervalTrigger(minutes=15),
        id="reengage",
        coalesce=True, max_instances=1, misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SEC,
    )

    # Session consolidation: every 15 min
    scheduler.add_job(
        maybe_run_session_consolidation,
        IntervalTrigger(minutes=15),
        id="consolidation",
        coalesce=True, max_instances=1, misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SEC,
    )

    from .proactive import fire_due_reminders
    reminder_poll = int(cfg.get("reminders.poll_interval_sec", 60))
    async def _fire_reminders_job(): return await fire_due_reminders(send_text)
    scheduler.add_job(
        _fire_reminders_job,
        IntervalTrigger(seconds=reminder_poll),
        id="reminders_fire",
        coalesce=True, max_instances=1, misfire_grace_time=120,
    )

    import sys
    if sys.platform == "darwin":
        from .proactive import sync_pending_apple_reminders
        apple_interval = int(cfg.get("reminders.apple_sync_interval_sec", 300))
        async def _apple_sync_job():
            return await sync_pending_apple_reminders()
        scheduler.add_job(
            _apple_sync_job,
            IntervalTrigger(seconds=apple_interval),
            id="reminders_apple_sync",
            coalesce=True, max_instances=1, misfire_grace_time=600,
        )

    from .proactive import sync_pending_gcal_reminders
    gcal_interval = int(cfg.get("reminders.gcal_sync_interval_sec", 300))
    if _calendar_creds_healthy():
        async def _gcal_sync_job():
            return await sync_pending_gcal_reminders()
        scheduler.add_job(
            _gcal_sync_job,
            IntervalTrigger(seconds=gcal_interval),
            id="reminders_gcal_sync",
            coalesce=True, max_instances=1, misfire_grace_time=600,
        )
    else:
        logger.info(
            "reminders_gcal_sync: skipped — calendar creds unhealthy. "
            "pending gcal mirrors will accumulate; new reminders still fire "
            "locally via the reminders_fire job."
        )

    # Daily reflection: 09:00 local (use OS-local TZ via cron trigger without tz)
    scheduler.add_job(
        run_daily_reflection,
        CronTrigger(hour=9, minute=0),
        id="daily_reflection",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    if bool(cfg.get("morning_brief.enabled", True)):
        from .morning_brief import maybe_send_morning_brief
        mb_hour = int(cfg.get("morning_brief.hour", 6))
        mb_minute = int(cfg.get("morning_brief.minute", 0))
        async def _morning_brief_job(): return await maybe_send_morning_brief(send_text)
        scheduler.add_job(
            _morning_brief_job,
            CronTrigger(hour=mb_hour, minute=mb_minute),
            id="morning_brief",
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

    if bool(cfg.get("daily_checkin.enabled", True)):
        from .daily_checkin import maybe_run_daily_checkin
        poll = int(cfg.get("daily_checkin.poll_interval_minutes", 5))
        async def _daily_checkin_job():
            return await maybe_run_daily_checkin(send_text)
        scheduler.add_job(
            _daily_checkin_job,
            IntervalTrigger(minutes=poll),
            id="daily_checkin",
            coalesce=True, max_instances=1, misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SEC,
        )

    # Daily evening diary: 22:00 local. Composes a private diary entry from
    # the day's receipts, fired reminders, today's episodes, and active facts
    # into data/diary/YYYY-MM-DD.md + an episode summary. Letta-style
    # diary-writing to reduce persona drift.
    if bool(cfg.get("evening_diary.enabled", True)):
        from .evening_diary import run_evening_diary
        ed_hour = int(cfg.get("evening_diary.hour", 22))
        ed_minute = int(cfg.get("evening_diary.minute", 0))
        async def _evening_diary_job(): return await run_evening_diary()
        scheduler.add_job(
            _evening_diary_job,
            CronTrigger(hour=ed_hour, minute=ed_minute),
            id="evening_diary",
            coalesce=True, max_instances=1, misfire_grace_time=3600,
        )

    # Drift canary: weekly Sunday 20:00 local. Probes one of three hard
    # opinions, LLM-as-judge classifies the answer, alerts via send_text on
    # 'drift' verdict. Single-user Nautilus Compass.
    if bool(cfg.get("drift_canary.enabled", True)):
        from .drift_canary import run_drift_canary
        async def _drift_canary_job(): return await run_drift_canary(send_text)
        scheduler.add_job(
            _drift_canary_job,
            CronTrigger(day_of_week="sun", hour=20, minute=0),
            id="drift_canary",
            coalesce=True, max_instances=1, misfire_grace_time=3600,
        )

    # Phase 11: weekly sleep-time consolidation, Sunday 04:30 local.
    # Letta sleep-time pattern (Apr 2025) — synthesizes a 200-word weekly
    # "what i noticed about him" summary into core_blocks['weekly_consolidation'],
    # archives the prior week's snapshot. Scheduled at 04:30 to sit after the
    # memory_prune (1st-of-month 04:00) so the two never compete for the DB
    # on the rare overlap, and well clear of the 09:00 daily_reflection.
    async def _weekly_consolidation_job():
        from .reflection import run_weekly_consolidation
        return await run_weekly_consolidation()
    scheduler.add_job(
        _weekly_consolidation_job,
        CronTrigger(day_of_week="sun", hour=4, minute=30),
        id="weekly_consolidation",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    return scheduler


def _calendar_creds_healthy() -> bool:
    """Phase 8 (Phase 10 update): cheap startup gate. If the bridge wrote
    ``runtime_state.calendar_heartbeat_healthy = '1'`` after a successful
    probe call, the job runs; otherwise it sits out.

    Default: if all three OAuth env vars for google-workspace-mcp are set,
    treat as healthy unless explicitly disabled. Bridge probes can override.
    """
    import os

    from storage import db

    explicit = db.runtime_get("calendar_heartbeat_healthy")
    if explicit is not None:
        return str(explicit).strip() == "1"
    # Fallback: presence of the OAuth env var trio (Phase 10 — was
    # GOOGLE_SERVICE_ACCOUNT_JSON, but the upstream package uses OAuth
    # user creds, not service-account JSON).
    return all(os.environ.get(k) for k in (
        "GOOGLE_WORKSPACE_CLIENT_ID",
        "GOOGLE_WORKSPACE_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
    ))


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
