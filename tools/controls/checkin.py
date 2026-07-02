"""``checkin_control`` — trigger or skip the daily morning brief.

Sprint 1: the daily brief (``agents/daily_brief.py``) replaced the old
morning_brief + daily_checkin ceremonies. This tool's surface (run_now /
skip_tomorrow) is unchanged; run_now now queues the brief instead.

action='run_now': queues the brief to fire on the next scheduler tick by
  clearing the ``daily_checkin_last_fired_date`` dedup key AND setting a
  ``daily_brief_force_run`` flag. The scheduler tick polls every ~5 minutes;
  the brief will fire within that window.
  NOTE: ``maybe_send_daily_brief`` also checks the target-time window
  (should_fire_now). To bypass time-window gating we additionally set
  ``daily_brief_force_run`` which ``daily_brief.should_fire_now`` peeks.
  Since the scheduler's send_text callback (Telegram bot) isn't available
  here, run_now sets state and the scheduler fires within a few minutes.

action='skip_tomorrow': adds tomorrow to the skip_dates list via
  ``apply_schedule_edit`` (the same call the retired /checkin skip
  command made before Phase 5b). ``daily_brief.should_fire_now`` reads the
  same ``daily_checkin_schedule`` core block, so this carries over to the
  brief automatically — no repoint needed here.
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok

_VALID_ACTIONS = frozenset({"run_now", "skip_tomorrow"})
_FORCE_KEY = "daily_brief_force_run"


@tool(
    "checkin_control",
    "Trigger or skip the daily morning brief. "
    "action='run_now' — queues the daily brief to fire on the next scheduler "
    "tick (within ~5 minutes). The brief will arrive as a normal Telegram "
    "message. "
    "action='skip_tomorrow' — adds tomorrow to the skip list so the brief "
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
    # on re-enable, which would be surprising. Sprint 1: run_now now queues
    # the daily brief, so the gate checks daily_brief.enabled (NOT
    # daily_checkin.enabled — that ceremony is permanently off, replaced by
    # the brief; see config/engagement.yaml).
    from agents import config as _cfg
    if not bool(_cfg.get("daily_brief.enabled", True)):
        return _ok(
            "daily brief is disabled in config — enable it first; nothing queued",
            data={"action": "run_now", "queued": False},
        )
    # Clear the "already fired today" dedup guard so should_fire_now passes.
    db.runtime_set("daily_checkin_last_fired_date", None)
    # Set the force flag so the scheduler fires even outside the normal
    # time window (daily_brief.should_fire_now peeks this flag).
    db.runtime_set(_FORCE_KEY, "1")
    return _ok(
        "daily brief queued — will fire within a few minutes via the scheduler.",
        data={"action": "run_now", "queued": True},
    )
