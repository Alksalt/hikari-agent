"""Boot-time reaper for stale proactive_events reservations.

Sprint 4B added proactive_gate.reserve_and_send which inserts a row with
status='reserved' before sending. If the process crashes between reservation
and terminal update, the row sits 'reserved' forever — blocking dedup checks.
This module scans for such rows at boot and flips them to 'aborted'.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

STALE_THRESHOLD_SECONDS = 60  # rows reserved longer than this on boot = crash victim


async def reap_stale_reservations() -> int:
    """Flip all proactive_events rows older than STALE_THRESHOLD_SECONDS
    that are still 'reserved' to 'aborted' with reason='crash_during_reservation'.
    Returns count of rows flipped. Called once at boot before scheduler.start()."""
    from storage import db
    cutoff = datetime.now(UTC) - timedelta(seconds=STALE_THRESHOLD_SECONDS)
    rows = db.proactive_events_stale_reserved(cutoff.isoformat())
    flipped = 0
    for row in rows:
        try:
            db.proactive_event_update_terminal(
                row["id"],
                status="aborted",
                aborted_reason="crash_during_reservation",
            )
            flipped += 1
        except Exception:
            logger.exception("reaper failed to flip row %s", row["id"])
    if flipped:
        logger.info("proactive_reaper: flipped %d stale reservations", flipped)
    return flipped
