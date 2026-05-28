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

# time_texture phase boundaries: (start_hour_inclusive, end_hour_exclusive, phase_name)
# 24h clock; 22-02 and 02-04 wrap midnight and are handled by the lookup function.
_TIME_TEXTURE_PHASES = (
    (4,  7,  "early_morning"),
    (7,  11, "morning"),
    (11, 14, "midday"),
    (14, 18, "afternoon"),
    (18, 22, "evening"),
    (22, 26, "late_night"),   # 26 == next-day 02:00 (virtual)
    (26, 28, "deep_night"),   # 26-28 == 02:00-04:00 (virtual)
)


def _hour_to_time_texture(hour: int) -> str:
    """Return the time_texture phase name for a given 0-23 hour."""
    # Normalise: midnight-wrap hours use virtual 24+ representation.
    virtual = hour if hour >= 4 else hour + 24
    for start, end, phase in _TIME_TEXTURE_PHASES:
        if start <= virtual < end:
            return phase
    return "late_night"  # fallback (shouldn't be reached)


def _add_graph_outbox_drain_job(scheduler: AsyncIOScheduler) -> None:
    """Idempotently register the graph_outbox_drain job on the given scheduler."""
    if scheduler.get_job("graph_outbox_drain") is not None:
        return
    from storage.graph import process_outbox

    async def _graph_outbox_drain_job():
        try:
            stats = await process_outbox(limit=50, max_per_call=10)
            if stats.get("sent") or stats.get("failed"):
                logger.info("graph_outbox_drain: %s", stats)
        except Exception:
            logger.exception("graph_outbox_drain: unexpected failure")

    scheduler.add_job(
        _graph_outbox_drain_job,
        IntervalTrigger(seconds=30),
        id="graph_outbox_drain",
        coalesce=True, max_instances=1, misfire_grace_time=60,
    )


def _pick_silent_day_this_week() -> None:
    """Sunday 18:00 picker: choose one weekday for the coming week.

    Writes the chosen date as an ISO string to runtime_state key
    ``silent_day_this_week``.  When the feature is disabled the key is
    cleared so a stale value never gates proactive sends indefinitely.
    """
    import random
    from datetime import date, timedelta

    from storage import db

    enabled = bool(cfg.get("engagement.weekly_silent_day_enabled", True))
    if not enabled:
        db.runtime_set("silent_day_this_week", None)
        logger.info("silent_day_picker: disabled — cleared runtime key")
        return

    pool = cfg.get("engagement.weekly_silent_day_pool", ["mon", "tue", "wed", "thu", "fri"])
    if not isinstance(pool, list) or not pool:
        logger.warning("silent_day_picker: pool is empty or invalid — skipping")
        return

    day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    pool_nums = [day_map[d.lower()] for d in pool if d.lower() in day_map]
    if not pool_nums:
        logger.warning("silent_day_picker: no valid weekday names in pool %r — skipping", pool)
        return

    today = date.today()
    # Compute Monday of the coming week (the week that starts after this Sunday).
    # today.weekday() == 6 (Sunday) so days_to_monday = 1.
    days_to_monday = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_to_monday)

    chosen_offset = random.choice(pool_nums)          # 0=Mon … 4=Fri
    target = monday + timedelta(days=chosen_offset)
    db.runtime_set("silent_day_this_week", target.isoformat())
    logger.info("silent_day_picker: picked %s for the week of %s", target.isoformat(), monday)


async def _time_texture_job() -> None:
    """Hourly job: write time_texture to runtime_state based on current local hour."""
    import datetime as _dt

    from storage import db

    try:
        tz_name = cfg.get("scheduler.timezone", "UTC")
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")
        now = _dt.datetime.now(tz)
        phase = _hour_to_time_texture(now.hour)
        db.runtime_set("time_texture", phase)
        logger.info("time_texture: hour=%d -> %s", now.hour, phase)
    except Exception:
        logger.exception("time_texture_job: unexpected failure")


async def _diary_writer_job() -> None:
    """Daily 02:00 job: call diary.write_today_diary_if_significant() if available."""
    try:
        from agents import diary  # lazy import to avoid cycle
        fn = getattr(diary, "write_today_diary_if_significant", None)
        if fn is None:
            logger.warning(
                "diary_writer: agents.diary.write_today_diary_if_significant not found — skipping"
            )
            return
        await fn()
    except ImportError:
        logger.warning("diary_writer: agents.diary not available — skipping")
    except Exception:
        logger.exception("diary_writer: unexpected failure")


async def _interests_refresh_job() -> None:
    """Monthly day-1 job: call reflection.interests_refresh() if available."""
    try:
        from agents import reflection  # lazy import to avoid cycle
        fn = getattr(reflection, "interests_refresh", None)
        if fn is None:
            logger.warning(
                "interests_refresh: agents.reflection.interests_refresh not found — skipping"
            )
            return
        await fn()
    except ImportError:
        logger.warning("interests_refresh: agents.reflection not available — skipping")
    except Exception:
        logger.exception("interests_refresh: unexpected failure")


def build_scheduler(send_text) -> AsyncIOScheduler:
    """Wire up the background jobs. send_text is `async def send_text(s: str)`."""
    from .reflection import maybe_run_session_consolidation, run_daily_reflection

    tz_name = cfg.get("scheduler.timezone", "UTC")
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.warning("scheduler: invalid timezone %r, falling back to UTC", tz_name)
        tz = zoneinfo.ZoneInfo("UTC")
    scheduler = AsyncIOScheduler(timezone=tz)

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

    async def _gcal_sync_job():
        # Re-check at execution time so a probe that lands after build_scheduler
        # (or a credential recovery mid-session) is honoured without a restart.
        if not _calendar_creds_healthy():
            logger.debug(
                "reminders_gcal_sync: skipping execution — calendar creds unhealthy"
            )
            return
        return await sync_pending_gcal_reminders()

    scheduler.add_job(
        _gcal_sync_job,
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

    # Phase S: Annual review ceremony — Dec 26-31, 11:00 local.
    # Composes a year synthesis in Hikari voice (things worth more of /
    # things worth less of) from episodes, receipts, decisions, and drift
    # canary divergences. Idempotent via runtime_state key.
    if bool(cfg.get("annual_review.enabled", True)):
        from agents.annual_review import run_annual_review
        ar_hour = int(cfg.get("annual_review.fire_hour", 11))
        async def _annual_review_job():
            return await run_annual_review(send_text=send_text)
        scheduler.add_job(
            _annual_review_job,
            CronTrigger(month=12, day="26-31", hour=ar_hour, minute=0),
            id="annual_review",
            coalesce=True, max_instances=1, misfire_grace_time=86400,
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

    # Phase F: silent-day picker — Sunday 18:00 local. Chooses one random
    # weekday for the coming week and writes it to runtime_state so the
    # proactive gate can skip all non-user-anchored sends on that day.
    if bool(cfg.get("engagement.weekly_silent_day_enabled", True)):
        scheduler.add_job(
            _pick_silent_day_this_week,
            CronTrigger(day_of_week="sun", hour=18, minute=0),
            id="silent_day_picker",
            coalesce=True, max_instances=1, misfire_grace_time=3600,
        )

    # Ghost-of-Future-Self letter: first Sunday of the month, 10:00 local
    # (avoids the memory_prune 04:00 on day-of-month=1 collision). Composes
    # a letter AS the user 5 years from now, drawing on 30 days of real
    # activity. MIT Media Lab "Future You" project pattern.
    # CronTrigger(day='1-7', day_of_week='sun', ...) fires on the Sunday
    # that falls in the first 7 days of the month — i.e. the first Sunday.
    if bool(cfg.get("future_letter.enabled", True)):
        from .future_letter import run_future_letter
        fl_hour = int(cfg.get("future_letter.hour", 10))
        fl_minute = int(cfg.get("future_letter.minute", 0))
        async def _future_letter_job():
            return await run_future_letter(send_text)
        scheduler.add_job(
            _future_letter_job,
            CronTrigger(
                day="1-7", day_of_week="sun",
                hour=fl_hour, minute=fl_minute,
            ),
            id="future_letter",
            coalesce=True, max_instances=1, misfire_grace_time=3600,
        )

    # Decision-log resolver: weekly Sunday 19:00 local. Asks about
    # decisions whose resolve_by has passed. See agents/decision_log.py.
    if bool(cfg.get("decision_log.enabled", True)):
        from .decision_log import run_decision_resolver
        dl_hour = int(cfg.get("decision_log.hour", 19))
        dl_minute = int(cfg.get("decision_log.minute", 0))
        async def _decision_resolver_job():
            return await run_decision_resolver(send_text)
        scheduler.add_job(
            _decision_resolver_job,
            CronTrigger(day_of_week="sun", hour=dl_hour, minute=dl_minute),
            id="decision_resolver",
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

    async def _monthly_prune_job():
        from storage import db

        def _safe_prune(fn, days, label):
            try:
                return fn(days)
            except Exception as exc:
                logger.error("monthly_prune: %s failed: %s", label, exc)
                return -1

        n1 = _safe_prune(db.prune_messages_older_than_days,
                         int(cfg.get("retention.messages_days", 365)), "messages")
        n2 = _safe_prune(db.prune_oauth_audit_log_older_than_days,
                         int(cfg.get("retention.oauth_audit_log_days", 365)), "oauth_audit")
        n3 = _safe_prune(db.prune_calendar_notifications_older_than_days,
                         int(cfg.get("retention.calendar_notifications_days", 90)), "calendar")
        n4 = _safe_prune(db.prune_tool_calls,
                         int(cfg.get("retention.tool_calls_days", 30)), "tool_calls")
        n5 = _safe_prune(db.prune_graph_outbox_sent,
                         int(cfg.get("retention.graph_outbox_sent_days", 14)), "graph_outbox_sent")
        n6 = _safe_prune(db.prune_media_outbox_terminal,
                         int(cfg.get("retention.media_outbox_terminal_days", 14)), "media_outbox_terminal")
        n7 = _safe_prune(db.prune_proactive_events,
                         int(cfg.get("retention.proactive_events_days", 90)), "proactive_events")
        logger.info(
            "monthly_prune: messages=%d oauth_audit=%d calendar=%d tool_calls=%d "
            "graph_outbox_sent=%d media_outbox_terminal=%d proactive_events=%d",
            n1, n2, n3, n4, n5, n6, n7,
        )

    scheduler.add_job(
        _monthly_prune_job,
        CronTrigger(day=1, hour=4, minute=0),
        id="monthly_prune",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    # Hourly time_texture: write the current time-of-day phase to runtime_state.
    scheduler.add_job(
        _time_texture_job,
        IntervalTrigger(minutes=60),
        id="time_texture",
        coalesce=True, max_instances=1, misfire_grace_time=300,
    )

    # Daily 02:00: diary writer — significant day entries.
    scheduler.add_job(
        _diary_writer_job,
        CronTrigger(hour=2, minute=0),
        id="diary_writer",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    # Monthly interests refresh: day 1, 05:00.
    scheduler.add_job(
        _interests_refresh_job,
        CronTrigger(day=1, hour=5, minute=0),
        id="interests_refresh",
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )

    # Phase I: unified engagement_tick — replaces the per-producer wiki_new_file_tick.
    # Runs every 60s, collects candidates from all enabled producers, selects the
    # highest-scoring one, composes + guards + sends it.
    async def _engagement_tick():
        import asyncio
        import json
        from datetime import datetime
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        from agents import cadence
        from agents.engagement import composer, guard, producers, selector, sender
        from agents.engagement.producers import DEFAULT_ENABLED_SOURCES
        from agents.runtime import _RUN_LOCK
        from storage import db

        # Early-return while a user turn is in progress so the tick never
        # queues behind the lock and never contends with the running turn.
        if _RUN_LOCK.locked():
            logger.info("engagement_tick: _RUN_LOCK held — skipping tick")
            return

        # Pre-run gate: skip the whole tick during quiet hours or silence.
        if not guard.should_wake():
            logger.debug("engagement_tick: gate=skip (quiet/silenced)")
            return

        # Resolve enabled sources: runtime override wins over config default.
        raw_override = db.runtime_get("proactive_enabled_sources_override")
        if raw_override:
            try:
                enabled = set(json.loads(raw_override))
            except (ValueError, TypeError):
                enabled = set(DEFAULT_ENABLED_SOURCES)
        else:
            cfg_sources = cfg.get("proactive.default_enabled_sources")
            enabled = set(cfg_sources) if cfg_sources else set(DEFAULT_ENABLED_SOURCES)

        # Collect candidates from all enabled producers (sync calls, run in executor).
        loop = asyncio.get_event_loop()
        tasks = []
        source_ids = []
        for source_id in enabled:
            mod = producers.get_producer(source_id)
            if mod is None:
                continue
            source_ids.append(source_id)
            tasks.append(loop.run_in_executor(None, mod.collect))

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        candidates = []
        for source_id, result in zip(source_ids, results):
            if isinstance(result, Exception):
                logger.warning("engagement_tick: producer %r raised %s", source_id, result)
                continue
            if isinstance(result, list):
                candidates.extend(result)

        if not candidates:
            return

        tz_name = cfg.get("scheduler.timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        # Pool caps: ask the governor whether each pool still has headroom.
        # Use a known valid source per pool — the governor checks the pool's
        # rolling 7d counter against the cap, not the source itself.
        pool_caps = {
            "user_anchored": cadence.can_send("wiki_new_file", cadence.Pool.USER_ANCHORED)[0],
            "agent_spontaneous": cadence.can_send(
                "reengage_silence", cadence.Pool.AGENT_SPONTANEOUS
            )[0],
            "scheduled_ceremony": False,  # ceremony sources have their own dedicated jobs
        }
        ctx = SimpleNamespace(
            now_local=datetime.now(tz),
            mood=db.runtime_get("mood_today") or "focused",
            enabled_sources=enabled,
            pool_caps=pool_caps,
            source_response_rate=db.proactive_source_response_rates(days=30),
            last_send_per_source=db.proactive_last_send_per_source(),
        )

        candidate = selector.select(candidates, ctx)
        if candidate is None:
            return

        text = await composer.compose(candidate)
        if not text:
            return

        ok, reason = guard.passes(text, candidate)
        if not ok:
            text = await composer.compose(candidate, retry_hint=reason)
            if not text:
                return
            ok, reason = guard.passes(text, candidate)
            if not ok:
                logger.info("engagement_tick: dropped after 2 guard fails — %s (source=%s)",
                            reason, candidate.source)
                return

        row_id = await sender.send(text, candidate, send_text)
        if row_id is not None:
            # Call mark_consumed on the producer module if it defines it.
            mod = producers.get_producer(candidate.source)
            if mod and hasattr(mod, "mark_consumed"):
                try:
                    mod.mark_consumed(candidate)
                except Exception:
                    logger.exception(
                        "engagement_tick: mark_consumed failed for %s", candidate.source
                    )

    scheduler.add_job(
        _engagement_tick,
        IntervalTrigger(seconds=60),
        id="engagement_tick",
        coalesce=True, max_instances=1, misfire_grace_time=60,
    )

    # Phase H: periodic MCP warm-pool eviction — runs every 30s to reap stale
    # server entries so the warm_servers() view stays accurate.
    from agents.mcp_manager import MANAGER as _mcp_manager
    async def _mcp_evict_job():
        await _mcp_manager.evict_stale()
    scheduler.add_job(
        _mcp_evict_job,
        IntervalTrigger(seconds=30),
        id="mcp_warm_pool_evict",
        coalesce=True, max_instances=1, misfire_grace_time=30,
    )

    # Phase 5D: Graphiti outbox drain — runs every 30s if GRAPHITI_ENABLED != 'false'.
    import os as _os
    if _os.environ.get("GRAPHITI_ENABLED", "true").strip().lower() != "false":
        _add_graph_outbox_drain_job(scheduler)

    # 9A: Periodic media_outbox drain — catches pending rows that weren't drained
    # after their originating turn (e.g. send_and_persist crash, restart mid-turn).
    from agents.runtime import owner_id as _owner_id

    async def _media_outbox_drain_job():
        from agents.telegram_bridge import _drain_media_outbox  # noqa: PLC0415
        try:
            from telegram import Bot  # noqa: PLC0415
            import os  # noqa: PLC0415
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if not token:
                return
            bot = Bot(token=token)
            counts = await _drain_media_outbox(bot, _owner_id())
            total = sum(counts.values())
            if total:
                logger.info("media_outbox_drain (periodic): %s", counts)
        except Exception:
            logger.exception("media_outbox_drain: unexpected failure")

    scheduler.add_job(
        _media_outbox_drain_job,
        IntervalTrigger(minutes=2),
        id="media_outbox_drain",
        coalesce=True, max_instances=1, misfire_grace_time=60,
    )

    return scheduler


def _calendar_creds_healthy() -> bool:
    """Checked at each job execution (not just at build time) so a probe that
    finishes after build_scheduler takes effect on the next tick without a
    restart. Returns True when ``runtime_state.calendar_heartbeat_healthy`` is
    '1', or when all three OAuth env vars are present and no explicit override.
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
