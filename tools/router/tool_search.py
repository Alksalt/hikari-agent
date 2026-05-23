"""Client-side tool_search MCP tool.

BM25 ranks Bucket-2 + Bucket-3 tool ids against the user's query. Returns
top-N tool ids + one-line descriptions so the model can decide which
specific tool to invoke or which subagent to dispatch.

Wired as an in-process MCP tool on the hikari_router MCP server.
Bucket-1 tools stay on the always-on allowlist — tool_search does NOT
need to find them; the model already has them in its toolbelt.
"""
from __future__ import annotations

import logging
from typing import Any

import bm25s
from claude_agent_sdk import tool

from tools._response import ok as _ok

logger = logging.getLogger(__name__)

_INDEX: dict[str, Any] = {
    "bm25": None,
    "tool_ids": [],
    "tool_descs": [],
    "tool_tags": [],
}


def rebuild_index() -> None:
    """Rebuild the BM25 corpus from the current registry. Call at boot
    and whenever the registry reloads."""
    from tools._tools_yaml import _load_yaml, DEFAULT_YAML_PATH
    reg = _load_yaml(DEFAULT_YAML_PATH)
    docs: list[str] = []
    ids: list[str] = []
    descs: list[str] = []
    tags_all: list[list[str]] = []
    for spec in reg.specs():
        bucket = getattr(spec, "bucket", None)
        if bucket == 1:
            continue
        # ToolSpec has no description or tags — derive from the tool id itself.
        tool_id = spec.id
        # Build a searchable doc: expanded id tokens only (no external description
        # field exists on ToolSpec; the model's tool definitions carry the prose).
        doc_text = tool_id.replace("mcp__", "").replace("__", " ").replace("_", " ").replace("-", " ")
        ids.append(tool_id)
        descs.append(doc_text)
        tags_all.append([])
        docs.append(doc_text)
    if not docs:
        _INDEX["bm25"] = None
        _INDEX["tool_ids"] = []
        _INDEX["tool_descs"] = []
        _INDEX["tool_tags"] = []
        logger.info("tool_search: no non-bucket-1 tools to index")
        return
    bm25 = bm25s.BM25()
    bm25.index(bm25s.tokenize(docs, stopwords="en"))
    _INDEX["bm25"] = bm25
    _INDEX["tool_ids"] = ids
    _INDEX["tool_descs"] = descs
    _INDEX["tool_tags"] = tags_all
    logger.info("tool_search: indexed %d tools (bucket 2+3)", len(ids))


@tool(
    "tool_search",
    "Search Hikari's larger toolbelt for tools matching a topic. Returns the top "
    "5 most-relevant tool ids so the model can decide which specific tool to invoke "
    "or which subagent to dispatch. Use when the user asks for something specific "
    "that you don't already have in your hands. After calling, dispatch the right "
    "subagent or call the surfaced tool directly. Don't list capabilities — just search.",
    {"query": str, "limit": int},
)
async def tool_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("tool_search: needs a non-empty query.")
    limit = max(1, min(20, int(args.get("limit") or 5)))
    bm25 = _INDEX.get("bm25")
    if bm25 is None:
        rebuild_index()
        bm25 = _INDEX.get("bm25")
    if bm25 is None:
        return _ok("tool_search: no indexable tools.", data={"hits": []})
    results = bm25.retrieve(bm25s.tokenize(query, stopwords="en"), k=limit)
    ids = _INDEX["tool_ids"]
    descs = _INDEX["tool_descs"]
    hits = []
    for row_idx in results.documents[0]:
        if row_idx < len(ids):
            hits.append({
                "tool_id": ids[row_idx],
                "description": descs[row_idx],
            })
    lines = [f"tool_search results for {query!r}:"]
    for h in hits:
        lines.append(f"- {h['tool_id']}: {h['description']}")
    return _ok("\n".join(lines), data={"hits": hits}, presentation_hint="search_hits")
