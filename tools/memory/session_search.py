"""session_search — FTS5 over the messages table (final sent text).

Untrusted output: user-message bodies may contain attacker URLs / prompts.
Always wrap via injection_guard.
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from agents import injection_guard
from storage import db as _db
from tools._response import ok as _ok


@tool(
    "session_search",
    "Search Hikari's chat-message history (verbatim final sent text on both sides). "
    "Args: query (required), limit (default 10), since_iso (optional ISO8601 lower bound), "
    "role (optional 'user' or 'assistant'). Use this when the lead asks 'what did i say "
    "about X' or 'when did i first mention Y' — NOT for stored facts (use recall).",
    {"query": str, "limit": int, "since_iso": str, "role": str},
)
async def session_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    limit = max(1, min(50, int(args.get("limit") or 10)))
    since_iso = (args.get("since_iso") or "").strip() or None
    role = (args.get("role") or "").strip().lower() or None
    if role and role not in ("user", "assistant"):
        role = None
    if not query:
        return _ok("session_search: empty query.", data={"hits": []})

    rows = _db.messages_fts_search(query, limit=limit, since_iso=since_iso, role=role)
    if not rows:
        body = injection_guard.wrap_untrusted(
            "session_search", f"no message matches for {query!r}."
        )
        return _ok(body, data={"hits": []}, presentation_hint="search_hits")

    lines = [f"top {len(rows)} message matches for {query!r}:"]
    for r in rows:
        snippet = (r["content"] or "")[:140].replace("\n", " ")
        lines.append(f"  [{r['role']} #{r['id']} @ {r['ts']}] {snippet}")
    body = injection_guard.wrap_untrusted("session_search", "\n".join(lines))
    return _ok(body, data={"hits": rows}, presentation_hint="search_hits")
