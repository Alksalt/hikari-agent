"""Phase 10: arxiv search for ML/DL papers."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import arxiv
from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok

logger = logging.getLogger(__name__)


@tool(
    "arxiv_search",
    "Search arxiv for recent ML/DL papers. query is plain text. categories defaults "
    "to cs.LG, cs.AI, cs.CL, stat.ML. days defaults to 14 (recency filter). "
    "limit defaults to 10. Returns title, abstract, authors, url for each match.",
    {"query": str, "categories": list, "days": int, "limit": int},
)
async def arxiv_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("refused: empty query")
    categories = args.get("categories") or cfg.get("arxiv.default_categories") \
        or ["cs.LG", "cs.AI", "cs.CL", "stat.ML"]
    days = int(args.get("days") or cfg.get("arxiv.default_days") or 14)
    limit = int(args.get("limit") or cfg.get("arxiv.default_limit") or 10)

    cat_filter = " OR ".join(f"cat:{c}" for c in categories)
    full_query = f"({cat_filter}) AND all:{query}"
    cutoff = datetime.now(UTC) - timedelta(days=days)

    try:
        search = arxiv.Search(
            query=full_query, max_results=limit * 3,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        papers = []
        for r in search.results():
            try:
                pub = r.published if isinstance(r.published, datetime) \
                    else datetime.fromisoformat(str(r.published))
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=UTC)
                if pub < cutoff:
                    continue
            except Exception:
                pass
            papers.append({
                "title": str(r.title).strip(),
                "abstract": str(r.summary).strip()[:600],
                "authors": [str(a.name) for a in (r.authors or [])][:5],
                "url": str(r.entry_id),
                "published": str(r.published),
                "categories": list(r.categories or []),
            })
            if len(papers) >= limit:
                break
    except Exception as e:
        logger.exception("arxiv search failed")
        return _ok(f"arxiv error: {e}", data={"error": str(e)})

    if not papers:
        return _ok(f"no papers found in last {days}d for {query!r}",
                   data={"papers": []})
    lines = [f"found {len(papers)} paper(s):"]
    for p in papers:
        lines.append(f"  - {p['title']} — {', '.join(p['authors'])}\n    {p['url']}")
    return _ok("\n".join(lines), data={"papers": papers})


ALL_TOOLS = [arxiv_search]
