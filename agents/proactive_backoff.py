"""Ignore-driven backoff for proactive sources (Sprint 1, send-iff rule).

A source ignored ``consecutive_ignore_threshold`` times running is snoozed
for ``snooze_days`` by writing into the SAME ``proactive_snooze_until``
runtime_state map the gate already checks (agents/proactive_gate._snooze_active)
— so suppression, status display, and un-snooze tooling all work unchanged.

One plain stand-down notice per suppression, tracked in
``backoff_notice_sent_v1`` so it never repeats. Never guilt copy.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)

_NOTICE_KEY = "backoff_notice_sent_v1"
_SNOOZE_KEY = "proactive_snooze_until"

# Deterministic, in-voice, no-LLM stand-down line. {label} is the source id
# with underscores swapped for spaces.
_STANDDOWN_TEMPLATE = (
    "i'll stop pinging about {label} — you weren't answering, so it's paused. "
    "say the word if you want it back."
)


def _cfg(key: str, default):
    return cfg.get(f"proactive_backoff.{key}", default)


def _tracked_sources() -> list[str]:
    """Sources that have ever sent, minus exemptions."""
    exempt = set(_cfg("exempt_sources", []) or [])
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source FROM proactive_events WHERE status='sent'"
        ).fetchall()
    return [r[0] for r in rows if r[0] not in exempt]


def consecutive_ignores(source: str) -> int:
    """Count unanswered sends, newest-first, stopping at the first answered one."""
    window_h = float(_cfg("response_window_hours", 6))
    lookback = int(_cfg("consecutive_ignore_threshold", 3)) + 2
    count = 0
    for ev in db.proactive_events_recent_sent(source, limit=lookback):
        if db.user_message_after(ev["sent_at"], within_hours=window_h):
            break
        count += 1
    return count


def _load_map(key: str) -> dict:
    raw = db.runtime_get(key) or ""
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _already_snoozed(source: str) -> bool:
    iso = _load_map(_SNOOZE_KEY).get(source)
    if not iso:
        return False
    try:
        until = datetime.fromisoformat(iso)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return datetime.now(UTC) < until
    except (ValueError, TypeError):
        return False


async def run_backoff_sweep(send_text) -> list[str]:
    """Scheduler entry. Returns newly suppressed source ids."""
    if not bool(_cfg("enabled", True)):
        return []
    threshold = int(_cfg("consecutive_ignore_threshold", 3))
    newly: list[str] = []
    for source in _tracked_sources():
        if _already_snoozed(source):
            continue
        if consecutive_ignores(source) < threshold:
            continue
        snooze = _load_map(_SNOOZE_KEY)
        until = datetime.now(UTC) + timedelta(days=float(_cfg("snooze_days", 14)))
        snooze[source] = until.isoformat()
        db.runtime_set(_SNOOZE_KEY, json.dumps(snooze))
        newly.append(source)
        logger.info("proactive_backoff: suppressed %r until %s", source, until)

        notices = _load_map(_NOTICE_KEY)
        if source in notices:
            continue  # suppressed before, notice already sent once
        notices[source] = datetime.now(UTC).isoformat()
        db.runtime_set(_NOTICE_KEY, json.dumps(notices))
        text = _STANDDOWN_TEMPLATE.format(label=source.replace("_", " "))
        from agents.proactive_gate import reserve_and_send
        result = await reserve_and_send(
            send_text_fn=send_text,
            producer_id="backoff_notice",
            pattern="notify",
            text=text,
            candidate={
                "anchor": source,
                "why_now": f"{source} ignored {threshold}x running",
                "suggested_action": "re-enable via set_proactive_source",
                "confidence": 1.0,
                "controls": {"mute_source": source},
                "data_checked": ["proactive_events", "messages"],
            },
        )
        if result.status != "sent":
            logger.info(
                "proactive_backoff: notice for %r aborted (%s) — "
                "suppression stands, notice not retried",
                source, result.reason,
            )
    return newly
