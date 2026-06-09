"""``checkin_control`` — trigger or skip the daily morning check-in.

action='run_now': queues the check-in to fire on the next scheduler tick
  by clearing the ``daily_checkin_last_fired_date`` dedup key AND setting
  a ``daily_checkin_force_run`` flag. The scheduler tick polls every
  ~5 minutes; the checkin will fire within that window.
  NOTE: ``maybe_run_daily_checkin`` also checks the target-time window
  (should_fire_now). To bypass time-window gating we additionally set
  ``daily_checkin_force_run`` which ``should_fire_now_or_forced`` reads.
  Since the scheduler's send_text callback (Telegram bot) isn't available
  here, run_now sets state and the scheduler fires within a minute.

action='skip_tomorrow': adds tomorrow to the skip_dates list via
  ``apply_schedule_edit`` (the same call the retired /checkin skip
  command made before Phase 5b).
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok

_VALID_ACTIONS = frozenset({"run_now", "skip_tomorrow"})
_FORCE_KEY = "daily_checkin_force_run"


@tool(
    "checkin_control",
    "Trigger or skip the daily morning check-in. "
    "action='run_now' — queues the check-in to fire on the next scheduler "
    "tick (within ~1 minute). The check-in question will arrive as a normal "
    "Telegram message. "
    "action='skip_tomorrow' — adds tomorrow to the skip list so the check-in "
    "does not fire tomorrow.",
    {"action": str},
    annotations=annotations_for("checkin_control"),
)
async def checkin_control(args: dict[str, Any]) -> dict[str, Any]:
    action = (args.get("action") or "").strip().lower()

    if action not in _VALID_ACTIONS:
        return _ok(
            f"refused: action must be one of {sorted(_VALID_ACTIONS)}, got {action!r}"
        )

    if action == "skip_tomorrow":
        from datetime import datetime as _dt
        from datetime import timedelta

        from agents.daily_checkin import _resolve_local_tz, apply_schedule_edit
        # Use HOME_TZ-aware "today" so the computed tomorrow matches the
        # same timezone _is_skipped_today uses for its comparison.
        tomorrow = (
            _dt.now(_resolve_local_tz()).date() + timedelta(days=1)
        ).isoformat()
        try:
            apply_schedule_edit({"kind": "skip", "date": tomorrow})
        except Exception as exc:
            return _ok(f"skip_tomorrow failed: {exc}")
        return _ok(
            f"checkin skipped for {tomorrow}.",
            data={"action": "skip_tomorrow", "skipped_date": tomorrow},
        )

    # action == 'run_now'
    # Refuse if the feature is disabled — a stuck flag would fire weeks later
    # on re-enable, which would be surprising.
    from agents.daily_checkin import _is_enabled
    if not _is_enabled():
        return _ok(
            "daily check-in is disabled in config — enable it first; nothing queued",
            data={"action": "run_now", "queued": False},
        )
    # Clear the "already fired today" dedup guard so should_fire_now passes.
    db.runtime_set("daily_checkin_last_fired_date", None)
    # Set the force flag so the scheduler fires even outside the normal
    # time window (daily_checkin.should_fire_now peeks this flag).
    db.runtime_set(_FORCE_KEY, "1")
    return _ok(
        "checkin queued — will fire within a minute via the scheduler.",
        data={"action": "run_now", "queued": True},
    )
