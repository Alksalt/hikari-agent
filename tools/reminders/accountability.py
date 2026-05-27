"""Accountability reminders: a primary push + a follow-up check in one atomic operation."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.reminders._shared import _parse_iso

logger = logging.getLogger(__name__)

_MIN_CHECK_MINUTES = 5
_MAX_CHECK_MINUTES = 1440 * 7  # one week


@tool(
    "accountability_create",
    (
        "Set an accountability reminder: a primary push at when_iso PLUS a "
        "follow-up check ~check_after_minutes later that asks if the user "
        "actually did it. Use when the user says 'push me to X', 'make me X', "
        "'hold me to X', 'remind me to X and check later', or similar "
        "accountability framing. For a one-shot reminder with NO follow-up, "
        "use reminder_create instead. "
        "when_iso MUST be a fully-resolved ISO-8601 timestamp computed from "
        "the `# now` block — never a natural-language string. task_text is "
        "the thing the user committed to ('drink water', 'submit the form'). "
        "check_after_minutes defaults to 180 (3h)."
    ),
    {"when_iso": str, "task_text": str, "check_after_minutes": int},
    annotations=annotations_for("accountability_create"),
)
async def accountability_create(args: dict[str, Any]) -> dict[str, Any]:
    when_iso = (args.get("when_iso") or "").strip()
    task_text = (args.get("task_text") or "").strip()
    check_after_minutes = int(args.get("check_after_minutes") or 180)

    if not task_text:
        return _ok("refused: task_text must not be empty")

    when = _parse_iso(when_iso)
    if when is None:
        return _ok(f"refused: cannot parse when_iso={when_iso!r}")

    if when <= datetime.now(UTC):
        return _ok("refused: primary fire time is in the past")

    if not (_MIN_CHECK_MINUTES <= check_after_minutes <= _MAX_CHECK_MINUTES):
        return _ok(
            f"refused: check_after_minutes={check_after_minutes} must be "
            f"between {_MIN_CHECK_MINUTES} and {_MAX_CHECK_MINUTES}"
        )

    follow_up_at = when + timedelta(minutes=check_after_minutes)

    rid, follow_rid, item_id = db.accountability_create_atomic(
        when_iso_primary=when.isoformat(),
        when_iso_followup=follow_up_at.isoformat(),
        task_text=task_text,
    )

    return _ok(
        f"accountability set: reminder #{rid} at {when.isoformat()}, "
        f"follow-up #{follow_rid} at {follow_up_at.isoformat()} "
        f"(+{check_after_minutes}m), item #{item_id}",
        data={
            "id": item_id,
            "reminder_id": rid,
            "follow_up_reminder_id": follow_rid,
        },
    )


@tool(
    "accountability_resolve",
    (
        "Mark an accountability check resolved with outcome 1 (did it) or 0 (didn't). "
        "Call ONLY when the user's reply to the follow-up question is unambiguous. "
        "Clears pending_accountability_check runtime state."
    ),
    {"id": int, "outcome": int},
    annotations=annotations_for("accountability_resolve"),
)
async def accountability_resolve(args: dict[str, Any]) -> dict[str, Any]:
    item_id = int(args.get("id") or 0)
    outcome = args.get("outcome")

    if outcome not in (0, 1):
        return _ok(f"refused: outcome must be 0 or 1, got {outcome!r}")

    item = db.accountability_get(item_id)
    if item is None:
        return _ok(f"refused: accountability item #{item_id} not found")

    db.accountability_resolve(item_id, int(outcome))
    pending_raw = db.runtime_get("pending_accountability_check")
    if pending_raw == str(item_id):
        db.runtime_set("pending_accountability_check", None)

    return _ok("logged.", data={"id": item_id, "outcome": outcome})
