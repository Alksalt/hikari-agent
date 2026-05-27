"""``reminder_create`` — schedule a future poke.

Optionally syncs to Google Calendar / Apple Reminders. The sync work
is queued in the DB (``gcal_sync_pending`` / ``apple_sync_pending``)
and drained asynchronously by the background scheduler — this tool
returns immediately.
"""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import tool

from agents import config as _cfg
from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.reminders._shared import _VALID_REPEAT, _parse_iso
from tools.reminders.recurrence import validate_rule as _validate_recurrence_rule

logger = logging.getLogger(__name__)

# Default budget cap when the user doesn't specify one per fire. Aligned
# with config/engagement.yaml runtime.scheduled_action_max_budget_usd.
_DEFAULT_ACTION_BUDGET_USD = 0.40
# Hard total-cost ceiling at create time: max_fires × budget_usd_per_fire
# must not exceed this. Prevents a malformed schedule from silently
# burning through the subscription budget.
_DEFAULT_TOTAL_BUDGET_CAP_USD = 5.0


@tool(
    "reminder_create",
    (
        "Schedule a reminder that fires as a real Telegram push at when_iso. "
        "when_iso MUST be a fully-resolved ISO-8601 timestamp (UTC or with tz "
        "offset) — the parser refuses anything else. If the user gives a "
        "relative time, YOU compute the ISO from the `# now` block injected "
        "at the top of your context. Do not call this tool with natural-"
        "language time strings like 'in 1h' or 'tomorrow'. "
        "Examples: "
        "(EN) user 'remind me in 5 min to stretch', `# now` utc 2026-05-20T18:00:00+00:00 "
        "→ when_iso='2026-05-20T18:05:00+00:00', text='stretch'. "
        "(UK) user 'нагадай через годину написати маріку', `# now` utc 2026-05-20T18:00:00+00:00 "
        "→ when_iso='2026-05-20T19:00:00+00:00', text='написати маріку'. "
        "(RU) user 'напомни мне завтра в 9 позвонить маме', "
        "`# now` local 2026-05-20 18:00 Europe/Kyiv → "
        "when_iso='2026-05-21T09:00:00+03:00', text='позвонить маме'. "
        "text is what Hikari will say when the reminder fires. "
        "lead_minutes (default 0) fires N minutes BEFORE when_iso — useful for "
        "events ('remind me 1h before my 14:00 meeting' → when_iso=14:00, "
        "lead_minutes=60, fires at 13:00). "
        "repeat one of {daily, weekly, monthly, yearly} for simple repeats, or "
        "an RRULE string for advanced. "
        "recurrence: structured recurrence rule for smart rescheduling. "
        "Grammar: 'daily' | 'weekly:MON,WED,FRI' | 'monthly:1' | 'monthly:last' "
        "| 'yearly:MM-DD' | 'every_n_days:N'. "
        "When set the reminder auto-reschedules after each fire — it keeps "
        "looping until the user explicitly cancels it. "
        "sync_to_gcal=True queues a Google Calendar mirror (non-blocking — the "
        "GCal sync job drains the queue separately). "
        "sync_to_apple=True queues an Apple Reminders mirror (macOS only, "
        "non-blocking). "
        "kind='action' creates an autonomous-action reminder: when fire_at hits, "
        "Hikari wakes (via run_scheduled_action) and executes the work in "
        "seed_prompt. Requires recurrence + max_fires + seed_prompt. Useful for "
        "time-spanning tasks like 'write a Notion row every 20 min for 2 hours'. "
        "max_fires caps the number of fires; after the last fire, summary_prompt "
        "(if set) runs as a final wrap-up turn whose text IS pushed to Telegram. "
        "budget_usd_per_fire and timeout_s override the defaults (0.40 USD / 180 s) "
        "per fire. The total cost (max_fires × budget) is capped at create time. "
        "kind='text' (default) is the existing static-text reminder."
    ),
    {"when_iso": str, "text": str, "lead_minutes": int, "repeat": str,
     "recurrence": str, "sync_to_gcal": bool, "sync_to_apple": bool,
     "kind": str, "seed_prompt": str, "max_fires": int,
     "summary_prompt": str, "budget_usd_per_fire": float, "timeout_s": int},
    annotations=annotations_for("reminder_create"),
)
async def reminder_create(args: dict[str, Any]) -> dict[str, Any]:
    when_iso = (args.get("when_iso") or "").strip()
    text = (args.get("text") or "").strip()
    lead_minutes = int(args.get("lead_minutes") or 0)
    repeat = (args.get("repeat") or "").strip() or None
    recurrence = (args.get("recurrence") or "").strip() or None
    sync_to_gcal = bool(args.get("sync_to_gcal", True))
    # Default True on macOS; False elsewhere (EventKit is Apple-only).
    sync_to_apple = bool(args.get("sync_to_apple", sys.platform == "darwin"))

    # Action-mode arguments.
    kind = (args.get("kind") or "text").strip().lower()
    seed_prompt = (args.get("seed_prompt") or "").strip() or None
    summary_prompt = (args.get("summary_prompt") or "").strip() or None
    max_fires_raw = args.get("max_fires")
    max_fires = int(max_fires_raw) if max_fires_raw not in (None, "", 0) else None
    budget_raw = args.get("budget_usd_per_fire")
    budget_usd_per_fire = float(budget_raw) if budget_raw not in (None, "", 0) else None
    timeout_raw = args.get("timeout_s")
    timeout_s = int(timeout_raw) if timeout_raw not in (None, "", 0) else None

    if kind not in {"text", "action"}:
        return _ok(f"refused: kind must be 'text' or 'action', got {kind!r}")

    if not text:
        return _ok("refused: empty text")
    when = _parse_iso(when_iso)
    if when is None:
        return _ok(f"refused: cannot parse when_iso={when_iso!r}")
    if when - timedelta(minutes=lead_minutes) <= datetime.now(UTC):
        return _ok("refused: fire time is in the past")
    if repeat not in _VALID_REPEAT and not repeat.upper().startswith("RRULE:"):
        return _ok(
            f"refused: repeat={repeat!r} must be one of {{daily,weekly,monthly,yearly}} "
            f"or an RRULE string"
        )
    if recurrence is not None:
        try:
            _validate_recurrence_rule(recurrence)
        except ValueError as exc:
            return _ok(f"refused: {exc}")

    # Action-mode validation.
    if kind == "action":
        if not seed_prompt:
            return _ok("refused: kind='action' requires seed_prompt")
        if not recurrence:
            return _ok("refused: kind='action' requires recurrence")
        if not max_fires or max_fires < 1:
            return _ok("refused: kind='action' requires max_fires >= 1")
        # Cost cap: max_fires × per-fire budget must not exceed the
        # configured total ceiling. Refuse loudly — no silent truncation.
        per_fire = budget_usd_per_fire or _DEFAULT_ACTION_BUDGET_USD
        cap = float(_cfg.get("reminders.action_max_total_usd",
                             _DEFAULT_TOTAL_BUDGET_CAP_USD))
        total = max_fires * per_fire
        if total > cap:
            return _ok(
                f"refused: total budget ${total:.2f} ({max_fires} × ${per_fire:.2f}) "
                f"exceeds cap ${cap:.2f}. lower max_fires or budget_usd_per_fire."
            )
        # Action reminders are background work — they must not also mirror
        # to user-visible calendars/apple-reminders (those represent the
        # *schedule* of fires, not the work). Force both off.
        sync_to_gcal = False
        sync_to_apple = False

    rid = db.reminder_insert(
        fire_at=when.isoformat(),
        text=text,
        lead_minutes=lead_minutes,
        repeat=repeat,
        recurrence_rule=recurrence,
        gcal_sync_pending=sync_to_gcal,
        apple_sync_pending=sync_to_apple,
        kind=kind,
        seed_prompt=seed_prompt,
        max_fires=max_fires,
        summary_prompt=summary_prompt,
        budget_usd_per_fire=budget_usd_per_fire,
        timeout_s=timeout_s,
    )
    if kind == "action":
        return _ok(
            f"action reminder #{rid} set for {when.isoformat()} "
            f"(recurrence {recurrence}, max_fires {max_fires}, "
            f"per-fire ${budget_usd_per_fire or _DEFAULT_ACTION_BUDGET_USD:.2f}, "
            f"summary {'yes' if summary_prompt else 'no'})",
            data={"id": rid, "kind": "action"},
        )
    return _ok(
        f"reminder #{rid} set for {when.isoformat()} "
        f"(lead {lead_minutes}m, repeat {repeat or 'none'}, "
        f"recurrence {recurrence or 'none'}, "
        f"gcal_sync {'queued' if sync_to_gcal else 'skipped'}, "
        f"apple_sync {'queued' if sync_to_apple else 'skipped'})",
        data={"id": rid},
    )
