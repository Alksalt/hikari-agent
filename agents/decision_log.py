"""Decision-log resolver. Weekly Sunday 19:00 — finds decisions whose
resolve_by has passed and asks the user about up to N per run. Marks them
as asked so we don't re-ask the same one every week (cooldown logic can
extend this later)."""
from __future__ import annotations

import json
import logging

from agents import cadence
from agents import config as cfg
from agents.cadence import Pool
from storage import db

logger = logging.getLogger(__name__)


async def run_decision_resolver(send_text) -> int:
    """Surface unresolved-and-overdue decisions to the user. Returns the
    number of decisions asked about this call."""
    if not bool(cfg.get("decision_log.enabled", True)):
        return 0
    allowed, reason = cadence.can_send("decision_log", Pool.SCHEDULED_CEREMONY)
    if not allowed:
        logger.info("decision_resolver: cadence governor vetoed: %s", reason)
        return 0
    max_per_run = int(cfg.get("decision_log.max_per_week_ask", 3))
    cooldown = int(cfg.get("decision_log.reask_cooldown_days", 14))
    overdue = db.decisions_unresolved_due(limit=max_per_run,
                                          cooldown_days=cooldown)
    if not overdue:
        return 0

    from agents.proactive_gate import reserve_and_send

    asked = 0
    for d in overdue:
        line = (
            f"calibration check: '{d['statement']}' "
            f"(you said {d['predicted_p']}). did it happen? yes / no."
        )
        result = await reserve_and_send(
            send_text_fn=send_text,
            producer_id="decision_log",
            pattern="ceremony",
            text=line,
            payload_json=json.dumps({"decision_id": d["id"]}),
            dedup_key=f"decision_log:{d['id']}",
            candidate={
                "anchor": f"decision_{d['id']}",
                "why_now": f"resolved_by {d['resolve_by']}",
                "suggested_action": "yes/no",
                "confidence": float(d["predicted_p"]),
                "controls": {},
                "data_checked": ["decisions"],
            },
        )
        if result.status != "sent":
            logger.info(
                "decision_resolver: gate skipped decision_id=%s (%s)",
                d["id"], result.reason,
            )
            continue
        try:
            db.decision_mark_asked(int(d["id"]))
        except Exception:
            logger.exception(
                "decision_resolver: mark_asked failed for decision_id=%s",
                d["id"],
            )
        cadence.record_ceremony_sent("decision_log")
        asked += 1
    logger.info("decision_resolver: asked about %d overdue decisions", asked)
    return asked
