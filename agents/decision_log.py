"""Decision-log resolver. Weekly Sunday 19:00 — finds decisions whose
resolve_by has passed and asks the user about up to N per run. Marks them
as asked so we don't re-ask the same one every week (cooldown logic can
extend this later)."""
from __future__ import annotations

import logging

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)


async def run_decision_resolver(send_text) -> int:
    """Surface unresolved-and-overdue decisions to the user. Returns the
    number of decisions asked about this call."""
    if not bool(cfg.get("decision_log.enabled", True)):
        return 0
    max_per_run = int(cfg.get("decision_log.max_per_week_ask", 3))
    overdue = db.decisions_unresolved_due(limit=max_per_run)
    if not overdue:
        return 0

    asked = 0
    for d in overdue:
        line = (
            f"calibration check: '{d['statement']}' "
            f"(you said {d['predicted_p']}). did it happen? yes / no."
        )
        try:
            await send_text(line)
            db.decision_mark_asked(int(d["id"]))
            asked += 1
        except Exception:
            logger.exception(
                "decision_resolver: send failed for decision_id=%s",
                d["id"],
            )
    logger.info("decision_resolver: asked about %d overdue decisions", asked)
    return asked
