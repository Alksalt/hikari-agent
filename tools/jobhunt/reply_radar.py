"""Gmail reply radar — typed adapter + append-only handoff writer.

Detects NEW inbox replies from known job-hunt contacts and hands them off
to the outreach repo's own operator via an append-only markdown file. This
module is the ONLY place in Hikari that writes to an external job-hunt repo
— it never touches outreach.db / job_search.db (read-only, via
``tools.jobhunt.readers``) and never writes Notion.

Mirrors ``tools/gmail/inbox.py``'s typed-adapter pattern: call
``MANAGER.call`` directly (no LLM tool hop), parse the real JSON response
into plain dicts, never raise out of the public entry point.

Handoff contract (verify-after-write is MANDATORY — see ``_append_and_verify``):
  - Path: ``<jobhunt.roots.outreach>/<jobhunt.handoff_file>``.
  - Created with a short header comment on first write.
  - One line per NEW reply (``message_id`` not already present anywhere in
    the file): ``- [YYYY-MM-DD HH:MM] reply from <sender> (<org>) — subject:
    <subject> — thread:<gmail_thread_id> — msg:<message_id> — status:
    unprocessed``.
  - After writing, the file is re-read and each new line's exact presence is
    confirmed. Any line that doesn't verify is dropped from the returned
    list — a reply is never surfaced as "logged" when the log write failed
    (or produced something other than what we intended to write).

``scan()`` never raises: any failure (gmail query error, MCP spawn failure,
missing/unwritable outreach root, corrupt handoff file) is caught, logged,
and mapped to an empty list so a jobhunt-radar hiccup never blocks the rest
of the daily brief.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agents import config as cfg
from agents.daily_checkin import _resolve_local_tz
from agents.mcp_manager import MANAGER
from tools.jobhunt import readers

logger = logging.getLogger(__name__)

# Gmail search — relative window, always "as of now" regardless of the
# `today` argument (which exists for signature parity with the readers.*
# functions and so callers/tests can reason about a fixed reference date).
_MAX_RESULTS = 50

_ALIASES = ("emails", "messages", "results", "items", "data")

_HEADER = (
    "<!-- append-only handoff written by hikari; consumed by the outreach "
    "repo operator; do not hand-edit lines, mark them processed instead. -->\n"
)


def _now_local() -> datetime:
    return datetime.now(_resolve_local_tz())


# ---------- gmail query + parsing (mirrors tools/gmail/inbox.py) ----------

def _extract_messages(result: dict[str, Any]) -> list[Any]:
    """Extract the message list from a MANAGER.call result dict.

    Same normalisation as ``tools/gmail/inbox.py::_extract_messages`` —
    duplicated rather than imported so this module has no dependency on
    another adapter's private helpers.
    """
    for key in _ALIASES:
        raw = result.get(key)
        if isinstance(raw, list):
            return raw

    text = result.get("text") or ""
    if text:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in _ALIASES:
                raw = parsed.get(key)
                if isinstance(raw, list):
                    return raw

    return []


async def _query_recent(days: int) -> list[Any]:
    result = await MANAGER.call(
        "google_workspace",
        "query_gmail_emails",
        {"query": f"newer_than:{days}d in:inbox", "max_results": _MAX_RESULTS},
    )
    return _extract_messages(result)


def _coerce_reply(raw: dict[str, Any]) -> dict[str, str]:
    """Normalise field names from the google_workspace MCP message shape.

    Live shape (verified against google-workspace-mcp 2.0.1's
    ``GmailService._parse_message``): top-level ``id`` is Gmail's own
    message id (stable, always present) and ``threadId`` is the thread id.
    There is ALSO a header-derived ``message_id`` (the RFC822 Message-ID
    header) which is a different value — we deliberately use Gmail's own
    ``id`` as our identifier (matches ``GmailMessage.id`` in
    ``tools/gmail/inbox.py`` and is guaranteed present on every message the
    list endpoint returns).
    """
    return {
        "id": str(raw.get("id") or raw.get("messageId") or "").strip(),
        "thread_id": str(raw.get("threadId") or raw.get("thread_id") or "").strip(),
        "from": str(
            raw.get("from") or raw.get("sender") or raw.get("fromAddress") or ""
        ).strip(),
        "subject": str(raw.get("subject") or "").strip(),
    }


def _extract_address(from_str: str) -> str:
    """Pull the bare, lowercased email address out of ``a@b.com`` or
    ``Name <a@b.com>``. Returns "" if no ``@`` is found."""
    s = (from_str or "").strip()
    if "<" in s and ">" in s:
        s = s[s.find("<") + 1 : s.find(">")]
    s = s.strip().strip("<>").lower()
    return s if "@" in s else ""


def _employer_label(domain: str) -> str:
    """Best-effort employer label derived from a sender's email domain.

    ``readers.contact_emails()`` is a flat ``set[str]`` with no org
    attached (by design — see its docstring), so there is no read-only,
    in-scope way to resolve an exact organisation name for an arbitrary
    contact address. This is a readable fallback, not a claim of accuracy:
    strips the TLD, replaces separators with spaces, title-cases.
    """
    if not domain:
        return "(unknown)"
    base = domain.split(".")[0]
    label = re.sub(r"[-_]+", " ", base).strip()
    return label.title() if label else domain


def _match_candidates(raw_messages: list[Any], contacts: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        coerced = _coerce_reply(raw)
        mid = coerced["id"]
        if not mid or mid in seen_ids:
            continue
        address = _extract_address(coerced["from"])
        if not address or address not in contacts:
            continue
        seen_ids.add(mid)
        domain = address.rsplit("@", 1)[-1] if "@" in address else ""
        out.append({
            "from": address,
            "org_or_employer": _employer_label(domain),
            "subject": coerced["subject"],
            "gmail_thread_id": coerced["thread_id"],
            "message_id": mid,
        })
    return out


# ---------- handoff file (append-only, verify-after-write) ----------

def _handoff_path() -> Path | None:
    raw = cfg.get("jobhunt.roots.outreach")
    if not raw:
        return None
    root = Path(str(raw))
    if not root.is_dir():
        return None
    fname = str(cfg.get("jobhunt.handoff_file", "hikari_inbox.md"))
    return root / fname


def _read_existing(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("reply_radar: could not read existing handoff file %s", path)
        return ""


def _format_line(ts: datetime, reply: dict[str, Any]) -> str:
    stamp = ts.strftime("%Y-%m-%d %H:%M")
    return (
        f"- [{stamp}] reply from {reply['from']} ({reply['org_or_employer']}) — "
        f"subject: {reply['subject']} — thread:{reply['gmail_thread_id']} — "
        f"msg:{reply['message_id']} — status: unprocessed"
    )


def _append_and_verify(
    path: Path, entries: list[tuple[dict[str, Any], str]]
) -> list[dict[str, Any]]:
    """Append every ``line`` in ``entries`` to ``path``, then re-read the
    file and confirm each line's exact presence. Only replies whose line
    verifies land in the returned list — this is the mandatory
    verify-after-write step from the handoff contract."""
    try:
        is_new_file = not path.exists()
        with path.open("a", encoding="utf-8") as fh:
            if is_new_file:
                fh.write(_HEADER)
            for _reply, line in entries:
                fh.write(line + "\n")
    except Exception:
        logger.error(
            "reply_radar: handoff write failed for %s — excluding %d reply(ies) "
            "from the brief (never surfacing an unlogged reply as logged)",
            path, len(entries), exc_info=True,
        )
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        logger.error(
            "reply_radar: verify-after-write read failed for %s — excluding "
            "%d reply(ies) from the brief", path, len(entries), exc_info=True,
        )
        return []

    verified: list[dict[str, Any]] = []
    for reply, line in entries:
        if line in text:
            verified.append(reply)
        else:
            logger.error(
                "reply_radar: verify-after-write mismatch for message_id=%s — "
                "excluding from brief (log write may have failed or been "
                "truncated)", reply.get("message_id"),
            )
    return verified


# ---------- public entry point ----------

async def scan(today: date) -> list[dict[str, Any]]:  # noqa: ARG001 — kept for signature parity with readers.*
    """Detect NEW replies from known job-hunt contacts in the last
    ``jobhunt.reply_lookback_days`` days, log each to the outreach repo's
    append-only handoff file, and return the ones that verified.

    Never raises: gmail errors, MCP spawn failures, and handoff-file I/O
    errors are all caught, logged, and mapped to an empty list (or a
    shorter list, for partial verify-after-write failures).
    """
    contacts = readers.contact_emails()
    if not contacts:
        return []

    try:
        days = int(cfg.get("jobhunt.reply_lookback_days", 2))
    except (TypeError, ValueError):
        days = 2

    try:
        raw_messages = await _query_recent(days)
    except Exception:
        logger.exception("reply_radar: gmail query failed")
        return []

    candidates = _match_candidates(raw_messages, contacts)
    if not candidates:
        return []

    path = _handoff_path()
    if path is None:
        logger.warning(
            "reply_radar: outreach root unresolved — %d matching reply(ies) "
            "found but cannot be logged; dropping from brief", len(candidates),
        )
        return []

    existing_text = _read_existing(path)
    now_local = _now_local()
    to_append: list[tuple[dict[str, Any], str]] = []
    for reply in candidates:
        mid = reply["message_id"]
        # Anchored on the "msg:" prefix so a short id can't false-match a
        # substring of some other id / subject text in the file.
        if mid and f"msg:{mid}" in existing_text:
            continue  # already logged in a prior scan — not new
        to_append.append((reply, _format_line(now_local, reply)))

    if not to_append:
        return []

    return _append_and_verify(path, to_append)
