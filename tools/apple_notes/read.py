"""``note_read`` — read the plaintext body of an Apple Note by id or title."""
from __future__ import annotations

import logging
import sys
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.apple_notes._shared import (
    _OSASCRIPT_TIMEOUT_SEC,
    _as_quoted,
    _run_osascript,
    _strip_html,
)

logger = logging.getLogger(__name__)


@tool(
    "note_read",
    (
        "Read the plaintext body of an Apple Note by id (preferred) "
        "or exact title (fallback). Use after ``note_search`` to "
        "pull the contents of a specific note. ``title_or_id``: the "
        "value returned by ``note_search``'s id field, or the exact "
        "title string."
    ),
    {"title_or_id": str},
    annotations=annotations_for("note_read"),
)
async def note_read(args: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        return _ok("apple notes is macOS-only — i can't reach it from here")
    key = (args.get("title_or_id") or "").strip()
    if not key:
        return _ok("refused: missing title_or_id")

    key_q = _as_quoted(key)
    # Try as id first; if no match, fall back to exact-name match. We
    # encode both branches in the AppleScript so we only spawn one
    # subprocess.
    script = (
        f'set _key to {key_q}\n'
        f'tell application "Notes"\n'
        f'  set _hits to every note whose id is _key\n'
        f'  if (count of _hits) is 0 then\n'
        f'    set _hits to every note whose name is _key\n'
        f'  end if\n'
        f'  if (count of _hits) is 0 then\n'
        f'    return ""\n'
        f'  end if\n'
        f'  set _n to item 1 of _hits\n'
        f'  return (name of _n) & linefeed & "---" & linefeed & '
        f'(plaintext of _n)\n'
        f'end tell'
    )

    try:
        rc, stdout, stderr = await _run_osascript(script)
    except FileNotFoundError:
        return _ok("apple notes unavailable — osascript not on PATH")
    except TimeoutError:
        return _ok(
            f"apple notes read timed out after {_OSASCRIPT_TIMEOUT_SEC:.0f}s"
        )
    if rc != 0 or stderr.strip():
        logger.warning("apple_notes osascript stderr: %s", stderr.strip()[:500])
        return _ok("apple notes error (see logs)")

    raw = stdout.strip("\n")
    if not raw:
        return _ok(f"no apple note matches {key!r}")
    # Split title/body on the first "---" separator we injected.
    if "\n---\n" in raw:
        title, body = raw.split("\n---\n", 1)
    else:
        title, body = "", raw
    body = _strip_html(body)
    return _ok(
        f"{title}\n---\n{body}" if title else body,
        data={"title": title, "body": body},
    )
