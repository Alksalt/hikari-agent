"""Daily inbox + calendar check-in routine.

Single scheduler poll every 5 min decides whether to fire today based on
``core_blocks.daily_checkin_schedule`` (YAML) and
``runtime_state.daily_checkin_last_fired_date``. On fire, posts a single
short message asking yes/no to email and calendar; the bridge pre-routes
the user's reply via ``parse_intent``.

See ``docs/superpowers/specs/2026-05-20-daily-inbox-calendar-routine-design.md``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any

import yaml

from agents import config as cfg
from agents.runtime import looks_like_sdk_error, run_internal_control
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


# ---------- email + calendar fetches ----------

def _empty_email_result() -> dict[str, Any]:
    """Factory so each error-path return owns its own nested dicts.
    A module-level constant would let callers mutate the shared state."""
    return {
        "unread_personal": [],
        "calendar_invites": [],
        "deletable": {"count": 0, "top_senders": [], "sample_ids": []},
    }


async def fetch_email_buckets() -> dict[str, Any]:
    """Delegate to the drive_gmail subagent and return three buckets.

    On ANY failure (auth error, malformed YAML, exception) returns the
    canonical empty shape. Never raises.
    """
    prompt = (
        "[daily check-in email fetch only — do NOT reply to the user. "
        "delegate to the drive_gmail specialist. perform three Gmail "
        "queries via mcp__google_workspace__query_gmail_emails:\n"
        "  1. is:unread is:inbox -category:promotions -category:updates -has:invite "
        "(unread personal mail, last 24h)\n"
        "  2. (has:invite OR from:noreply@google.com) is:unread "
        "(unread calendar invites)\n"
        "  3. (category:promotions OR category:updates) newer_than:7d "
        "(deletable promo/update pile)\n\n"
        "return ONLY a strict YAML document in this exact shape:\n"
        "unread_personal:\n"
        "  - {id: '', from: '', subject: '', snippet: ''}\n"
        "calendar_invites:\n"
        "  - {id: '', from: '', subject: ''}\n"
        "deletable:\n"
        "  count: 0\n"
        "  top_senders: []\n"
        "  sample_ids: []\n\n"
        "for top_senders, return up to 3 most-frequent sender domains in the "
        "deletable bucket. for sample_ids, return ALL message IDs in the "
        "deletable bucket (will be capped client-side). do not wrap in markdown "
        "fences, do not add commentary.]"
    )
    try:
        raw = await run_internal_control(prompt, max_turns=5,
                                          max_budget_usd=0.05)
    except Exception:
        logger.exception("daily_checkin email fetch failed")
        return _empty_email_result()
    if not raw or looks_like_sdk_error(raw):
        if raw:
            logger.warning("daily_checkin email fetch: SDK error string in result: %r",
                           raw[:120])
        return _empty_email_result()
    try:
        data = yaml.safe_load(_strip_yaml_fences(raw)) or {}
    except yaml.YAMLError:
        logger.warning("daily_checkin email fetch: malformed YAML; got %r", raw[:120])
        return _empty_email_result()
    if not isinstance(data, dict):
        return _empty_email_result()

    out: dict[str, Any] = _empty_email_result()
    out["unread_personal"] = _coerce_message_list(data.get("unread_personal"))
    out["calendar_invites"] = _coerce_message_list(data.get("calendar_invites"))
    deletable = data.get("deletable") or {}
    if isinstance(deletable, dict):
        count = int(deletable.get("count") or 0)
        senders = [str(s) for s in (deletable.get("top_senders") or []) if s]
        sample_ids = [str(m) for m in (deletable.get("sample_ids") or []) if m]
        max_ids = int(cfg.get("daily_checkin.max_delete_ids", 200))
        out["deletable"] = {
            "count": count,
            "top_senders": senders[: int(cfg.get(
                "daily_checkin.deletable_top_senders_cap", 3))],
            "sample_ids": sample_ids[:max_ids],
        }
    return out


async def fetch_calendar_events() -> list[dict[str, Any]]:
    """Delegate to drive_gmail for today's calendar events.

    Mutates ``runtime_state.calendar_last_known_event_ids`` to enable
    new-since-yesterday detection on the next call.
    """
    from datetime import datetime as _dt
    from datetime import time as _time

    tz = _resolve_local_tz()
    now_local = _dt.now(tz)
    end_local = _dt.combine(now_local.date(), _time(23, 59, 59), tzinfo=tz)
    time_min = now_local.isoformat()
    time_max = end_local.isoformat()
    prompt = (
        "[daily check-in calendar fetch only — do NOT reply to the user. "
        "delegate to the drive_gmail specialist: call "
        "mcp__google_workspace__calendar_get_events with "
        f"time_min='{time_min}' and time_max='{time_max}', "
        "calendar_id='primary'.\n"
        "return ONLY a strict YAML document in this exact shape:\n"
        "events:\n"
        "  - {id: '', title: '', start_iso: '', end_iso: '', location: '', "
        "attendees_count: 0}\n"
        "if there are no events return events: [] . do not wrap in markdown "
        "fences, do not add commentary.]"
    )
    try:
        raw = await run_internal_control(prompt, max_turns=5,
                                          max_budget_usd=0.05)
    except Exception:
        logger.exception("daily_checkin calendar fetch failed")
        return []
    if not raw or looks_like_sdk_error(raw):
        return []
    try:
        data = yaml.safe_load(_strip_yaml_fences(raw)) or {}
    except yaml.YAMLError:
        logger.warning("daily_checkin calendar fetch: malformed YAML; got %r",
                       raw[:120])
        return []
    raw_events = data.get("events") or []
    if not isinstance(raw_events, list):
        return []

    prev_ids = set()
    prev_raw = db.runtime_get("calendar_last_known_event_ids") or ""
    if prev_raw:
        try:
            loaded = json.loads(prev_raw)
            if isinstance(loaded, list):
                prev_ids = set(str(i) for i in loaded)
        except (ValueError, TypeError):
            pass

    out: list[dict[str, Any]] = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("id") or "").strip()
        out.append({
            "id": eid,
            "title": str(ev.get("title") or "").strip(),
            "start_iso": str(ev.get("start_iso") or "").strip(),
            "end_iso": str(ev.get("end_iso") or "").strip(),
            "location": str(ev.get("location") or "").strip(),
            "attendees_count": int(ev.get("attendees_count") or 0),
            "is_new_since_yesterday": eid not in prev_ids,
        })

    # Persist unconditionally — a successful zero-event day resets the known
    # set, so the next day everything is correctly flagged is_new. Error
    # paths return early above and DO NOT overwrite the prior set.
    new_ids = sorted({e["id"] for e in out if e["id"]})
    db.runtime_set("calendar_last_known_event_ids", json.dumps(new_ids))
    return out


# ---------- internal helpers ----------

def _strip_yaml_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    return raw.strip()


def _coerce_message_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cap = int(cfg.get("daily_checkin.personal_subject_cap", 5))
    out: list[dict[str, Any]] = []
    for item in value[:cap]:
        if not isinstance(item, dict):
            continue
        out.append({
            "id": str(item.get("id") or "").strip(),
            "from": str(item.get("from") or "").strip(),
            "subject": str(item.get("subject") or "").strip(),
            "snippet": str(item.get("snippet") or "").strip(),
        })
    return out


def _resolve_local_tz():
    """Resolve the user's local TZ via HOME_TZ env, falling back to UTC."""
    import os
    import zoneinfo
    name = os.environ.get("HOME_TZ", "UTC")
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return zoneinfo.ZoneInfo("UTC")
