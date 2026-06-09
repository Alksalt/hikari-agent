"""``diary_read`` — read Hikari's recent diary entries.

Wraps ``db.diary_entries_recent``, the same store the /diary command
and ``cockpit.format_diary`` use. Optional ``days`` arg controls how
many entries to return (default 7); ``page`` offsets for older entries.
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok

_PAGE_SIZE = 5


@tool(
    "diary_read",
    "Read Hikari's recent diary entries. "
    "days=7 (default) returns up to 7 entries from the last 7 diary sessions. "
    "page=0 (default) is most recent; page=1 is the next older page, etc. "
    "Returns entries with dates and body text.",
    {"days": int, "page": int},
    annotations=annotations_for("diary_read"),
)
async def diary_read(args: dict[str, Any]) -> dict[str, Any]:
    days_raw = args.get("days")
    days = int(days_raw) if days_raw and int(days_raw) > 0 else 7
    page_raw = args.get("page")
    page = int(page_raw) if page_raw is not None and int(page_raw) >= 0 else 0

    limit = max(days, _PAGE_SIZE * (page + 2))
    all_entries = db.diary_entries_recent(limit=limit)

    total = len(all_entries)
    if total == 0:
        return _ok("no diary entries yet.", data={"entries": []})

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    start = page * _PAGE_SIZE
    page_entries = all_entries[start: start + _PAGE_SIZE]

    if not page_entries:
        return _ok(
            f"no entries on page {page + 1} (total pages: {total_pages}).",
            data={"entries": []},
        )

    lines = [f"diary — page {page + 1}/{total_pages}:"]
    for entry in page_entries:
        ts = (entry.get("entry_date") or "")[:10]
        body = (entry.get("body") or "")[:300]
        lines.append(f"\n{ts}")
        lines.append(f"  {body}")

    data_entries = [
        {
            "entry_date": e.get("entry_date"),
            "body": e.get("body"),
            "sentiment": e.get("sentiment"),
        }
        for e in page_entries
    ]
    return _ok("\n".join(lines), data={"entries": data_entries, "total": total})
