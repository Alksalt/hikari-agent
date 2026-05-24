"""``receipt_search`` — full-text search across entries and tags."""
from __future__ import annotations

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
    "receipt_search",
    (
        "Full-text search across entries and tags. Substring match — "
        "case-sensitive at the SQL layer per the LIKE clause. "
        "query: required search string. "
        "limit: max hits (default 25). "
        "Returns matches as a list of entries (id, date, category, "
        "text, tags, created_at), newest first."
    ),
    {"query": str, "limit": int},
    annotations=annotations_for("receipt_search"),
)
async def receipt_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("refused: receipt_search needs a query")
    raw_limit = args.get("limit")
    limit = int(raw_limit) if raw_limit else 25
    hits = _receipt_db.search(query, limit=limit)
    payload = {
        "query": query,
        "count": len(hits),
        "matches": [_entry_dict(e) for e in hits],
    }
    if not hits:
        return _ok(f"no entries matched {query!r}", data=payload)
    summary = f"found {len(hits)} entr{'y' if len(hits) == 1 else 'ies'} for {query!r}"
    return _ok(summary, data=payload)
