"""``receipt_add`` — log a single entry into one of the four bands."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.day_receipt import _db as _receipt_db
from tools.day_receipt._shared import CATEGORIES, parse_date


@tool(
    "receipt_add",
    (
        "Add a single entry to the day's receipt. Categories are fixed: "
        "'made' (created / shipped), 'moved' (advanced but not done), "
        "'learned' (insights), 'avoided' (chose-not-to — also valuable "
        "signal). "
        "category: one of made / moved / learned / avoided. "
        "text: short prose describing the entry. "
        "date: optional ISO date (YYYY-MM-DD), 'today', 'yesterday', or "
        "'-N'. Defaults to today. "
        "tags: optional list of short topic tags for later search."
    ),
    {"category": str, "text": str, "date": str, "tags": list},
)
async def receipt_add(args: dict[str, Any]) -> dict[str, Any]:
    category = (args.get("category") or "").strip().lower()
    text = (args.get("text") or "").strip()
    date_arg = args.get("date")
    raw_tags = args.get("tags") or []
    if category not in CATEGORIES:
        return _ok(f"refused: category must be one of {list(CATEGORIES)}")
    if not text:
        return _ok("refused: text is empty")
    d = parse_date(date_arg)
    tags_tuple = tuple(str(t).strip() for t in raw_tags if str(t).strip())
    try:
        entry_id = _receipt_db.add_entry(
            category,  # type: ignore[arg-type]
            text,
            d,
            tags=tags_tuple,
        )
    except ValueError as exc:
        return _ok(f"refused: {exc}")
    return _ok(
        f"logged [{category}] #{entry_id} on {d.isoformat()}: {text}",
        data={
            "ok": True,
            "id": entry_id,
            "date": d.isoformat(),
            "category": category,
        },
    )
