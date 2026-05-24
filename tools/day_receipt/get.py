"""``receipt_get`` — structured snapshot of a specific date's receipt."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.day_receipt import _db as _receipt_db
from tools.day_receipt._shared import parse_date


def _entry_dict(e) -> dict[str, Any]:
    return {
        "id": e.id,
        "date": e.receipt_date.isoformat(),
        "category": e.category,
        "text": e.text,
        "tags": list(e.tags),
        "created_at": e.created_at.isoformat(),
    }


@tool(
    "receipt_get",
    (
        "Get a receipt for a specific date as structured data. "
        "date: ISO date (YYYY-MM-DD), 'today', 'yesterday', or '-N' "
        "(e.g. -2 = two days ago). Returns counts per band, the "
        "optional note, and every entry. No rendering."
    ),
    {"date": str},
    annotations=annotations_for("receipt_get"),
)
async def receipt_get(args: dict[str, Any]) -> dict[str, Any]:
    date_arg = args.get("date") or ""
    try:
        d = parse_date(date_arg)
    except ValueError as exc:
        return _ok(f"refused: invalid date {date_arg!r} ({exc})")
    r = _receipt_db.get_receipt(d)
    payload = {
        "date": d.isoformat(),
        "note": r.note,
        "counts": dict(r.counts),
        "entries": [_entry_dict(e) for e in r.entries],
    }
    summary = (
        f"{d.isoformat()}: "
        + " ".join(f"{k}:{v}" for k, v in payload["counts"].items())
        + (f" (note: {r.note})" if r.note else "")
    )
    return _ok(summary, data=payload)
