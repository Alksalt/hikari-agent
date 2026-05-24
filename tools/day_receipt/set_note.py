"""``receipt_set_note`` — set or clear the top-of-receipt note for a date."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.day_receipt import _db as _receipt_db
from tools.day_receipt._shared import parse_date


@tool(
    "receipt_set_note",
    (
        "Set or clear the top-of-receipt note for a date (mood, "
        "weather, one-liner — printed under the header). "
        "text: required. Pass an empty string to clear an existing "
        "note. "
        "date: optional ISO date, 'today', 'yesterday', or '-N'. "
        "Defaults to today."
    ),
    {"text": str, "date": str},
    annotations=annotations_for("receipt_set_note"),
)
async def receipt_set_note(args: dict[str, Any]) -> dict[str, Any]:
    text = args.get("text")
    if text is None:
        return _ok("refused: receipt_set_note needs text (use '' to clear)")
    date_arg = args.get("date")
    try:
        d = parse_date(date_arg)
    except ValueError as exc:
        return _ok(f"refused: invalid date {date_arg!r} ({exc})")
    _receipt_db.set_note(d, text)
    cleared = not text.strip()
    summary = (
        f"cleared note for {d.isoformat()}" if cleared
        else f"note set for {d.isoformat()}: {text.strip()}"
    )
    return _ok(summary, data={"ok": True, "date": d.isoformat(), "cleared": cleared})
