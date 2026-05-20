"""wiki_backlinks — find notes that link to a given topic via the vault graph."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.wiki._shared import _vault


@tool(
    "wiki_backlinks",
    "List notes in the user's Obsidian wiki that LINK TO a given topic/note. "
    "Use to find related material via the wiki's graph, not by keyword. topic can "
    "be an exact note name or a substring. "
    "e.g. user asks 'what notes reference my meria project' → wiki_backlinks('meria'). "
    "Don't use this for text search across notes (use `wiki_search`) — backlinks "
    "follow [[wikilink]] edges, not content matches.",
    {"topic": str, "limit": int},
)
async def wiki_backlinks(args: dict[str, Any]) -> dict[str, Any]:
    topic = (args.get("topic") or "").strip()
    limit = max(1, min(50, int(args.get("limit") or 10)))
    if not topic:
        return _ok("wiki_backlinks: topic is required.")

    v = _vault()
    # Try exact note-name match first
    if topic in v.md_file_index:
        try:
            links = v.get_backlinks(topic)
        except Exception:  # noqa: BLE001
            links = []
        if links:
            shown = links[:limit]
            lines = [f"{len(links)} backlinks to {topic!r}:"]
            lines.extend(f"  - {n}" for n in shown)
            return _ok("\n".join(lines), data={"topic": topic, "backlinks": links})

    # Fall back: find notes whose name contains the topic, return their combined backlinks
    matching_notes = [n for n in v.md_file_index if topic.lower() in n.lower()]
    if not matching_notes:
        return _ok(f"wiki_backlinks: no notes match {topic!r}.")

    aggregated: dict[str, int] = {}
    for n in matching_notes[:5]:
        try:
            for src in v.get_backlinks(n):
                aggregated[src] = aggregated.get(src, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    if not aggregated:
        return _ok(f"wiki_backlinks: matched {len(matching_notes)} note(s) but no backlinks.")

    ranked = sorted(aggregated.items(), key=lambda kv: -kv[1])[:limit]
    lines = [f"backlinks via fuzzy match on {topic!r} ({len(matching_notes)} notes):"]
    lines.extend(f"  - {src} (×{count})" for src, count in ranked)
    return _ok(
        "\n".join(lines),
        data={"topic": topic, "matched_notes": matching_notes, "backlinks": dict(ranked)},
    )
