"""Phase 11: shared per-session scratch memory for subagents.

Lets the recall + wiki subagents (and future ones) share findings within a
session. recall fetches a fact, writes it to scratch under topic="Meria";
a subsequent wiki write can scratch_get(session_id, "Meria") instead of
re-querying. Saves tokens + improves coherence.

Hindsight pattern (May 2026). Session-scoped, 24h TTL, 100-row cap per session.
"""
from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok

logger = logging.getLogger(__name__)


def _current_session_id() -> str:
    """Get the current session id from runtime_state."""
    sid = db.runtime_get("current_session_id")
    return sid or "default"


@tool(
    "scratch_put",
    "Write a finding to per-session scratch memory. Use to share context with "
    "later subagent calls in the same session. topic is the noun/key (e.g. "
    "'Meria', 'transformer paper'); payload is a dict or string.",
    {"topic": str, "payload": dict},
)
async def scratch_put(args: dict[str, Any]) -> dict[str, Any]:
    topic = (args.get("topic") or "").strip()
    payload = args.get("payload")
    if not topic:
        return _ok("refused: empty topic")
    if payload is None:
        return _ok("refused: missing payload")
    sid = _current_session_id()
    rid = db.scratch_put(sid, topic, payload)
    return _ok(f"scratch[{topic}] saved as #{rid}", data={"id": rid})


@tool(
    "scratch_get",
    "Read recent scratch entries for a topic in this session. Returns up to "
    "`limit` (default 5) entries, newest first.",
    {"topic": str, "limit": int},
)
async def scratch_get(args: dict[str, Any]) -> dict[str, Any]:
    topic = (args.get("topic") or "").strip()
    limit = int(args.get("limit") or 5)
    if not topic:
        return _ok("refused: empty topic")
    sid = _current_session_id()
    entries = db.scratch_get(sid, topic, limit=limit)
    if not entries:
        return _ok(f"no scratch for topic={topic!r}", data={"entries": []})
    return _ok(
        f"{len(entries)} scratch entries for {topic!r}",
        data={"entries": entries},
    )


ALL_TOOLS = [scratch_put, scratch_get]
