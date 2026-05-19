"""APScheduler setup for proactive heartbeat + daily reflection + episode consolidation."""

from __future__ import annotations

import logging

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

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Heartbeat check: every 30 min, the function itself respects min/max interval + quiet hours
    scheduler.add_job(
        lambda: maybe_send_heartbeat(send_text),
        IntervalTrigger(minutes=30),
        id="heartbeat",
        coalesce=True, max_instances=1, misfire_grace_time=300,
    )

    # Calendar-aware heartbeat: polls calendar via the drive_gmail subagent and
    # fires one prep message when an event falls in the lead-window jitter band.
    calendar_interval = int(
        cfg.get("calendar_heartbeat.scheduler_interval_minutes", 5)
    )
    scheduler.add_job(
        lambda: maybe_send_calendar_heartbeat(send_text),
        IntervalTrigger(minutes=calendar_interval),
        id="calendar_heartbeat",
        coalesce=True, max_instances=1, misfire_grace_time=300,
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

    # Daily reflection: 09:00 local (use OS-local TZ via cron trigger without tz)
    scheduler.add_job(
        run_daily_reflection,
        CronTrigger(hour=9, minute=0),
        id="daily_reflection",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    return scheduler
