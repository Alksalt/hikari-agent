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
import re
from datetime import date as _date
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


# ---------- intent parser ----------

_AFFIRMATIVE_RE = re.compile(
    r"^\s*(y|yes|yeah|yep|ok|okay|sure|fine|go|do it|both|yes both|both yes)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^\s*(n|no|nope|nah|skip|skip it|leave (it|them)|not now)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_EMAIL_ONLY_RE = re.compile(
    r"^\s*(just|only)\s+(email|emails|inbox)\s*[.!]?\s*$"
    r"|^\s*(email|emails|inbox)\s+only\s*[.!]?\s*$",
    re.IGNORECASE,
)
_CALENDAR_ONLY_RE = re.compile(
    r"^\s*(just|only)\s+(calendar|cal)\s*[.!]?\s*$"
    r"|^\s*(calendar|cal)\s+only\s*[.!]?\s*$",
    re.IGNORECASE,
)


def parse_intent(text: str) -> dict[str, bool] | None:
    """Map a short user reply to ``{email: bool, calendar: bool}``.

    Returns ``None`` if the reply is ambiguous — caller may then either
    drop the pending state or call the LLM fallback parser.
    """
    if not text:
        return None
    if _EMAIL_ONLY_RE.match(text):
        return {"email": True, "calendar": False}
    if _CALENDAR_ONLY_RE.match(text):
        return {"email": False, "calendar": True}
    if _AFFIRMATIVE_RE.match(text):
        return {"email": True, "calendar": True}
    if _NEGATIVE_RE.match(text):
        return {"email": False, "calendar": False}
    return None


# ---------- schedule edit parser ----------

_OVERRIDE_RE = re.compile(
    r"\bcheck\s*in\s+at\s+(\d{1,2}):(\d{2})\s+(today|tomorrow|tmrw)\b",
    re.IGNORECASE,
)
_DEFAULT_RE = re.compile(
    r"\b(?:from now on\s+)?(?:set\s+(?:morning|daily)\s+check\s+to|"
    r"check\s+in\s+at)\s+(\d{1,2}):(\d{2})\b(?!\s+(?:today|tomorrow|tmrw))",
    re.IGNORECASE,
)
_SKIP_RE = re.compile(
    r"\bskip\s+(?:the\s+)?(?:morning|daily)\s+check\s+(today|tomorrow|tmrw)\b",
    re.IGNORECASE,
)
_QUERY_RE = re.compile(
    r"\bwhat\s+time\s+is\s+my\s+(?:morning\s+|daily\s+)?check[-\s]?in\b",
    re.IGNORECASE,
)


def _resolve_relative_date(token: str, today: _date) -> str:
    token = token.lower()
    if token in ("tomorrow", "tmrw"):
        return (today + timedelta(days=1)).isoformat()
    return today.isoformat()


def parse_schedule_edit(text: str, *, today: _date) -> dict[str, Any] | None:
    """Detect schedule-change commands. Returns a dict like::

        {"kind": "override", "date": "YYYY-MM-DD", "time": "HH:MM"}
        {"kind": "default",  "time": "HH:MM"}
        {"kind": "skip",     "date": "YYYY-MM-DD"}
        {"kind": "query"}

    Returns ``None`` if no pattern matches."""
    if not text:
        return None
    m = _OVERRIDE_RE.search(text)
    if m:
        hh, mm, when = m.group(1), m.group(2), m.group(3)
        return {
            "kind": "override",
            "date": _resolve_relative_date(when, today),
            "time": f"{int(hh):02d}:{mm}",
        }
    m = _SKIP_RE.search(text)
    if m:
        when = m.group(1)
        return {"kind": "skip", "date": _resolve_relative_date(when, today)}
    m = _DEFAULT_RE.search(text)
    if m:
        hh, mm = m.group(1), m.group(2)
        return {"kind": "default", "time": f"{int(hh):02d}:{mm}"}
    if _QUERY_RE.search(text):
        return {"kind": "query"}
    return None


def apply_schedule_edit(edit: dict[str, Any]) -> None:
    """Mutate ``core_blocks.daily_checkin_schedule`` per the parsed edit."""
    schedule = _load_schedule()
    kind = edit.get("kind")
    if kind == "override":
        schedule["override_date"] = edit["date"]
        schedule["override_time"] = edit["time"]
    elif kind == "default":
        schedule["default_time"] = edit["time"]
    elif kind == "skip":
        skip = schedule.get("skip_dates") or []
        if not isinstance(skip, list):
            skip = []
        date_iso = edit["date"]
        if date_iso not in [str(d) for d in skip]:
            skip.append(date_iso)
        schedule["skip_dates"] = sorted(set(str(d) for d in skip))
    elif kind == "query":
        return  # read-only; caller composes the answer
    else:
        raise ValueError(f"unknown schedule edit kind: {kind!r}")
    db.upsert_core_block("daily_checkin_schedule",
                         yaml.safe_dump(schedule, sort_keys=True))


def describe_current_schedule() -> str:
    """Human-readable summary for the 'what time is my check-in' query."""
    schedule = _load_schedule()
    default_time = (schedule.get("default_time")
                    or cfg.get("daily_checkin.default_time", "07:00"))
    parts = [f"default {default_time}"]
    if schedule.get("override_date") and schedule.get("override_time"):
        parts.append(f"override {schedule['override_date']} at {schedule['override_time']}")
    skip = schedule.get("skip_dates") or []
    if skip:
        parts.append(f"skipping {', '.join(str(d) for d in skip)}")
    return "; ".join(parts)
