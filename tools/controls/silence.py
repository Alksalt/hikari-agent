"""``set_silence`` — silence proactive messages for N minutes (or clear).

Writes the same ``silence_until`` runtime_state key the /silence and
/unsilence Telegram commands write, so the proactive gate's silence
check keeps working without any changes.

Args:
  minutes: int > 0 — silence proactives for this many minutes.
  off: bool — if True, clear the silence immediately (equivalent to
       /unsilence). ``minutes`` is ignored when off=True.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok


def _until_local(until_utc: datetime) -> str:
    """Format expiry timestamp in local tz, best-effort."""
    try:
        from zoneinfo import ZoneInfo
        tz_name = os.environ.get("HOME_TZ") or "UTC"
        local = until_utc.astimezone(ZoneInfo(tz_name))
        return local.strftime(f"%Y-%m-%d %H:%M {tz_name}")
    except Exception:
        return until_utc.strftime("%Y-%m-%d %H:%M UTC")


@tool(
    "set_silence",
    "Silence proactive messages for a given number of minutes, or clear an "
    "active silence. "
    "minutes: int > 0 — how long to silence proactives. "
    "off=True — clear the silence immediately (resume proactives). "
    "Returns confirmation with the until-time when silencing, or a clear "
    "acknowledgment when unsilencing.",
    {"minutes": int, "off": bool},
    annotations=annotations_for("set_silence"),
)
async def set_silence(args: dict[str, Any]) -> dict[str, Any]:
    off = bool(args.get("off", False))

    if off:
        db.runtime_set("silence_until", None)
        return _ok("ok. proactives back on.")

    minutes_raw = args.get("minutes")
    try:
        minutes = int(minutes_raw) if minutes_raw is not None else 0
    except (ValueError, TypeError):
        minutes = 0

    if minutes <= 0:
        return _ok(
            "refused: minutes must be > 0, or pass off=True to clear silence"
        )

    until_utc = datetime.now(UTC) + timedelta(minutes=minutes)
    # Write the same key the /silence command writes
    db.runtime_set("silence_until", until_utc.isoformat())

    expiry_str = _until_local(until_utc)
    return _ok(
        f"ok. quiet for {minutes} minutes (until {expiry_str}).",
        data={"silence_until": until_utc.isoformat(), "minutes": minutes},
    )
