"""wiki_append — write content into a note (optionally under an H2 section)."""
from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok
from tools.wiki._shared import _do_wiki_append

logger = logging.getLogger(__name__)


@tool(
    "wiki_append",
    "Write content into a note in the user's Obsidian wiki. With section_heading, "
    "appends under that H2 (creating it if absent). Preserves frontmatter; use "
    "[[wikilinks]] for cross-refs. Runs WITHOUT an approval prompt (iCloud history "
    "is the safety net) and every write is audit-logged. "
    "e.g. user says 'add this to my notes on rust' → wiki_append('rust', None, '<text>'). "
    "Don't use this to store a fact about the user (use `remember`) or to read (use `wiki_read`).",
    {"path": str, "section_heading": str, "content": str},
)
async def wiki_append(args: dict[str, Any]) -> dict[str, Any]:
    path_arg = (args.get("path") or "").strip()
    section = (args.get("section_heading") or "").strip()
    content = (args.get("content") or "").rstrip()
    if not path_arg:
        return _ok("wiki_append: path is required.")
    if not content:
        return _ok("wiki_append: content is empty.")

    result_str = await _do_wiki_append(args)
    # Audit every wiki append so the trail is intact even without an approval row.
    try:
        section_str = f" under '## {section}'" if section else ""
        db.audit_append(
            tool="mcp__hikari_wiki__wiki_append",
            args_json_redacted=(
                f"path={path_arg!r}{section_str} ({len(content)} chars)"
            )[:500],
            result_summary=result_str[:500],
            approved_by="auto",
        )
    except Exception:
        logger.exception("wiki_append: audit_append failed (non-fatal)")
    return _ok(result_str, data={"path": path_arg})
