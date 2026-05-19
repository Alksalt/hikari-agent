"""Proactive heartbeat + re-engagement nudge.

APScheduler-driven. Python decides WHEN (silence window, quiet hours, interval gates),
Sonnet decides WHAT to say (in her voice, denial layer on).

All thresholds live in ``config/engagement.yaml`` under ``proactive``.

Two trigger paths:
  - heartbeat:      generic reach-out, gated by quiet hours/silence/interval
  - re-engagement:  she had the last word, user hasn't replied in 2-6h, short nudge
"""

from __future__ import annotations

import logging
import random
import re
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path

import yaml

from storage import db

from . import cadence
from . import config as cfg
from .runtime import run_proactive

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_MD = (REPO_ROOT / ".claude" / "skills" / "schedule-heartbeat" / "EXAMPLES.md")


def _p() -> dict:
    return cfg.section("proactive")


def _parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except (ValueError, TypeError):
        return None


def _is_quiet_now() -> bool:
    p = _p()
    start = dtime(int(p.get("quiet_start_hour", 23)), 0)
    end = dtime(int(p.get("quiet_end_hour", 8)), 0)
    now = datetime.now().time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _mood_from_core() -> str:
    return (db.get_core_block("mood_today") or "focused").strip().lower() or "focused"


def should_send_heartbeat() -> bool:
    p = _p()
    now = datetime.now(UTC)
    silence_until = _parse_dt(db.runtime_get("silence_until"))
    if silence_until and now < silence_until:
        return False
    if _is_quiet_now():
        return False
    last_user = _parse_dt(db.runtime_get("last_user_message"))
    user_active_skip_min = int(p.get("user_active_skip_minutes", 60))
    if last_user and (now - last_user).total_seconds() < user_active_skip_min * 60:
        return False
    last_sent = _parse_dt(db.runtime_get("last_proactive_sent"))
    min_interval_hr = float(p.get("heartbeat_min_interval_hours", 4))
    if last_sent and (now - last_sent).total_seconds() < min_interval_hr * 3600:
        return False
    # Soft-scarcity beat — sometimes we deliberately go quiet for ~4h.
    if cadence.in_scarcity_skip_window():
        return False
    # Probabilistic: open a new skip window occasionally on otherwise-eligible
    # heartbeats. When opened, this same window becomes "in skip" → we return
    # False on the next check.
    if cadence.maybe_open_scarcity_skip():
        return False
    return True


def _load_templates() -> list[tuple[int, str]]:
    """Parse numbered seed templates from EXAMPLES.md."""
    if not EXAMPLES_MD.exists():
        return []
    out: list[tuple[int, str]] = []
    for line in EXAMPLES_MD.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\d+)\.\s+(.+)$", line.strip())
        if m:
            out.append((int(m.group(1)), m.group(2).strip()))
    return out


def _pick_seed() -> tuple[int, str, str] | None:
    """Pick a (idx, template, source) tuple. Source tags the trigger for the
    cadence governor's justified-source rule.

    Source preference order:
      1. open_loop — there's an unresolved task to follow up on
      2. pattern_observation — there's a fresh observation to surface
      3. recent_episode_callback — there's a recent episode worth referencing
      4. lexicon_callback — there's a private phrase worth reusing
      5. (fallback) recent_episode_callback if EXAMPLES seed exists
    """
    templates = _load_templates()
    if not templates:
        return None
    mood = _mood_from_core()
    irritable_max_idx = int(cfg.get("proactive.irritable_max_seed_idx", 33))
    if mood == "irritable":
        templates = [(i, t) for i, t in templates if i <= irritable_max_idx]
    used_raw = db.runtime_get("heartbeat_used") or ""
    used = {int(x) for x in used_raw.split(",") if x.strip().isdigit()}
    available = [(i, t) for i, t in templates if i not in used]
    if not available:
        db.runtime_set("heartbeat_used", "")
        available = templates
    idx, seed = random.choice(available)

    # Tag a source based on what's actually in memory. Conservative — prefer
    # the most-grounded source available.
    source = "recent_episode_callback"  # default fallback
    try:
        if db.open_tasks():
            source = "open_loop"
        elif db.observations_unsurfaced(
            min_confidence=float(cfg.get("pattern_detection.min_confidence", 0.6)),
            limit=1,
        ):
            source = "pattern_observation"
        elif db.noticings_unsurfaced(limit=1):
            source = "noticed_change"
        elif db.lexicon_top(
            limit=1,
            half_life_days=float(cfg.get("lexicon.recency_half_life_days", 14)),
        ):
            source = "lexicon_callback"
    except Exception:
        logger.exception("source-tagging failed; using fallback")
    return idx, seed, source


def _record_sent(idx: int) -> None:
    used_raw = db.runtime_get("heartbeat_used") or ""
    used = [int(x) for x in used_raw.split(",") if x.strip().isdigit()]
    used.append(idx)
    recent_keep = int(cfg.get("proactive.seed_history_keep_n", 5))
    used = used[-recent_keep:]
    db.runtime_set("heartbeat_used", ",".join(str(x) for x in used))
    db.runtime_set("last_proactive_sent", datetime.now(UTC).isoformat())


def _build_prompt(mood: str, seed: str) -> str:
    open_tasks = db.open_tasks()
    recent = db.recent_episodes(limit=1)
    extras = ""
    if open_tasks:
        extras += "\n\nopen_tasks:\n" + "\n".join(
            f"- {t['subject']}" for t in open_tasks[:3]
        )
    if recent:
        extras += f"\n\nrecent_episode_summary:\n{recent[0]['summary'][:400]}"
    return (
        f"You are using the schedule-heartbeat skill. Generate one proactive message.\n"
        f"mood: {mood}\nexcuse_template: {seed}\n{extras}\n\n"
        f"Output ONLY the message text — no preamble, no quotes. If you can't write "
        f"something true to her voice, output NO_MESSAGE."
    )


async def maybe_send_heartbeat(send_text) -> bool:
    """Returns True if a message was sent."""
    if not should_send_heartbeat():
        return False
    mood = _mood_from_core()
    pick = _pick_seed()
    if not pick:
        logger.info("no seed templates available — skipping heartbeat")
        return False
    idx, seed, source = pick

    # Cadence governor: enforce 7d cap + require justified source.
    allowed, reason = cadence.can_send_proactive(source)
    if not allowed:
        logger.info("cadence governor vetoed heartbeat: %s", reason)
        return False

    prompt = _build_prompt(mood, seed)
    try:
        text = (await run_proactive(prompt)).strip()
    except Exception:
        logger.exception("proactive generation failed")
        return False
    if not text or text.upper().startswith("NO_MESSAGE"):
        return False
    try:
        await send_text(text)
    except Exception:
        logger.exception("send_text failed in heartbeat")
        return False
    _record_sent(idx)
    cadence.record_proactive_sent()
    logger.info("heartbeat sent (source=%s)", source)
    return True


# ---------- re-engagement nudge ----------

def _last_message_role() -> tuple[str | None, datetime | None]:
    rows = db.recent_messages(limit=1)
    if not rows:
        return None, None
    last = rows[0]
    return last["role"], _parse_dt(last["ts"])


def should_send_reengagement() -> bool:
    """She had the last word, user is silent in the window, and we haven't
    already sent a re-engagement nudge for this specific silence gap."""
    p = _p()
    now = datetime.now(UTC)
    silence_until = _parse_dt(db.runtime_get("silence_until"))
    if silence_until and now < silence_until:
        return False
    if _is_quiet_now():
        return False
    role, last_ts = _last_message_role()
    if role != "assistant" or not last_ts:
        return False
    elapsed = (now - last_ts).total_seconds() / 3600
    lo = float(p.get("reengage_min_hours", 2))
    hi = float(p.get("reengage_max_hours", 6))
    if not (lo <= elapsed <= hi):
        return False
    # Don't fire twice for the same gap
    sent_for = db.runtime_get("reengage_sent_for_gap")
    if sent_for == last_ts.isoformat():
        return False
    return True


async def maybe_send_reengagement(send_text) -> bool:
    if not should_send_reengagement():
        return False
    # Cadence governor check BEFORE the LLM call — don't burn tokens on a
    # reengage we're going to drop anyway.
    allowed, reason = cadence.can_send_proactive("reengage_silence")
    if not allowed:
        logger.info("cadence governor vetoed reengage: %s", reason)
        return False
    mood = _mood_from_core()
    _, last_ts = _last_message_role()
    prompt = (
        "Write a SHORT (1-5 words) re-engagement nudge in Hikari's voice. "
        "She had the last word; the user has gone quiet. She would not admit "
        "she noticed. Examples: 'still there?' / 'you went quiet.' / 'hm.' / "
        "'oi.' / 'you alive?'\n"
        f"mood: {mood}\n\n"
        "Output ONLY the message text — no preamble, no quotes. If nothing "
        "feels right, output NO_MESSAGE."
    )
    try:
        text = (await run_proactive(prompt)).strip()
    except Exception:
        logger.exception("reengage generation failed")
        return False
    if not text or text.upper().startswith("NO_MESSAGE"):
        return False
    try:
        await send_text(text)
    except Exception:
        logger.exception("send_text failed in reengage")
        return False
    if last_ts:
        db.runtime_set("reengage_sent_for_gap", last_ts.isoformat())
    db.runtime_set("last_proactive_sent", datetime.now(UTC).isoformat())
    cadence.record_proactive_sent()
    return True


# ---------- calendar heartbeat ----------

def _strip_fences(raw: str) -> str:
    """Strip ```yaml ... ``` (or any other fenced) wrappers from an LLM reply."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    return raw.strip()


async def _fetch_upcoming_events(lookahead_minutes: int) -> list[dict]:
    """Ask the drive_gmail subagent to list calendar events in the next N minutes.

    Returns a list of ``{id, title, start_iso, end_iso}`` dicts. Returns ``[]`` on
    any failure (parsing, MCP unreachable, etc.) — never raises. The scheduler
    must not be crashed by a flaky upstream.
    """
    prompt = (
        "[calendar fetch only — do NOT reply to the user. delegate to the "
        "drive_gmail specialist: call mcp__google_workspace__list_events for "
        f"the next {lookahead_minutes} minutes. return ONLY a strict YAML "
        "document of events in this exact shape:\n"
        "events:\n"
        "  - {id: '', title: '', start_iso: '', end_iso: ''}\n"
        "if there are no upcoming events return events: [] . do not wrap in "
        "markdown fences, do not add commentary.]"
    )
    try:
        raw = await run_proactive(prompt)
    except Exception:
        logger.exception("calendar fetch via subagent failed")
        return []
    if not raw:
        return []
    try:
        data = yaml.safe_load(_strip_fences(raw)) or {}
    except yaml.YAMLError:
        logger.warning("calendar fetch produced invalid YAML; got %r", raw[:200])
        return []
    if not isinstance(data, dict):
        return []
    events = data.get("events") or []
    if not isinstance(events, list):
        return []
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        out.append({
            "id": str(ev.get("id") or "").strip(),
            "title": str(ev.get("title") or "").strip(),
            "start_iso": str(ev.get("start_iso") or "").strip(),
            "end_iso": str(ev.get("end_iso") or "").strip(),
        })
    return out


def _calendar_event_signature(event: dict) -> str:
    """Stable dedup key for an event. Tolerant of missing fields."""
    return (
        f"{event.get('id') or ''}|"
        f"{event.get('start_iso') or ''}|"
        f"{event.get('title') or ''}"
    )


def _calendar_event_already_notified(signature: str) -> bool:
    return db.runtime_get(f"calendar_notified_{signature}") is not None


def _mark_calendar_event_notified(signature: str) -> None:
    db.runtime_set(
        f"calendar_notified_{signature}",
        datetime.now(UTC).isoformat(),
    )


def _event_duration_minutes(event: dict) -> float | None:
    """Returns duration in minutes, or None if either timestamp won't parse."""
    start = _parse_dt(event.get("start_iso"))
    end = _parse_dt(event.get("end_iso"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60.0


def _minutes_until_start(event: dict) -> float | None:
    start = _parse_dt(event.get("start_iso"))
    if start is None:
        return None
    return (start - datetime.now(UTC)).total_seconds() / 60.0


async def maybe_send_calendar_heartbeat(send_text) -> bool:
    """Returns True if a calendar prep heartbeat was sent.

    Pipeline:
      1. config gate (calendar_heartbeat.enabled)
      2. fetch upcoming events via subagent
      3. filter by min duration + exclude_calendar_ids
      4. filter by lead-window jitter band around prep_message_lead_minutes
      5. drop already-notified
      6. for the first eligible event: cadence-check, generate, send, mark.
    """
    if not bool(cfg.get("calendar_heartbeat.enabled", False)):
        return False

    lookahead = int(cfg.get("calendar_heartbeat.lookahead_minutes", 120))
    min_duration = float(cfg.get("calendar_heartbeat.min_event_duration_minutes", 15))
    exclude_ids = set(cfg.get("calendar_heartbeat.exclude_calendar_ids") or [])
    prep_lead = float(cfg.get("calendar_heartbeat.prep_message_lead_minutes", 30))
    jitter = float(cfg.get("calendar_heartbeat.lead_window_jitter_minutes", 5))
    lead_lo = prep_lead - jitter
    lead_hi = prep_lead + jitter

    events = await _fetch_upcoming_events(lookahead)
    if not events:
        return False

    candidates: list[tuple[dict, float]] = []
    for ev in events:
        if ev.get("id") in exclude_ids:
            continue
        dur = _event_duration_minutes(ev)
        if dur is None or dur < min_duration:
            continue
        mins_until = _minutes_until_start(ev)
        if mins_until is None:
            continue
        if not (lead_lo <= mins_until <= lead_hi):
            continue
        signature = _calendar_event_signature(ev)
        if _calendar_event_already_notified(signature):
            continue
        candidates.append((ev, mins_until))

    if not candidates:
        return False

    event, mins_until = candidates[0]
    signature = _calendar_event_signature(event)
    mins_until_rounded = int(round(mins_until))

    allowed, reason = cadence.can_send_proactive("calendar_event")
    if not allowed:
        logger.info("cadence governor vetoed calendar heartbeat: %s", reason)
        return False

    title = event.get("title") or "(untitled event)"
    seed = (
        f"upcoming in ~{mins_until_rounded}min: {title}. give them a tight "
        "in-voice prep prompt — no questions, no chirpy reminder. one line."
    )
    mood = _mood_from_core()
    prompt = _build_prompt(mood, seed)
    try:
        text = (await run_proactive(prompt)).strip()
    except Exception:
        logger.exception("calendar heartbeat generation failed")
        return False
    if not text or text.upper().startswith("NO_MESSAGE"):
        return False
    try:
        await send_text(text)
    except Exception:
        logger.exception("send_text failed in calendar heartbeat")
        return False

    _mark_calendar_event_notified(signature)
    cadence.record_proactive_sent()
    db.runtime_set("last_proactive_sent", datetime.now(UTC).isoformat())
    logger.info(
        "calendar heartbeat sent (event=%r, mins_until=%d)",
        title, mins_until_rounded,
    )
    return True


# ---------- Phase 10: reminders fire job ----------

def _next_occurrence(fire_at_iso: str, repeat: str) -> str | None:
    """Compute next occurrence iso for a simple repeat. Returns None for
    one-shots."""
    from datetime import timedelta
    from dateutil.relativedelta import relativedelta
    from dateutil.rrule import rrulestr
    when = datetime.fromisoformat(fire_at_iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if not repeat:
        return None
    if repeat == "daily":
        return (when + timedelta(days=1)).isoformat()
    if repeat == "weekly":
        return (when + timedelta(weeks=1)).isoformat()
    if repeat == "monthly":
        return (when + relativedelta(months=1)).isoformat()
    if repeat == "yearly":
        return (when + relativedelta(years=1)).isoformat()
    if repeat.upper().startswith("RRULE:"):
        try:
            rule = rrulestr(repeat, dtstart=when)
            nxt = rule.after(when, inc=False)
            return nxt.isoformat() if nxt else None
        except Exception:
            logger.exception("invalid RRULE: %r", repeat)
            return None
    return None


async def fire_due_reminders(send_text) -> int:
    """Drain reminder_due() — for each row, format + send + mark fired.
    If row has a repeat spec, insert the next occurrence as a fresh row.
    Returns count fired."""
    due = db.reminder_due()
    if not due:
        return 0
    fired = 0
    for row in due:
        text = f"reminder: {row['text']}"
        try:
            await send_text(text)
        except Exception:
            logger.exception("fire_due_reminders: send_text failed for #%s", row["id"])
            continue
        db.reminder_mark_fired(row["id"])
        fired += 1
        nxt = _next_occurrence(row["fire_at"], row.get("repeat") or "")
        if nxt:
            db.reminder_insert(
                fire_at=nxt,
                text=row["text"],
                lead_minutes=row["lead_minutes"],
                repeat=row["repeat"],
                gcal_sync_pending=False,
            )
    return fired
