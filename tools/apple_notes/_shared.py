"""Shared helpers for the Apple Notes tools.

The npm ``apple_events`` MCP server only covers EventKit (Reminders +
Calendar). Notes.app has no EventKit surface — it's AppleScript-only.
Rather than fork the npm package, we drive ``osascript`` directly via
``asyncio.create_subprocess_exec`` (the argv-style Python API — never
shell-mode, so shell metacharacters in user input cannot escape).

Use the bundled tools for QUICK CAPTURE and cross-device sticky notes
synced via iCloud. For PERMANENT personal knowledge (research, growth,
technical notes) use the wiki subagent (Obsidian) instead.

Security: user-supplied strings (title, body, folder, query) are
embedded into AppleScript via the local ``_as_quoted`` helper that
escapes the two characters AppleScript cares about inside a quoted
string literal: backslash and double-quote. The subprocess invocation
itself is argv-style; the only injection surface is the AppleScript
source, which the quoter neutralizes.
"""
from __future__ import annotations

import asyncio
import re

# Each osascript invocation is capped at this many seconds. Notes.app
# usually responds in well under a second; if it hangs (permission
# prompt waiting on the user, Notes mid-startup, etc.) we want to fail
# loud rather than block the agent loop.
_OSASCRIPT_TIMEOUT_SEC = 10.0

# How many search hits to return by default if the caller omits ``limit``.
_DEFAULT_SEARCH_LIMIT = 10

# Variants of the "Can't get account" error that signal we should retry
# without the explicit iCloud account clause.
_ACCOUNT_ERRORS = ("can't get account", "can’t get account")

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

    Returns ``(returncode, stdout, stderr)``. Raises ``TimeoutError`` if
    osascript doesn't exit within ``_OSASCRIPT_TIMEOUT_SEC`` and
    ``FileNotFoundError`` if osascript is missing (non-mac, or a
    stripped image). The script body is passed as a single argv element,
    so shell metacharacters in user input cannot escape.
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
