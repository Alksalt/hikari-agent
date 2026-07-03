"""One-tap capability offers — deterministic post-task discoverability.

After a successful task turn the bridge may attach ONE inline button
offering an adjacent capability the owner hasn't discovered yet. Selection
is plain code over tool_calls telemetry + the curated offer catalog in
config/engagement.yaml — the model is never asked to generate offers, so
persona rules can't be violated and rationing is exact.

Lifecycle: shown → tapped (button fires the phrase as a normal agent turn,
Task 9) or ignored (lazily marked when the next offer is selected). An
offer ignored `drop_after_ignored` times in a row is never shown again
(research rule: drop if declined, 2026-07-02 review).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)


def _catalog() -> list[dict]:
    entries = cfg.get("capability_offers.catalog") or []
    return [e for e in entries if isinstance(e, dict) and e.get("id") and e.get("phrase")]


def catalog_entry(offer_id: str) -> dict | None:
    for e in _catalog():
        if str(e["id"]) == offer_id:
            return e
    return None


def _domains_used(turn_elapsed_sec: float) -> set[str]:
    """Domains of tools that ran during this turn (by started_at window)."""
    since = (
        datetime.now(UTC) - timedelta(seconds=float(turn_elapsed_sec) + 30.0)
    ).isoformat()
    used_ids = db.tool_calls_used_since(since)
    if not used_ids:
        return set()
    from tools.catalog import get_catalog
    by_name = {e.name: e.domain for e in get_catalog().entries}
    return {by_name[t] for t in used_ids if t in by_name}


def select_offer(*, turn_elapsed_sec: float) -> dict | None:
    """Pick at most one offer for this turn. Pure read — no side effects."""
    if not bool(cfg.get("capability_offers.enabled", True)):
        return None
    used_domains = _domains_used(turn_elapsed_sec)
    if not used_domains:
        return None

    drop_after = int(cfg.get("capability_offers.drop_after_ignored", 2))
    min_gap_days = int(cfg.get("capability_offers.min_days_between_same_offer", 7))
    unused_days = int(cfg.get("capability_offers.unused_window_days", 30))
    unused_cutoff = (datetime.now(UTC) - timedelta(days=unused_days)).isoformat()
    gap_cutoff = (datetime.now(UTC) - timedelta(days=min_gap_days)).isoformat()

    candidates: list[tuple[str, dict]] = []  # (sort_key, entry)
    for entry in _catalog():
        if not (set(entry.get("domains") or []) & used_domains):
            continue
        # Already discovered — any of its tools ran inside the window.
        last_used = db.tool_calls_last_used(list(entry.get("tool_ids") or []))
        if any(ts >= unused_cutoff for ts in last_used.values()):
            continue
        outcomes = db.capability_offer_recent_outcomes(str(entry["id"]), limit=drop_after)
        if len(outcomes) >= drop_after and all(o == "ignored" for o in outcomes):
            continue
        last_shown = db.capability_offer_last_shown(str(entry["id"]))
        if last_shown and last_shown >= gap_cutoff:
            continue
        candidates.append((last_shown or "", entry))  # never-shown sorts first

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


async def maybe_offer(
    *, chat_id: int, turn_elapsed_sec: float, telegram_message_id: int | None
) -> str | None:
    """Attach at most one offer button to the just-sent reply.

    Returns the offer_id shown, or None. Never raises."""
    try:
        if not bool(cfg.get("capability_offers.enabled", True)):
            return None
        if telegram_message_id is None:
            return None
        if db.capability_offers_today_count() >= int(
            cfg.get("capability_offers.max_per_day", 1)
        ):
            return None
        # Lazy decline detection: anything still 'shown' was never tapped.
        db.capability_offer_mark_stale_ignored()
        entry = select_offer(turn_elapsed_sec=turn_elapsed_sec)
        if entry is None:
            return None
        offer_id = str(entry["id"])
        row_id = db.capability_offer_insert(
            offer_id=offer_id, telegram_message_id=telegram_message_id
        )
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                str(entry.get("label") or offer_id),
                callback_data=f"offer:go:{row_id}:{offer_id}",
            )
        ]])
        from agents.telegram_bridge import attach_keyboard_to_sent_message

        attached = await attach_keyboard_to_sent_message(telegram_message_id, kb)
        if not attached:
            db.capability_offer_delete(row_id)
            return None
        logger.info("capability_offers: shown %r (row %d)", offer_id, row_id)
        return offer_id
    except Exception:
        logger.exception("capability_offers: maybe_offer failed (non-fatal)")
        return None
