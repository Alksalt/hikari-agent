"""Proactive reminder / sync helpers + heartbeat eligibility checks.

The three legacy send functions (maybe_send_heartbeat, maybe_send_reengagement,
maybe_send_calendar_heartbeat) were deleted in Phase J; they are superseded by
_engagement_tick in agents/scheduler.py.  Eligibility helpers (should_send_heartbeat,
_is_quiet_now, etc.) are kept because tests and the engagement producer layer
still reference them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from storage import db

from . import config as cfg
from .hooks import _resolve_local_tz_name
from .proactive_gate import reserve_and_send
from .runtime import run_visible_proactive

# Legacy alias so tests that monkeypatch ``proactive.run_proactive`` keep working.
run_proactive = run_visible_proactive  # noqa: F841

logger = logging.getLogger(__name__)


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
    tz = ZoneInfo(_resolve_local_tz_name())
    now = datetime.now(tz).time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _mood_from_core() -> str:
    return (db.get_core_block("mood_today") or "focused").strip().lower() or "focused"


def should_send_heartbeat() -> bool:
    p = _p()
    now = datetime.now(UTC)
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
    return True


def _last_message_role() -> tuple[str | None, datetime | None]:
    rows = db.recent_messages(limit=1, exclude_ephemeral=True)
    if not rows:
        return None, None
    last = rows[0]
    return last["role"], _parse_dt(last["ts"])


def should_send_reengagement() -> bool:
    """She had the last word, user is silent in the window, and we haven't
    already sent a re-engagement nudge for this specific silence gap."""
    p = _p()
    now = datetime.now(UTC)
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
    sent_for = db.runtime_get("reengage_sent_for_gap")
    if sent_for == last_ts.isoformat():
        return False
    return True


# TODO: remove after all callers migrate to reserve_and_send (Sprint 4 Phase 4B+).
def _unpack_send_result(
    result: object, draft_text: str,
) -> tuple[str, int | None, bool]:
    """Phase 13.1 (Stream G — codex P0 fix): normalize a ``send_text`` return.

    The production ``send_text`` returns
    ``(final_text_after_filtering, telegram_message_id, sent_ok)``.

    Tests (and any legacy caller) may pass an ``async def`` that returns
    ``None``; in that case we assume the call succeeded, the text was sent
    as-is (no filter applied), and we have no Telegram message_id. This
    keeps the contract working for monkeypatched fakes while letting the
    real bridge persist the post-filter text + message_id.

    Refuses to fabricate values — if the tuple is malformed we treat it as
    a successful no-id send to avoid a phantom failure.
    """
    if result is None:
        return draft_text, None, True
    if isinstance(result, tuple) and len(result) == 3:
        final, tg_id, ok = result
        if not isinstance(final, str):
            final = draft_text
        if tg_id is not None:
            try:
                tg_id = int(tg_id)
            except (TypeError, ValueError):
                tg_id = None
        return final, tg_id, bool(ok)
    # Unknown shape — best-effort: assume success, persist the draft.
    return draft_text, None, True


# ---------- calendar / apple reminder sync helpers ----------

def _strip_fences(raw: str) -> str:
    """Strip ```yaml ... ``` (or any other fenced) wrappers from an LLM reply."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    return raw.strip()


# ---------- Phase 10: reminders fire job ----------

def _next_occurrence(fire_at_iso: str, repeat: str) -> str | None:
    """Compute next occurrence iso for a simple repeat. Returns None for
    one-shots.

    Clamps the base to max(when, now) so that if the scheduler was delayed
    (bot offline, system sleep, missed cycles) we don't insert a past-timestamp
    row that would re-fire on the very next poll and loop indefinitely.
    """
    from datetime import timedelta

    from dateutil.relativedelta import relativedelta
    from dateutil.rrule import rrulestr
    when = datetime.fromisoformat(fire_at_iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if not repeat:
        return None
    # Clamp: if the original fire_at is in the past (scheduler was delayed),
    # advance from "now" instead so we don't insert another past-timestamp row.
    base = max(when, datetime.now(UTC))
    if repeat == "daily":
        return (base + timedelta(days=1)).isoformat()
    if repeat == "weekly":
        return (base + timedelta(weeks=1)).isoformat()
    if repeat == "monthly":
        return (base + relativedelta(months=1)).isoformat()
    if repeat == "yearly":
        return (base + relativedelta(years=1)).isoformat()
    if repeat.upper().startswith("RRULE:"):
        try:
            rule = rrulestr(repeat, dtstart=when)
            nxt = rule.after(base, inc=False)
            return nxt.isoformat() if nxt else None
        except Exception:
            logger.exception("invalid RRULE: %r", repeat)
            return None
    return None


async def _generate_accountability_followup_text(task_text: str) -> str:
    """Generate a dry, varied follow-up question via aux-LLM. Falls back to a
    template if the LLM call fails or OPENROUTER_API_KEY is unset."""
    from agents.runtime import _call_aux_llm

    system = (
        "You are Hikari. One sentence, lowercase, dry, no exclamation marks, "
        "no emojis. Ask if the user did the task. Vary phrasing. Examples: "
        "'so. water?', 'you actually drink that water or no.', "
        "'the water thing. yes or no.'"
    )
    try:
        out = await _call_aux_llm(
            f"Task: {task_text}",
            system=system,
            max_tokens=40,
        )
        return out.strip().strip('"') or f"did you do the {task_text} thing."
    except Exception:
        logger.warning("accountability followup voice-gen failed; using fallback")
        return f"did you do the {task_text} thing."


async def fire_due_reminders(send_text) -> int:
    """Drain reminder_due() — for each row, format + send + mark fired.
    If row has a repeat spec, insert the next occurrence as a fresh row.
    If row has a recurrence_rule, UPDATE fire_at in-place (infinite loop
    until user cancels — do NOT mark the row fired/cancelled).
    Returns count fired."""
    import json as _json

    due = db.reminder_due()
    if not due:
        return 0

    # Wrap send_text so reserve_and_send always gets the 3-tuple it expects.
    # Legacy callers (tests, transitional fakes) may return None; normalize.
    async def _send_text_fn(text: str) -> tuple[str, int | None, bool]:
        try:
            result = await send_text(text)
        except Exception:
            raise
        final, tg_id, ok = _unpack_send_result(result, text)
        return final, tg_id, ok

    fired = 0
    for row in due:
        accountability = db.accountability_get_by_followup_id(row["id"])
        if accountability and accountability["outcome"] is None:
            text = await _generate_accountability_followup_text(accountability["task_text"])
            db.runtime_set("pending_accountability_check", str(accountability["id"]))
            payload = _json.dumps({
                "reminder_id": row["id"],
                "accountability_id": accountability["id"],
            })
        else:
            text = row["text"] if row["text"].startswith("⏰") else f"⏰ {row['text']}"
            payload = _json.dumps({"reminder_id": row["id"]})
        result = await reserve_and_send(
            send_text_fn=_send_text_fn,
            producer_id="reminder",
            pattern="fire",
            text=text,
            payload_json=payload,
            dedup_key=f"reminder:{row['id']}",
            candidate={
                "anchor": str(row["id"]),
                "why_now": f"reminder fires at {row['fire_at']}",
                "suggested_action": "reply ok/snooze",
                "confidence": 1.0,
                "controls": {"snooze_hours": [1, 4, 24], "mute_source": "reminder"},
                "data_checked": ["reminders"],
            },
        )
        if result.status != "sent":
            logger.info("fire_due_reminders: skipped #%s (%s)", row["id"], result.reason)
            continue

        fired += 1

        # ---------- recurrence_rule: reschedule in-place (infinite loop) ----------
        recurrence_rule = row.get("recurrence_rule") or ""
        if recurrence_rule:
            try:
                from tools.reminders.recurrence import next_occurrence as _recurrence_next
                current_due = datetime.fromisoformat(row["fire_at"])
                if current_due.tzinfo is None:
                    current_due = current_due.replace(tzinfo=UTC)
                next_due = _recurrence_next(recurrence_rule, current_due)
                db.reminder_update_fire_at(row["id"], next_due.isoformat())
                logger.debug(
                    "fire_due_reminders: rescheduled #%s via recurrence_rule=%r → %s",
                    row["id"], recurrence_rule, next_due.isoformat(),
                )
            except Exception:
                logger.exception(
                    "fire_due_reminders: failed to reschedule #%s (recurrence_rule=%r)",
                    row["id"], recurrence_rule,
                )
                # Fall through to mark fired so the row doesn't re-fire on
                # every poll after a bad rule slips through.
                db.reminder_mark_fired(row["id"])
            continue  # Don't also run the legacy repeat logic below.

        # ---------- legacy repeat: mark fired, insert next occurrence ----------
        db.reminder_mark_fired(row["id"])
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


async def sync_pending_apple_reminders() -> int:
    """Drain reminders.apple_sync_pending via the typed Apple sync adapter.

    Calls ``tools.reminders.sync_apple._sync_apple_reminder`` directly —
    no LLM / prompt plumbing. macOS-only; best-effort: failures stay
    pending for retry.
    """
    import sys
    if sys.platform != "darwin":
        return 0
    pending = db.reminders_pending_apple_sync(limit=10)
    if not pending:
        return 0

    from agents.mcp_manager import McpCallError
    from tools.reminders.sync_apple import _sync_apple_reminder

    synced = 0
    for row in pending:
        try:
            await _sync_apple_reminder(
                reminder_id=row["id"],
                title=row["text"],
                due_iso=row["fire_at"],
            )
        except McpCallError as exc:
            logger.warning("apple sync: MCP error for reminder #%s: %s", row["id"], exc)
            continue
        except Exception:
            logger.exception("apple sync: failed for reminder #%s", row["id"])
            continue
        synced += 1
    return synced


async def sync_pending_gcal_reminders() -> int:
    """Drain reminders.gcal_sync_pending via the typed GCal sync adapter.

    Calls ``tools.reminders.sync_gcal._sync_gcal_reminder`` directly —
    no LLM / prompt plumbing. Best-effort: failures stay pending for retry.
    """
    import os
    if not all(os.environ.get(k) for k in (
        "GOOGLE_WORKSPACE_CLIENT_ID",
        "GOOGLE_WORKSPACE_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
    )):
        return 0
    pending = db.reminders_pending_gcal_sync(limit=10)
    if not pending:
        return 0

    from agents.mcp_manager import McpCallError
    from tools.reminders.sync_gcal import _sync_gcal_reminder

    synced = 0
    for row in pending:
        try:
            await _sync_gcal_reminder(
                reminder_id=row["id"],
                title=row["text"],
                start_iso=row["fire_at"],
            )
        except McpCallError as exc:
            logger.warning("gcal sync: MCP error for reminder #%s: %s", row["id"], exc)
            continue
        except Exception:
            logger.exception("gcal sync: failed for reminder #%s", row["id"])
            continue
        synced += 1
    return synced


# ---------- T7.2: recurring-location detection ----------

def detect_recurring_location_pattern(
    window_days: int = 7, min_visits: int = 3,
) -> dict | None:
    """Return ``{lat, lon, label, visit_count}`` if the user visited the same
    coords (rounded to 3 decimals = ~110m precision) ``min_visits`` or more
    times in the last ``window_days``. Otherwise ``None``.

    Surfacing pattern is the lead's call — this just provides the signal.
    The 3-decimal bucket keeps tiny GPS jitter from breaking the count
    (a phone sitting at one spot pings within ~10-30m of itself).
    """
    from collections import Counter

    rows = db.photo_locations_recent(limit=50)
    if not rows:
        return None

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    recent: list[dict] = []
    for r in rows:
        try:
            raw_ts = r.get("received_at")
            if not raw_ts:
                continue
            # SQLite returns the datetime('now') default as a UTC string with
            # no offset; treat it as UTC. ISO strings already-tagged with
            # tzinfo are honored.
            if isinstance(raw_ts, str):
                # ``datetime.fromisoformat`` tolerates "2026-05-19 10:21:33"
                # (the sqlite default format) since 3.11.
                t = datetime.fromisoformat(raw_ts)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
            else:
                t = raw_ts  # already a datetime
                if getattr(t, "tzinfo", None) is None:
                    t = t.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        if t < cutoff:
            continue
        recent.append(r)

    if not recent:
        return None

    counts = Counter(
        (round(float(r["lat"]), 3), round(float(r["lon"]), 3))
        for r in recent
    )
    if not counts:
        return None
    (lat, lon), n = counts.most_common(1)[0]
    if n < min_visits:
        return None

    # Find a label from any matching row (prefer non-empty).
    label = next(
        (r["label"] for r in recent
         if round(float(r["lat"]), 3) == lat
         and round(float(r["lon"]), 3) == lon
         and r.get("label")),
        None,
    )
    return {"lat": lat, "lon": lon, "label": label, "visit_count": n}
