"""Daily inbox + calendar check-in routine.

Single scheduler poll every 5 min decides whether to fire today based on
``core_blocks.daily_checkin_schedule`` (YAML) and
``runtime_state.daily_checkin_last_fired_date``. On fire, posts a single
short message asking yes/no to email and calendar; the bridge pre-routes
the user's reply via ``parse_intent``.

See ``docs/superpowers/specs/2026-05-20-daily-inbox-calendar-routine-design.md``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import yaml

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)

POLL_TOLERANCE_MINUTES = 5  # how wide a window counts as "matches today's target"


# ---------- enable flag ----------

def _is_enabled() -> bool:
    return bool(cfg.get("daily_checkin.enabled", True))


# ---------- schedule resolver ----------

def _load_schedule() -> dict[str, Any]:
    raw = db.get_core_block("daily_checkin_schedule")
    if not raw:
        return {}
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        logger.warning("daily_checkin_schedule: malformed YAML; using defaults")
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _resolve_target_time(now_local: datetime) -> str:
    """Resolve today's target HH:MM string, accounting for one-shot override."""
    schedule = _load_schedule()
    override_date = str(schedule.get("override_date") or "")
    override_time = str(schedule.get("override_time") or "")
    today_iso = now_local.date().isoformat()
    if override_date == today_iso and override_time:
        return override_time
    default_time = str(schedule.get("default_time") or "")
    if not default_time:
        default_time = str(cfg.get("daily_checkin.default_time", "07:00"))
    return default_time


def _is_skipped_today(now_local: datetime) -> bool:
    schedule = _load_schedule()
    skip = schedule.get("skip_dates") or []
    if not isinstance(skip, list):
        return False
    today_iso = now_local.date().isoformat()
    return today_iso in [str(d) for d in skip]


def _already_fired_today(now_local: datetime) -> bool:
    last = db.runtime_get("daily_checkin_last_fired_date") or ""
    return last == now_local.date().isoformat()


def should_fire_now(now_local: datetime) -> bool:
    """True iff the daily check-in should fire *now* given the configured
    schedule, override, skip-list, and dedup state.

    ``now_local`` MUST be timezone-aware in the user's local zone."""
    if not _is_enabled():
        return False
    if _is_skipped_today(now_local):
        return False
    if _already_fired_today(now_local):
        return False
    target_hhmm = _resolve_target_time(now_local)
    try:
        hh, mm = [int(p) for p in target_hhmm.split(":", 1)]
    except (ValueError, AttributeError):
        logger.warning("daily_checkin: malformed target time %r", target_hhmm)
        return False
    target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    poll_tolerance = int(cfg.get("daily_checkin.poll_interval_minutes",
                                 POLL_TOLERANCE_MINUTES))
    # Fire if now ∈ [target, target + poll_tolerance) — the poll runs every
    # ``poll_tolerance`` minutes; this window ensures we catch the slot once
    # without firing twice.
    return target <= now_local < target + timedelta(minutes=poll_tolerance)


def mark_fired_today(now_local: datetime) -> None:
    db.runtime_set("daily_checkin_last_fired_date", now_local.date().isoformat())


def clear_expired_overrides(now_local: datetime) -> None:
    """Remove override/skip entries that are in the past. Called after fire."""
    schedule = _load_schedule()
    if not schedule:
        return
    today_iso = now_local.date().isoformat()
    changed = False
    override_date = str(schedule.get("override_date") or "")
    if override_date and override_date <= today_iso:
        schedule.pop("override_date", None)
        schedule.pop("override_time", None)
        changed = True
    skip = schedule.get("skip_dates") or []
    if isinstance(skip, list):
        kept = [str(d) for d in skip if str(d) > today_iso]
        if len(kept) != len(skip):
            schedule["skip_dates"] = kept
            changed = True
    if changed:
        db.upsert_core_block("daily_checkin_schedule",
                             yaml.safe_dump(schedule, sort_keys=True))
