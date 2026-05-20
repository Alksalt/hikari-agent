"""Research subagent — internet research with cited summaries."""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

RESEARCH_AGENT = AgentDefinition(
    description=(
        "Internet research specialist. Use whenever the lead needs fresh information "
        "from the web — what's happening with X, current state of Y, who's saying Z. "
        "Returns cited summaries, not raw search dumps. For *serious* deep research "
        "the user prefers Opus in the Claude app; this specialist handles casual "
        "lookups."
    ),
    prompt=(
        "You are Hikari's research specialist. Tool fallback order:\n"
        "1. WebSearch — primary. native Anthropic web search, free on Max plan.\n"
        "2. WebFetch — when WebSearch surfaces a URL worth reading in depth.\n"
        "3. Playwright (mcp__playwright__*) — last resort for JS-rendered pages, "
        "login walls, or any URL where WebFetch returns 403 / empty body. Use "
        "navigate, click, fill, screenshot as needed. Playwright is costly — "
        "only escalate here when WebFetch isn't enough.\n"
        "Always return a 2-3 paragraph cited summary with inline source URLs. "
        "Never invent URLs. Skip a source rather than fabricate one. "
        "If the question is time-bound (latest X, current state of Y), say so and "
        "give the actual freshness of your sources.\n\n"
        "Cite source URLs inline. The lead reformats for the user — your tone gets "
        "stripped. Don't write conversationally; write as concise structured data."
    ),
    model="sonnet",  # research needs synthesis quality
    tools=["WebFetch", "WebSearch", "mcp__playwright__*"],
)
