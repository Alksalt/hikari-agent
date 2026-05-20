"""``note_create`` — make a new Apple Note (macOS, iCloud-synced)."""
from __future__ import annotations

import logging
import sys
from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.apple_notes._shared import (
    _ACCOUNT_ERRORS,
    _OSASCRIPT_TIMEOUT_SEC,
    _as_quoted,
    _run_osascript,
)

logger = logging.getLogger(__name__)


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
    # If the iCloud account clause failed (e.g. "Can't get account"), retry
    # once without it so the default Notes account is used instead.
    if (rc != 0 or stderr.strip()) and any(
        e in (stdout + stderr).lower() for e in _ACCOUNT_ERRORS
    ):
        logger.warning(
            "apple_notes: 'account iCloud' clause failed — retrying with default account. "
            "stderr: %s", stderr.strip()[:300]
        )
        # Rebuild the script without the `tell account "iCloud"` wrapper.
        if folder:
            folder_q = _as_quoted(folder)
            inner_fallback = (
                f'    tell folder {folder_q}\n'
                f'      {make_stmt}\n'
                f'    end tell'
            )
        else:
            inner_fallback = f'    {make_stmt}'
        script_fallback = (
            f'tell application "Notes"\n'
            f'{inner_fallback}\n'
            f'  return id of theNote\n'
            f'end tell'
        )
        try:
            rc, stdout, stderr = await _run_osascript(script_fallback)
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
