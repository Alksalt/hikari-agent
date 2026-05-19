"""Research subagent tools — Tavily search + browser-use as escape hatch.

Note: native WebSearch + WebFetch are SDK built-ins already in the research
subagent's allowed_tools, so they don't need wrappers here.

Tools are optional — if TAVILY_API_KEY isn't set, tavily_search returns a clean
error. If browser-use isn't installed, browser_navigate returns an error.
The SDK MCP server still registers fine even with missing optional deps.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from claude_agent_sdk import tool

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


def _tavily_key() -> str | None:
    return os.environ.get("TAVILY_API_KEY")


@tool(
    "tavily_search",
    "Search the web via Tavily — LLM-optimized search results with ranked snippets "
    "and source URLs. Use this first for any 'what's happening with X' or 'recent state of Y' "
    "research question. max_results 1-10 (default 5).",
    {"query": str, "max_results": int},
)
async def tavily_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    max_results = max(1, min(10, int(args.get("max_results") or 5)))
    if not query:
        return _ok("tavily_search: empty query.")
    key = _tavily_key()
    if not key:
        return _ok(
            "tavily_search: TAVILY_API_KEY not set. ask the user to add it to .env "
            "(get a free key at tavily.com)."
        )

    payload = {
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": True,
        "include_raw_content": False,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(TAVILY_API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return _ok(f"tavily_search: HTTP {e.response.status_code} — {e.response.text[:200]}")
    except Exception as e:  # noqa: BLE001
        return _ok(f"tavily_search: failed — {e}")

    answer = (data.get("answer") or "").strip()
    results = data.get("results") or []
    if not results:
        return _ok(f"tavily_search: no results for {query!r}.")

    lines: list[str] = []
    if answer:
        lines.append(f"summary: {answer}")
        lines.append("")
    lines.append(f"top {len(results)} sources:")
    for r in results:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("content") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        lines.append(f"- [{title}]({url})\n  {snippet}")
    from agents.injection_guard import wrap_untrusted
    wrapped = wrap_untrusted("mcp__hikari_research__tavily_search",
                             "\n".join(lines))
    return _ok(wrapped, data={
        "query": query, "answer": answer, "untrusted": True,
        "results": [{"title": r.get("title"), "url": r.get("url"),
                     "content": r.get("content")} for r in results],
    })


@tool(
    "web_fetch",
    "Fetch a URL's text content. Lightweight passthrough — use this when Tavily surfaces "
    "a specific URL you want to read in depth. Returns page body (HTML-stripped, ~30k cap). "
    "Fails on JS-heavy SPAs and login-walled pages — use browser_navigate for those.",
    {"url": str},
)
async def web_fetch(args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url:
        return _ok("web_fetch: url is required.")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "hikari-agent/0.1"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "html" not in content_type and "text" not in content_type:
                return _ok(
                    f"web_fetch: {url} is {content_type!r}, not text/html. skipping."
                )
            raw = resp.text
    except httpx.HTTPStatusError as e:
        return _ok(f"web_fetch: HTTP {e.response.status_code} for {url}")
    except Exception as e:  # noqa: BLE001
        return _ok(f"web_fetch: failed — {e}")

    # Strip HTML lightly. For full extraction, fall through to browser_navigate.
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "lxml")
        for el in soup(["script", "style", "noscript"]):
            el.decompose()
        text = soup.get_text(separator="\n", strip=True)
    except Exception:  # noqa: BLE001
        text = raw

    if len(text) > 30000:
        text = text[:30000] + "\n\n[...truncated]"

    # Wrap fetched web content — this is the canonical attacker surface.
    from agents.injection_guard import wrap_untrusted
    wrapped = wrap_untrusted("mcp__hikari_research__web_fetch", text)
    return _ok(f"# fetched {url}\n\n{wrapped}",
               data={"url": url, "len": len(text), "untrusted": True})


@tool(
    "browser_navigate",
    "Escape hatch: drive a real browser via browser-use to extract content from a "
    "JS-heavy, login-walled, or paywalled page. Slower (~5-30s) and more expensive "
    "than web_fetch — use only when web_fetch returns garbage or a login wall. "
    "Specify the goal ('extract the article body', 'find the price', 'list the authors').",
    {"url": str, "goal": str},
)
async def browser_navigate(args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    goal = (args.get("goal") or "").strip() or "extract the main content"
    if not url:
        return _ok("browser_navigate: url is required.")

    try:
        # browser-use is an optional heavy dep. Loaded lazily.
        from browser_use import Agent as BrowserAgent
        from langchain_anthropic import ChatAnthropic
    except ImportError as e:
        return _ok(
            f"browser_navigate: browser-use or langchain-anthropic not installed ({e}). "
            "ask the user to `uv add browser-use langchain-anthropic` and accept the "
            "~150MB Chromium download on first run."
        )

    try:
        llm = ChatAnthropic(model="claude-sonnet-4-6")
        task = f"Navigate to {url} and {goal}. Return the extracted content concisely."
        agent = BrowserAgent(task=task, llm=llm)
        result = await agent.run()
        text = str(result)[:30000]
        from agents.injection_guard import wrap_untrusted
        wrapped = wrap_untrusted("mcp__hikari_research__browser_navigate", text)
        return _ok(f"# browser_navigate: {url}\n\n{wrapped}",
                   data={"url": url, "goal": goal, "untrusted": True})
    except Exception as e:  # noqa: BLE001
        return _ok(f"browser_navigate: failed — {e}")


ALL_TOOLS = [tavily_search, web_fetch, browser_navigate]
