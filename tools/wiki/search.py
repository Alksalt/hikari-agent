"""wiki_search — fuzzy filename + full-text search across the user's vault."""
from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.wiki._shared import _vault


@tool(
    "wiki_search",
    "Search the USER'S OWN Obsidian wiki (their personal notes vault) by query. "
    "Matches filenames (fuzzy) and full-text. Returns top matches with paths and "
    "short excerpts so you can pick one to `wiki_read`. "
    "e.g. user says 'what did I write about meria last month' → wiki_search('meria'). "
    "Don't use this for Hikari's private memory of chats (use `recall`) or for "
    "current-events / public-web lookup (use the `research` subagent).",
    {"query": str, "limit": int},
)
async def wiki_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    limit = max(1, min(20, int(args.get("limit") or 5)))
    if not query:
        return _ok("wiki_search: empty query.")

    q_lower = query.lower()
    q_tokens = [t for t in re.findall(r"\w+", q_lower) if len(t) > 2]

    hits: list[tuple[float, str, str]] = []  # (score, rel_path, excerpt)
    v = _vault()
    for note_name, rel_path in v.md_file_index.items():
        name_score = 0.0
        name_lower = note_name.lower()
        if q_lower in name_lower:
            name_score = 3.0
        elif any(t in name_lower for t in q_tokens):
            name_score = 1.5

        content_score = 0.0
        excerpt = ""
        try:
            text = v.get_readable_text(note_name) or ""
        except Exception:  # noqa: BLE001
            text = ""
        text_lower = text.lower()
        if q_lower in text_lower:
            content_score = 2.0
            idx = text_lower.find(q_lower)
            start = max(0, idx - 60)
            end = min(len(text), idx + len(q_lower) + 60)
            excerpt = text[start:end].replace("\n", " ")
        elif q_tokens:
            matched = sum(1 for t in q_tokens if t in text_lower)
            if matched:
                content_score = 0.5 * matched

        total = name_score + content_score
        if total > 0:
            hits.append((total, str(rel_path), excerpt[:200]))

    hits.sort(key=lambda x: -x[0])
    hits = hits[:limit]
    if not hits:
        return _ok(f"wiki_search: no matches for {query!r}.")

    lines = [f"top {len(hits)} wiki matches for {query!r}:"]
    for score, path, excerpt in hits:
        lines.append(f"  [{score:.1f}] {path}" + (f" — {excerpt}" if excerpt else ""))
    return _ok(
        "\n".join(lines),
        data=[{"score": s, "path": p, "excerpt": e} for s, p, e in hits],
    )
