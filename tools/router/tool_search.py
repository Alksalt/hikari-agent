"""Client-side tool_search MCP tool.

Delegates to ``tools.catalog.Catalog`` which builds a BM25 index over
rich semantic metadata (descriptions, tags, domain, operation, examples)
rather than plain id-token expansion.

Wired as an in-process MCP tool on the hikari_router MCP server.
Bucket-1 tools stay on the always-on allowlist — tool_search does NOT
need to find them; the model already has them in its toolbelt.

Backward-compat note
--------------------
``_INDEX`` is a legacy shim retained for test compatibility.  Its keys
``bm25``, ``tool_ids``, and ``tool_descs`` are populated after
``rebuild_index()`` to reflect the catalog state.  Internal logic uses
``tools.catalog.get_catalog()`` directly.
"""
from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok

logger = logging.getLogger(__name__)

# Legacy shim — populated by rebuild_index() so existing tests can introspect.
_INDEX: dict[str, Any] = {
    "bm25": None,
    "tool_ids": [],
    "tool_descs": [],
    "tool_tags": [],
}


def rebuild_index() -> None:
    """Rebuild the catalog index.  Call at boot and after registry reloads."""
    from tools.catalog import _reset_catalog, get_catalog

    _reset_catalog()
    cat = get_catalog()

    # Build the BM25 index eagerly so _INDEX reflects truth immediately.
    cat._build_index()

    # Populate legacy shim: only bucket-2/3 entries (bucket-1 stay on the
    # always-on allowlist and are excluded from the deferred search index).
    non_b1 = [e for e in cat.entries if e.bucket != 1]
    _INDEX["bm25"] = cat._bm25
    _INDEX["tool_ids"] = [e.name for e in non_b1]
    _INDEX["tool_descs"] = [e.description for e in non_b1]
    _INDEX["tool_tags"] = [e.tags for e in non_b1]

    logger.info(
        "tool_search: catalog rebuilt — %d tools total, %d in deferred index",
        len(cat.entries),
        len(non_b1),
    )


@tool(
    "tool_search",
    "Search Hikari's larger toolbelt for tools matching a topic. Returns the top "
    "5 most-relevant tool ids so the model can decide which specific tool to invoke "
    "or which subagent to dispatch. Use when the user asks for something specific "
    "that you don't already have in your hands. After calling, dispatch the right "
    "subagent or call the surfaced tool directly. Don't list capabilities — just search.",
    {"query": str, "limit": int},
    annotations=annotations_for("tool_search"),
)
async def tool_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("tool_search: needs a non-empty query.")
    limit = max(1, min(20, int(args.get("limit") or 5)))

    from tools.catalog import get_catalog

    cat = get_catalog()

    # Search over the full catalog — the catalog includes bucket-1 tools but
    # the model already has those in context, so we filter them out of results.
    all_results = cat.search(query, k=limit + 20)
    results = [e for e in all_results if e.bucket != 1][:limit]

    if not results:
        return _ok("tool_search: no results.", data={"hits": []})

    hits = [
        {
            "tool_id": entry.name,
            "description": entry.description,
            "domain": entry.domain,
        }
        for entry in results
    ]

    lines = [f"tool_search results for {query!r}:"]
    for h in hits:
        lines.append(f"- {h['tool_id']}: {h['description']}")
    return _ok("\n".join(lines), data={"hits": hits}, presentation_hint="search_hits")
