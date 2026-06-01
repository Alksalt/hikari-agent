"""Typed adapter for ``mcp__google_workspace__query_gmail_emails``.

Calls the MCP tool directly via ``MANAGER.call``, parses the result into
``GmailMessage`` Pydantic models, and returns structured inbox buckets.
No LLM / prompt plumbing involved — this is the fabrication-proof
replacement for the old ``daily_checkin.fetch_email_buckets`` which
delegated the read to the ``drive_gmail`` subagent and trusted whatever
free-text YAML came back.

Mirror of ``tools/calendar/get_events.py`` (the typed calendar adapter).

Live response shape (probed against google-workspace-mcp 2.0.1):
    {"count": N, "emails": [{"id", "from", "subject", "snippet",
                             "internalDate" (epoch-ms str), ...}, ...]}
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from claude_agent_sdk import tool
from pydantic import BaseModel, Field, field_validator

from agents import config as cfg
from agents.mcp_manager import MANAGER, McpCallError
from tools._annotations import annotations_for
from tools._response import ok as _ok

logger = logging.getLogger(__name__)

# Gmail search strings — VERBATIM from the old daily_checkin prompt
# (agents/daily_checkin.py:300-306). Kept identical so bucket semantics
# don't drift from the LLM-delegated version we're replacing.
Q_PERSONAL = "is:unread is:inbox -category:promotions -category:updates -has:invite"
Q_INVITES = "(has:invite OR from:noreply@google.com) is:unread"
Q_DELETABLE = "(category:promotions OR category:updates) newer_than:7d"

# 1970-01-02 in epoch seconds. Any timestamp at or below this is a
# null/zero placeholder (the "1970-01-01" tell from the incident) — drop it
# rather than render a fake date.
_EPOCH_SANITY_FLOOR = 86_400
# Above this (~year 2001 in seconds) a value must be epoch *milliseconds*.
_MS_THRESHOLD = 1_000_000_000_000


class GmailMessage(BaseModel):
    """One Gmail message, normalised from the MCP response.

    ``from`` is a Python keyword, so the field is ``from_`` with an alias.
    EVERY serialization MUST use ``model_dump(by_alias=True)`` so downstream
    code (e.g. ``compose_email_message`` reading ``p['from']``) sees the
    aliased key — a plain ``model_dump()`` silently drops it.
    """

    model_config = {"populate_by_name": True}

    id: str
    from_: str = Field(default="", alias="from")
    subject: str = ""
    snippet: str = ""
    internal_date: int | None = None

    @field_validator("internal_date")
    @classmethod
    def _no_epoch_zero(cls, v: int | None) -> int | None:
        # Defense-in-depth: never store a null/epoch-zero timestamp.
        if v is not None and v <= _EPOCH_SANITY_FLOOR:
            return None
        return v


def _fail(text: str) -> dict[str, Any]:
    """Return an error envelope matching the ``ok()`` shape."""
    return {
        "content": [{"type": "text", "text": f"error: {text}"}],
        "data": {"_error": text},
    }


def _extract_messages(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the message list from a MANAGER.call result dict.

    ``MANAGER.call`` normalises the CallToolResult to either a
    structuredContent dict or ``{"text": "<json>"}`` (mcp_manager.py:88-114).
    The live google_workspace server returns ``{"count": N, "emails": [...]}``;
    we key on ``emails`` first and keep aliases for robustness across versions.
    """
    aliases = ("emails", "messages", "results", "items", "data")

    for key in aliases:
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
            for key in aliases:
                raw = parsed.get(key)
                if isinstance(raw, list):
                    return raw

    return []


def _coerce_epoch(raw: dict[str, Any]) -> int | None:
    """Parse Gmail ``internalDate`` (epoch-milliseconds string) to epoch
    seconds. Returns None for missing / unparseable / epoch-zero values."""
    val = raw.get("internalDate")
    if val is None:
        val = raw.get("internal_date")
    if val is None:
        return None
    try:
        n = int(str(val).strip())
    except (ValueError, TypeError):
        return None
    if n > _MS_THRESHOLD:  # epoch milliseconds → seconds
        n //= 1000
    if n <= _EPOCH_SANITY_FLOOR:
        return None
    return n


def _coerce_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise field names from the google_workspace MCP message shape."""
    return {
        "id": str(
            raw.get("id") or raw.get("messageId") or raw.get("message_id") or ""
        ).strip(),
        "from": str(
            raw.get("from") or raw.get("sender") or raw.get("fromAddress") or ""
        ).strip(),
        "subject": str(raw.get("subject") or "").strip(),
        "snippet": str(raw.get("snippet") or "").strip(),
        "internal_date": _coerce_epoch(raw),
    }


def _domain_of(from_str: str) -> str:
    """Extract the sender domain from ``a@b.com`` or ``Name <a@b.com>``."""
    s = (from_str or "").strip()
    if "<" in s and ">" in s:
        s = s[s.find("<") + 1 : s.find(">")]
    s = s.strip().strip("<>").lower()
    if "@" not in s:
        return ""
    dom = s.rsplit("@", 1)[-1].strip()
    # Constrain to a valid domain charset so a hostile From header can't smuggle
    # arbitrary text downstream via the "domain".
    m = re.match(r"[a-z0-9.\-]+", dom)
    return m.group(0) if m else ""


async def _query(query_str: str, *, max_results: int = 25) -> list[GmailMessage]:
    """Run one Gmail query directly and parse into typed messages.

    Raises ``McpCallError`` on tool error so callers can decide how to handle
    it (mirrors ``tools/calendar/get_events.py:_fetch_events``).
    """
    result = await MANAGER.call(
        "google_workspace",
        "query_gmail_emails",
        {"query": query_str, "max_results": int(max_results)},
    )
    raw_messages = _extract_messages(result)
    out: list[GmailMessage] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        coerced = _coerce_message(item)
        if not coerced["id"]:
            continue
        out.append(GmailMessage(**coerced))
    return out


def _aggregate_deletable(
    messages: list[GmailMessage], *, max_ids: int, top_cap: int
) -> dict[str, Any]:
    """Pure-Python aggregation of the deletable promo/update pile — the
    fabrication-proof replacement for what the LLM used to confabulate."""
    sample_ids = [m.id for m in messages if m.id][:max_ids]
    domains: Counter[str] = Counter(
        d for m in messages if (d := _domain_of(m.from_))
    )
    top_senders = [dom for dom, _ in domains.most_common(top_cap)]
    return {
        "count": len(messages),
        "top_senders": top_senders,
        "sample_ids": sample_ids,
    }


async def _fetch_inbox_buckets() -> dict[str, Any]:
    """Run the three inbox queries and return the canonical bucket shape
    consumed by ``daily_checkin.compose_email_message`` — identical to the
    old ``fetch_email_buckets`` output, minus the LLM."""
    personal_cap = int(cfg.get("daily_checkin.personal_subject_cap", 5))
    max_ids = int(cfg.get("daily_checkin.max_delete_ids", 200))
    top_cap = int(cfg.get("daily_checkin.deletable_top_senders_cap", 3))

    personal = await _query(Q_PERSONAL, max_results=max(personal_cap * 2, 25))
    invites = await _query(Q_INVITES, max_results=25)
    promos = await _query(Q_DELETABLE, max_results=max_ids)

    return {
        "unread_personal": [
            m.model_dump(by_alias=True) for m in personal[:personal_cap]
        ],
        "calendar_invites": [
            m.model_dump(by_alias=True) for m in invites[:personal_cap]
        ],
        "deletable": _aggregate_deletable(promos, max_ids=max_ids, top_cap=top_cap),
    }


@tool(
    "query_inbox",
    "Read the user's Gmail inbox directly (typed, no subagent). With no "
    "'query' arg, returns three buckets: unread_personal, calendar_invites, "
    "and a deletable promo/update pile. With a 'query' (Gmail search syntax, "
    "e.g. 'is:unread from:boss@x.com'), returns the matching messages. "
    "Prefer this over delegating inbox reads to drive_gmail.",
    {"query": str, "max_results": int},
    annotations=annotations_for("query_inbox"),
)
async def query_inbox(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    try:
        max_results = int(args.get("max_results") or 25)
    except (TypeError, ValueError):
        max_results = 25
    try:
        if query:
            msgs = await _query(query, max_results=max_results)
            return _ok(
                f"{len(msgs)} messages for query",
                data={"messages": [m.model_dump(by_alias=True) for m in msgs]},
            )
        buckets = await _fetch_inbox_buckets()
        return _ok(
            f"{len(buckets['unread_personal'])} unread personal, "
            f"{len(buckets['calendar_invites'])} invites, "
            f"{buckets['deletable']['count']} deletable",
            data=buckets,
        )
    except McpCallError as exc:
        logger.warning("query_inbox: MCP error: %s", exc)
        return _fail(f"gmail fetch failed: {exc.message}")
