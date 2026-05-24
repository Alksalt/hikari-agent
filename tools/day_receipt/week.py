"""``receipt_week`` — render the last N days of receipts as ASCII."""
from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.day_receipt import _db as _receipt_db
from tools.day_receipt._render import RenderOptions, render_week


@tool(
    "receipt_week",
    (
        "Render the last N days of receipts as ASCII. Empty days are "
        "skipped — only days with at least one entry or a note are "
        "included. "
        "days: how many trailing days to include (default 7). "
        "width: optional column width (default 46)."
    ),
    {"days": int, "width": int},
    annotations=annotations_for("receipt_week"),
)
async def receipt_week(args: dict[str, Any]) -> dict[str, Any]:
    raw_days = args.get("days")
    raw_width = args.get("width")
    days = int(raw_days) if raw_days else 7
    width = int(raw_width) if raw_width else 46
    end = date_cls.today()
    receipts = []
    for offset in range(days - 1, -1, -1):
        d = end - timedelta(days=offset)
        r = _receipt_db.get_receipt(d)
        if r.entries or r.note:
            receipts.append(r)
    text = render_week(receipts, RenderOptions(width=width))
    return _ok(text, data={"days": days, "width": width, "non_empty_days": len(receipts)})
