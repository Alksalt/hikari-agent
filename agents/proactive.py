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
from typing import Any
from zoneinfo import ZoneInfo

from storage import db

from . import config as cfg
from .hooks import _resolve_local_tz_name
from .proactive_gate import reserve_and_send

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


async def _push_action_message(
    rid: int,
    text: str,
    send_text_fn,
    reason: str,
) -> None:
    """Push a one-off Telegram message from an action reminder (summary or
    failure notice). Uses reserve_and_send so cadence/silence still apply."""
    import json as _json
    payload = _json.dumps({
        "reminder_id": rid, "kind": "action", "reason": reason,
    })
    await reserve_and_send(
        send_text_fn=send_text_fn,
        producer_id="reminder",
        pattern=f"action_{reason}",
        text=text,
        payload_json=payload,
        dedup_key=f"reminder_action:{rid}:{reason}",
        candidate={
            "anchor": str(rid),
            "why_now": f"scheduled action {reason}",
            "suggested_action": "reply ok/snooze",
            "confidence": 1.0,
            "controls": {"snooze_hours": [], "mute_source": "reminder"},
            "data_checked": ["reminders"],
        },
    )


def _reschedule_action_row(row: dict) -> None:
    """Advance an action reminder's fire_at via its recurrence_rule. Falls
    through silently if the rule is malformed — the row keeps its old
    fire_at so the next poll just retries it."""
    recurrence_rule = row.get("recurrence_rule") or ""
    if not recurrence_rule:
        return
    try:
        from tools.reminders.recurrence import next_occurrence as _recurrence_next
        current_due = datetime.fromisoformat(row["fire_at"])
        if current_due.tzinfo is None:
            current_due = current_due.replace(tzinfo=UTC)
        new_due = _recurrence_next(recurrence_rule, current_due)
        db.reminder_update_fire_at(row["id"], new_due.isoformat())
        db.reminder_requeue_sync(row["id"])
    except Exception:
        logger.exception(
            "action reminder #%s: failed to reschedule (rule=%r)",
            row["id"], recurrence_rule,
        )


async def _fire_action_reminder(row: dict, send_text_fn) -> int:
    """Handle one action-mode reminder: defer on quiet-hours / user-turn
    contention, otherwise invoke ``run_scheduled_action`` and advance state.

    Returns 1 if the row was processed this poll (success OR failure),
    0 if deferred (caller skips it but does not count it as fired).
    """
    rid = int(row["id"])
    seed = row.get("seed_prompt")
    if not seed:
        logger.error("action reminder #%s has no seed_prompt; cancelling", rid)
        db.reminder_set_status(rid, "cancelled")
        return 1

    # Quiet-hours: defer 15 min, don't drop. User explicitly opted into the
    # schedule but quiet-hours is the global noise gate for proactive sends.
    try:
        from agents.engagement.guard import should_wake
        if not should_wake(source_id="reminder_action"):
            new_due = datetime.now(UTC) + timedelta(minutes=15)
            db.reminder_update_fire_at(rid, new_due.isoformat())
            logger.info(
                "action reminder #%s deferred 15 min (quiet hours)", rid,
            )
            return 0
    except Exception:
        logger.warning(
            "action reminder #%s: quiet-hours check raised; proceeding "
            "(user explicitly scheduled)", rid, exc_info=True,
        )

    # User-turn contention: defer 2 min. We must NOT preempt a live user
    # interaction — Hikari's chat-turn lock is single-conversation; running
    # two SDK turns against the same session_id would fork state.
    from agents.runtime import _RUN_LOCK
    if _RUN_LOCK.locked():
        new_due = datetime.now(UTC) + timedelta(minutes=2)
        db.reminder_update_fire_at(rid, new_due.isoformat())
        logger.info(
            "action reminder #%s deferred 2 min (user turn in progress)", rid,
        )
        return 0

    fires_done = int(row.get("fires_done") or 0)
    max_fires = int(row.get("max_fires") or 0)
    seed_with_context = (
        f"{seed}\n\n[scheduled action #{fires_done + 1}"
        + (f"/{max_fires}" if max_fires else "")
        + f"; fire_at={row['fire_at']}]"
    )

    from agents.runtime import run_scheduled_action

    try:
        await run_scheduled_action(
            seed_with_context,
            timeout_s=row.get("timeout_s"),
            max_budget_usd=row.get("budget_usd_per_fire"),
        )
    except Exception as exc:
        logger.exception("action reminder #%s fire failed", rid)
        n_failures = db.reminder_increment_failures(rid)
        if n_failures >= 3:
            db.reminder_set_status(rid, "cancelled")
            try:
                await _push_action_message(
                    rid,
                    f"scheduled action cancelled — 3 failures in a row "
                    f"(last: {type(exc).__name__}).",
                    send_text_fn,
                    reason="failed_cap",
                )
            except Exception:
                logger.exception(
                    "action reminder #%s: failure-surface send failed", rid,
                )
        else:
            # Try again at the next cadence step so transient errors clear.
            _reschedule_action_row(row)
        return 1

    # Success path
    new_fires_done = db.reminder_increment_fires_done(rid)
    db.reminder_reset_failures(rid)

    if max_fires and new_fires_done >= max_fires:
        summary_prompt = row.get("summary_prompt")
        if summary_prompt:
            try:
                summary_text = await run_scheduled_action(summary_prompt)
                if summary_text and summary_text.strip():
                    await _push_action_message(
                        rid, summary_text.strip(), send_text_fn,
                        reason="summary",
                    )
            except Exception:
                logger.exception(
                    "action reminder #%s: summary turn failed", rid,
                )
        db.reminder_set_status(rid, "fired")
    else:
        _reschedule_action_row(row)
    return 1


async def fire_due_reminders(send_text) -> int:
    """Drain reminder_due() — for each row, format + send + mark fired.
    If row has a repeat spec, insert the next occurrence as a fresh row.
    If row has a recurrence_rule, UPDATE fire_at in-place (infinite loop
    until user cancels — do NOT mark the row fired/cancelled).

    Action-mode rows (``kind='action'``) branch to ``_fire_action_reminder``
    which invokes ``run_scheduled_action`` instead of pushing static text.
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
        # Action-mode reminders: invoke Hikari autonomously, do not push
        # static text. _fire_action_reminder handles defer / failure cap /
        # summary / reschedule on its own.
        if row.get("kind") == "action":
            fired += await _fire_action_reminder(row, _send_text_fn)
            continue

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
                db.reminder_requeue_sync(row["id"])
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


# Per-reminder consecutive gcal-sync failure counter. In-process (reset on
# restart) on purpose: transient blips self-heal within a few ticks, and a
# restart legitimately re-surfaces a still-broken mirror. Bounded — entries
# are popped on success or when the mirror is abandoned.
_GCAL_SYNC_FAILS: dict[int, int] = {}
_GCAL_SYNC_MAX_RETRIES = 3


async def _notify_gcal_sync_failed(row: dict[str, Any], exc: Exception) -> None:
    """Tell the owner, once, that a reminder's Calendar mirror was abandoned.

    The reminder itself still fires via Telegram — only the optional Google
    Calendar event failed to save. Surfacing this is the whole point: a silent
    mirror failure is what let Hikari claim a calendar write that never landed.
    """
    rid = row["id"]
    title = (row.get("text") or "reminder").strip()
    msg = (
        f'couldn\'t save "{title}" to google calendar after '
        f"{_GCAL_SYNC_MAX_RETRIES} tries — reminder #{rid}. the reminder still "
        f"fires, the calendar event didn't. last error: {type(exc).__name__}."
    )
    try:
        import os

        from telegram import Bot

        from agents.messaging import send_and_persist
        from agents.runtime import owner_id

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            logger.error(
                "gcal sync: TELEGRAM_BOT_TOKEN missing — cannot notify owner "
                "that reminder #%s failed to mirror to calendar",
                rid,
            )
            return
        # Same direct-Bot path the media_outbox drain job uses: this runs from a
        # scheduler job with no send_text_fn in scope.
        result = await send_and_persist(
            bot=Bot(token=token),
            chat_id=owner_id(),
            text=msg,
            source="proactive",
            skip_choreography=True,
        )
        if not result.ok:
            logger.error(
                "gcal sync: could not deliver mirror-failure notice for reminder #%s",
                rid,
            )
    except Exception:
        logger.exception(
            "gcal sync: error while notifying owner about reminder #%s", rid
        )


async def sync_pending_gcal_reminders() -> int:
    """Drain reminders.gcal_sync_pending via the typed GCal sync adapter.

    Calls ``tools.reminders.sync_gcal._sync_gcal_reminder`` directly —
    no LLM / prompt plumbing. Best-effort, but NOT silent: after
    ``_GCAL_SYNC_MAX_RETRIES`` consecutive failures for a reminder the mirror
    is abandoned (``gcal_sync_pending`` cleared so the job stops hammering) and
    the owner is told via Telegram — instead of retrying forever in silence.
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
        rid = row["id"]
        try:
            await _sync_gcal_reminder(
                reminder_id=rid,
                title=row["text"],
                start_iso=row["fire_at"],
            )
        except Exception as exc:
            fails = _GCAL_SYNC_FAILS.get(rid, 0) + 1
            _GCAL_SYNC_FAILS[rid] = fails
            log = logger.warning if isinstance(exc, McpCallError) else logger.exception
            log(
                "gcal sync: attempt %d/%d failed for reminder #%s: %s",
                fails, _GCAL_SYNC_MAX_RETRIES, rid, exc,
            )
            if fails >= _GCAL_SYNC_MAX_RETRIES:
                # Stop the silent retry loop and surface the failure to the owner.
                db.reminder_clear_gcal_pending(rid)
                _GCAL_SYNC_FAILS.pop(rid, None)
                await _notify_gcal_sync_failed(row, exc)
            continue
        _GCAL_SYNC_FAILS.pop(rid, None)
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
