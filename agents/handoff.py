"""Session handoff — write a small breadcrumb at session end so the next
session can cold-open with "where were we" energy.

Stored in ``runtime_state["session_handoff"]`` as JSON:
  {"ts": iso, "turns": [{"role": "...", "content": "..."}, ...]}

Consumed by ``agents.hooks.inject_memory`` when the next inbound arrives within
``session_handoff.max_gap_hours`` of the stored ts. After consumption the entry
is cleared so we don't echo the same handoff twice.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)

_KEY = "session_handoff"


def _enabled() -> bool:
    return bool(cfg.get("session_handoff.enabled", True))


def _last_n() -> int:
    return int(cfg.get("session_handoff.last_n_turns", 2))


def _max_gap_hours() -> float:
    return float(cfg.get("session_handoff.max_gap_hours", 48))


def _min_gap_hours() -> float:
    """How recent counts as 'still in session' — suppress to avoid echoing
    context the agent already has in its live window."""
    return float(cfg.get("session_handoff.min_gap_hours", 0.5))


def write_handoff() -> None:
    """Snapshot the most-recent N turns into runtime_state.

    Called by runtime.respond() after each successful turn. Cheap — small JSON.
    """
    if not _enabled():
        return
    try:
        msgs = db.recent_messages(limit=_last_n())
    except Exception:
        logger.exception("write_handoff: recent_messages failed")
        return
    if not msgs:
        return
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "turns": [{"role": m["role"], "content": (m["content"] or "")[:400]}
                  for m in msgs],
    }
    try:
        db.runtime_set(_KEY, json.dumps(payload))
    except Exception:
        logger.exception("write_handoff: runtime_set failed")


def peek_handoff() -> dict | None:
    """Return the stored handoff dict (without clearing) or None if stale/missing."""
    if not _enabled():
        return None
    raw = db.runtime_get(_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    ts_iso = data.get("ts")
    if not ts_iso:
        return None
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
    hours = (datetime.now(UTC) - ts).total_seconds() / 3600
    if hours > _max_gap_hours():
        # Stale — clear and ignore.
        db.runtime_set(_KEY, None)
        return None
    if hours < _min_gap_hours():
        # Mid-session — don't echo what's already in the live context window.
        return None
    return data


def consume_handoff() -> dict | None:
    """Return + clear the handoff so it's only injected once per gap."""
    data = peek_handoff()
    if data is None:
        return None
    db.runtime_set(_KEY, None)
    return data


def format_for_injection(data: dict) -> str:
    ts = data.get("ts", "")
    turns = data.get("turns") or []
    if not turns:
        return ""
    lines = [f"# session handoff (last activity {ts})"]
    for t in turns:
        role = (t.get("role") or "?").upper()
        content = (t.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    out = "\n".join(lines)
    # SPASM Egocentric Context Projection (arxiv 2604.09212): rewrite
    # USER:/ASSISTANT: labels into [partner]:/[self]: so Hikari reads this
    # handoff as her own first-person memory instead of a third-person log.
    # Documented Cohen's d=-0.75 drop on emotion drift over 18-turn chats.
    from . import ecp
    return ecp.maybe_project(out)
