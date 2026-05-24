"""``receipt_today`` — structured (no formatting) snapshot of today's receipt."""
from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.day_receipt import _db as _receipt_db


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
    "receipt_today",
    (
        "Return today's receipt as structured data — counts per band, "
        "the optional top-of-receipt note, and every entry. No "
        "rendering. Use this when you want the raw facts; use "
        "receipt_print for the ASCII slip."
    ),
    {},
    annotations=annotations_for("receipt_today"),
)
async def receipt_today(args: dict[str, Any]) -> dict[str, Any]:
    d = date_cls.today()
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
