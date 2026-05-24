"""``receipt_print`` — render a single day's receipt as 46-col ASCII."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.day_receipt import _db as _receipt_db
from tools.day_receipt._render import RenderOptions, render_receipt
from tools.day_receipt._shared import parse_date


@tool(
    "receipt_print",
    (
        "Render the receipt for a date as ASCII text — looks like a "
        "thermal printer slip. "
        "date: optional ISO date, 'today', 'yesterday', or '-N'. "
        "Defaults to today. "
        "width: optional column width (default 46). "
        "Empty sections are skipped."
    ),
    {"date": str, "width": int},
    annotations=annotations_for("receipt_print"),
)
async def receipt_print(args: dict[str, Any]) -> dict[str, Any]:
    date_arg = args.get("date")
    raw_width = args.get("width")
    try:
        d = parse_date(date_arg)
    except ValueError as exc:
        return _ok(f"refused: invalid date {date_arg!r} ({exc})")
    width = int(raw_width) if raw_width else 46
    r = _receipt_db.get_receipt(d)
    text = render_receipt(r, RenderOptions(width=width))
    return _ok(text, data={"date": d.isoformat(), "width": width})
