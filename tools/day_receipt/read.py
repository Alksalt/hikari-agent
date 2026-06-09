"""``receipt_read`` — read the day receipt for today, this week, or a specific date.

Wraps the same storage layer the existing receipt tools use.
``period`` accepts: 'today' (default), 'week', or a date string 'YYYY-MM-DD'.
Returns Made / Moved / Learned / Avoided entries with ids + notes.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok


@tool(
    "receipt_read",
    "Read the day receipt. period='today' (default) returns today's entries; "
    "'week' returns the rolling 7-day window; 'YYYY-MM-DD' returns a specific date. "
    "Returns Made / Moved / Learned / Avoided entries with ids and notes.",
    {"period": str},
    annotations=annotations_for("receipt_read"),
)
async def receipt_read(args: dict[str, Any]) -> dict[str, Any]:
    period = (args.get("period") or "today").strip().lower()

    from tools.day_receipt import _db as _receipt_db
    from tools.day_receipt._render import RenderOptions, render_receipt, render_week

    try:
        if period in ("today", ""):
            target = date.today()
            r = _receipt_db.get_receipt(target)
            text = render_receipt(r, RenderOptions(width=46))
            if not text.strip():
                text = "nothing logged today."
            entries = [
                {
                    "id": e.id,
                    "category": e.category,
                    "text": e.text,
                    "tags": list(e.tags),
                    "created_at": e.created_at.isoformat(),
                }
                for e in r.entries
            ]
            data: dict[str, Any] = {
                "period": "today",
                "date": target.isoformat(),
                "entries": entries,
                "note": r.note,
            }
            return _ok(text, data=data)

        if period == "week":
            from datetime import timedelta
            end = date.today()
            receipts = []
            for offset in range(6, -1, -1):
                d = end - timedelta(days=offset)
                rr = _receipt_db.get_receipt(d)
                if rr.entries or rr.note:
                    receipts.append(rr)
            text = render_week(receipts, RenderOptions(width=46))
            if not text.strip():
                text = "nothing logged this week."
            week_entries = []
            for rr in receipts:
                for e in rr.entries:
                    week_entries.append({
                        "id": e.id,
                        "date": rr.receipt_date.isoformat(),
                        "category": e.category,
                        "text": e.text,
                        "tags": list(e.tags),
                    })
            return _ok(text, data={"period": "week", "entries": week_entries})

        # Try to parse as YYYY-MM-DD
        try:
            target = date.fromisoformat(period)
        except ValueError:
            return _ok(
                f"refused: period must be 'today', 'week', or 'YYYY-MM-DD', got {period!r}"
            )
        r = _receipt_db.get_receipt(target)
        text = render_receipt(r, RenderOptions(width=46))
        if not text.strip():
            text = f"nothing logged on {target.isoformat()}."
        entries = [
            {
                "id": e.id,
                "category": e.category,
                "text": e.text,
                "tags": list(e.tags),
                "created_at": e.created_at.isoformat(),
            }
            for e in r.entries
        ]
        return _ok(text, data={
            "period": target.isoformat(),
            "date": target.isoformat(),
            "entries": entries,
            "note": r.note,
        })

    except Exception as exc:
        return _ok(f"receipt_read error: {exc}")
