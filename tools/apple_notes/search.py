"""``note_search`` — find Apple Notes by substring (macOS, iCloud)."""
from __future__ import annotations

import logging
import sys
from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.apple_notes._shared import (
    _DEFAULT_SEARCH_LIMIT,
    _OSASCRIPT_TIMEOUT_SEC,
    _as_quoted,
    _run_osascript,
)

logger = logging.getLogger(__name__)


@tool(
    "note_search",
    (
        "Search Apple Notes (macOS, iCloud) for a substring across "
        "title and body. Use for finding a quick-capture note the "
        "user mentioned ('that shopping list', 'the note about X'). "
        "For research-grade personal knowledge search use the wiki "
        "subagent. query: substring to look for. limit: max hits "
        "(default 10). Returns id, title, modification date."
    ),
    {"query": str, "limit": int},
)
async def note_search(args: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        return _ok("apple notes is macOS-only — i can't reach it from here")
    query = (args.get("query") or "").strip()
    limit = int(args.get("limit") or _DEFAULT_SEARCH_LIMIT)
    if limit <= 0:
        limit = _DEFAULT_SEARCH_LIMIT
    if not query:
        return _ok("refused: empty query")

    query_q = _as_quoted(query)
    # ``plaintext`` is the body without HTML; ``name`` is the title.
    # We compose one tab-separated record per match: id\ttitle\tmodDate
    # then split on newlines on the Python side. AppleScript's default
    # list rendering is unreliable so we walk and emit lines ourselves.
    script = (
        f'set _q to {query_q}\n'
        f'set _out to ""\n'
        f'tell application "Notes"\n'
        f'  set _matches to every note whose (name contains _q) or '
        f'(plaintext contains _q)\n'
        f'  repeat with n in _matches\n'
        f'    set _out to _out & (id of n) & tab & (name of n) & tab '
        f'& ((modification date of n) as string) & linefeed\n'
        f'  end repeat\n'
        f'end tell\n'
        f'return _out'
    )

    try:
        rc, stdout, stderr = await _run_osascript(script)
    except FileNotFoundError:
        return _ok("apple notes unavailable — osascript not on PATH")
    except TimeoutError:
        return _ok(
            f"apple notes search timed out after {_OSASCRIPT_TIMEOUT_SEC:.0f}s"
        )
    if rc != 0 or stderr.strip():
        logger.warning("apple_notes osascript stderr: %s", stderr.strip()[:500])
        return _ok("apple notes error (see logs)")

    hits: list[dict[str, str]] = []
    for line in stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        nid, name, modified = parts[0], parts[1], "\t".join(parts[2:])
        hits.append({"id": nid, "title": name, "modified": modified})
        if len(hits) >= limit:
            break

    if not hits:
        return _ok(f"no apple notes matched {query!r}", data={"hits": []})
    lines = [f"apple notes ({len(hits)} hit{'s' if len(hits) != 1 else ''}):"]
    for h in hits:
        lines.append(f"  - {h['title']} [{h['modified']}]")
    return _ok("\n".join(lines), data={"hits": hits})
