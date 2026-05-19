"""Notion helpers — schema introspection cache for the notion-agent.

The actual Notion MCP server (@notionhq/notion-mcp-server) provides the heavy
lifting (search, fetch, query, create, update). This module just caches database
schema (property_name → property_id) at startup so the agent doesn't burn tokens
re-introspecting every turn.

Call refresh_schema_cache() once at startup (or on schema error) to populate.
The notion-agent's prompt references the cached schema implicitly via runtime_state.
"""

from __future__ import annotations

import json
import logging

from storage import db

logger = logging.getLogger(__name__)


def get_cached_schema(database_id: str) -> dict[str, str] | None:
    """Returns {property_name: property_id} for a database, or None if not cached."""
    raw = db.runtime_get(f"notion_schema_{database_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def set_cached_schema(database_id: str, schema: dict[str, str]) -> None:
    db.runtime_set(f"notion_schema_{database_id}", json.dumps(schema))


def clear_cached_schema(database_id: str) -> None:
    db.runtime_set(f"notion_schema_{database_id}", None)


# No tools here — the agent uses mcp__notion__* directly. This module is just
# a thin cache layer the agent can rely on via the system prompt instruction
# "introspect via notion-fetch on first use, cache via storage.runtime_state".
