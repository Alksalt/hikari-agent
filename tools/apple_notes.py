"""Apple Notes access via osascript (macOS only).

The npm ``apple_events`` MCP server only covers EventKit (Reminders +
Calendar). Notes.app has no EventKit surface — it's AppleScript-only.
Rather than fork the npm package, we drive ``osascript`` directly through
``asyncio.create_subprocess_exec`` (argv-style, never shell-mode).

Three tools, all macOS-only:
  - ``note_create`` — make a new note (optionally in a named folder).
  - ``note_search`` — search by title / body substring.
  - ``note_read`` — read the plaintext of a note by id or title.

Use these for QUICK CAPTURE and cross-device sticky notes synced via
iCloud. For PERMANENT personal knowledge (research, growth, technical
notes) use the wiki subagent (Obsidian) instead — that's the user's
durable knowledge graph; Notes is just the fast-capture surface.

Security: user-supplied strings (title, body, folder, query) are
embedded into AppleScript via the local ``_as_quoted`` helper that
escapes the two characters AppleScript cares about inside a quoted
string literal: backslash and double-quote. Subprocess is invoked argv-
style so shell metacharacters cannot escape; the only injection surface
is the AppleScript source itself, which the quoter neutralizes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok

logger = logging.getLogger(__name__)

# Each osascript invocation is capped at this many seconds. Notes.app
# usually responds in well under a second; if it hangs (permission
# prompt waiting on the user, Notes mid-startup, etc.) we want to fail
# loud rather than block the agent loop.
_OSASCRIPT_TIMEOUT_SEC = 10.0

# How many search hits to return by default if the caller omits ``limit``.
_DEFAULT_SEARCH_LIMIT = 10


def _as_quoted(s: str) -> str:
    """Wrap ``s`` as an AppleScript string literal.

    AppleScript string literals are double-quoted; the two characters
    that need escaping inside are backslash and double-quote. Backslash
    must be escaped first or we'd double-escape the backslashes we just
    added for the quotes.

    Do NOT substitute ``json.dumps`` here — JSON escapes a wider set
    (``\\n``, ``\\t``, unicode) that AppleScript would interpret
    differently or pass through as literal backslash sequences.
    """
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


async def _run_osascript(script: str) -> tuple[int, str, str]:
    """Run an AppleScript via the osascript binary, argv-style.

    Returns ``(returncode, stdout, stderr)``. Raises ``asyncio.
    TimeoutError`` if osascript doesn't exit within
    ``_OSASCRIPT_TIMEOUT_SEC`` and ``FileNotFoundError`` if osascript
    is missing (non-mac, or a stripped image). Never shell-mode — the
    script body is passed as a single argv element, so shell
    metacharacters in user input cannot escape.
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_OSASCRIPT_TIMEOUT_SEC,
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Drain after kill so we don't leak the transport.
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001
            pass
        raise
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return proc.returncode or 0, stdout, stderr


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """Crude HTML-to-plaintext for Notes.app body output.

    Notes stores body as HTML; ``plaintext of`` usually returns
    plain text but some fields (e.g. ``body of note``) return HTML.
    We only need a readable rendering, not full fidelity.
    """
    no_tags = _HTML_TAG_RE.sub("", s)
    # Collapse runs of whitespace produced by stripped block tags.
    return re.sub(r"\n{3,}", "\n\n", no_tags).strip()


@tool(
    "note_create",
    (
        "Create a new Apple Note (macOS, iCloud-synced). Use for QUICK "
        "CAPTURE — shopping items, fleeting thoughts, things to "
        "remember on the user's phone. For permanent personal knowledge "
        "use the wiki subagent (Obsidian) instead. "
        "title: required short header. body: note contents (HTML is "
        "accepted; plain text is fine and will be wrapped). folder: "
        "optional iCloud Notes folder name; omitted = default Notes "
        "folder. Returns the new note's id."
    ),
    {"title": str, "body": str, "folder": str},
)
async def note_create(args: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        return _ok("apple notes is macOS-only — i can't reach it from here")
    title = (args.get("title") or "").strip()
    body = args.get("body") or ""
    folder = (args.get("folder") or "").strip()
    if not title:
        return _ok("refused: missing title")

    # Notes' ``body`` property is HTML. Wrap plain text so newlines
    # render; if the caller already passed HTML (starts with ``<``) pass
    # through as-is.
    if body.lstrip().startswith("<"):
        body_html = body
    else:
        # Escape HTML-special chars in plain text, then wrap. Order
        # matters: ``&`` first or we'd double-escape entity refs we just
        # introduced.
        esc = (body.replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;"))
        body_html = "<div>" + esc.replace("\n", "<br>") + "</div>"

    title_q = _as_quoted(title)
    body_q = _as_quoted(body_html)
    # The inner "make new note ..." statement is identical regardless of
    # folder. The only difference is whether it's wrapped in a
    # ``tell folder {folder_q}`` block. Build the inner once, then wrap
    # conditionally so the two branches can't drift.
    make_stmt = (
        f'set theNote to make new note with properties '
        f'{{name:{title_q}, body:{body_q}}}'
    )
    if folder:
        folder_q = _as_quoted(folder)
        inner = (
            f'    tell folder {folder_q}\n'
            f'      {make_stmt}\n'
            f'    end tell'
        )
    else:
        inner = f'    {make_stmt}'
    script = (
        f'tell application "Notes"\n'
        f'  tell account "iCloud"\n'
        f'{inner}\n'
        f'  end tell\n'
        f'  return id of theNote\n'
        f'end tell'
    )

    try:
        rc, stdout, stderr = await _run_osascript(script)
    except FileNotFoundError:
        return _ok("apple notes unavailable — osascript not on PATH")
    except TimeoutError:
        return _ok(
            f"apple notes timed out after {_OSASCRIPT_TIMEOUT_SEC:.0f}s "
            f"(notes.app may be waiting on a permission prompt)"
        )
    if rc != 0 or stderr.strip():
        logger.warning("apple_notes osascript stderr: %s", stderr.strip()[:500])
        return _ok("apple notes error (see logs)")
    note_id = stdout.strip()
    if not note_id:
        return _ok("apple notes returned no id (create may have failed silently)")
    return _ok(
        f"note created: {title}" + (f" (folder {folder})" if folder else ""),
        data={"id": note_id, "title": title, "folder": folder or None},
    )


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


ALL_TOOLS = [note_create, note_search, note_read]
