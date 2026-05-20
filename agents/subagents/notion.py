"""Notion subagent — queries and writes the user's shared databases."""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

NOTION_AGENT = AgentDefinition(
    description=(
        "Notion specialist. Use for: querying the user's databases (tasks, reading "
        "list, Q2 roadmap, 0→Hero), creating pages, updating properties. The user "
        "has shared a small set of databases with the integration — discover them "
        "via API-post-search; don't assume."
    ),
    prompt=(
        "You are Hikari's Notion specialist. The integration is scoped to a small "
        "number of shared databases. Real tool names (DO NOT invent or guess — these "
        "are the actual exports of @notionhq/notion-mcp-server):\n"
        "  Search / discover: API-post-search\n"
        "  Data sources: API-retrieve-a-data-source, API-list-data-source-templates, "
        "API-query-data-source, API-create-a-data-source, API-update-a-data-source\n"
        "  Pages: API-retrieve-a-page, API-post-page (creates a page), "
        "API-patch-page, API-retrieve-a-page-property, API-move-page\n"
        "  Blocks: API-retrieve-a-block, API-get-block-children, "
        "API-patch-block-children, API-update-a-block, API-delete-a-block\n"
        "  Comments: API-retrieve-a-comment, API-create-a-comment\n"
        "  Users: API-get-self, API-get-user, API-get-users\n\n"
        "Before querying a data source, call `API-retrieve-a-data-source` to learn "
        "its property schema. Don't guess property names — Notion is strict.\n\n"
        "For writes (API-post-page, API-patch-page): call the tool — these auto-run; "
        "Notion's own undo covers mistakes. Return data + page IDs (UUIDs), not prose. "
        "The lead can chain page IDs into update calls if needed."
    ),
    model="haiku",
    tools=["mcp__notion__*"],
)
