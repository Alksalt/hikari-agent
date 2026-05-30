"""Decision-log resolver. Weekly Sunday 19:00 — finds decisions whose
resolve_by has passed and asks the user about up to N per run. Marks them
as asked so we don't re-ask the same one every week (cooldown logic can
extend this later)."""
from __future__ import annotations

import json
import logging
from datetime import date

from agents import cadence
from agents import config as cfg
from agents.cadence import Pool
from storage import db

logger = logging.getLogger(__name__)


def _format_calibration_surface(curve: list[dict]) -> str | None:
    """Pick the bucket with the biggest |predicted - actual| gap, render as
    one in-voice line. Returns None if no bucket has enough samples (n>=3)
    or no gap is significant (>= 0.2 absolute)."""
    if not curve:
        return None
    candidates = [b for b in curve if b["n"] >= 3]
    if not candidates:
        return None
    worst = max(candidates, key=lambda b: abs(b["mean_predicted"] - b["actual_rate"]))
    gap = abs(worst["mean_predicted"] - worst["actual_rate"])
    if gap < 0.2:
        return None
    pred_pct = round(worst["mean_predicted"] * 100)
    actual_pct = round(worst["actual_rate"] * 100)
    direction = "overconfident" if worst["mean_predicted"] > worst["actual_rate"] else "underconfident"
    if worst["bucket_low"] >= 0.6:
        zone = "up high"
    elif worst["bucket_high"] <= 0.4:
        zone = "down low"
    else:
        zone = "in the middle"
    return (
        f"you said {pred_pct}% on things that only happened {actual_pct}% of the time. "
        f"{direction} {zone}."
    )


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

    # Calibration curve surface: after processing overdue decisions, check
    # if we have enough resolved data (n >= 8 total) to surface a meaningful
    # per-bucket calibration signal. Tetlock: bucketed feedback drives
    # recalibration 2-3x faster than a scalar Brier score alone.
    curve = db.decision_calibration_curve(window_days=90, buckets=5)
    n_total = sum(b["n"] for b in curve)
    if n_total >= 8:
        surface = _format_calibration_surface(curve)
        if surface:
            iso_week = date.today().strftime("%G-W%V")
            result = await reserve_and_send(
                send_text_fn=send_text,
                producer_id="decision_log",
                pattern="ceremony",
                text=surface,
                payload_json="{}",
                dedup_key=f"decision_log:calibration:{iso_week}",
                candidate={
                    "anchor": f"calibration_{iso_week}",
                    "why_now": "weekly calibration curve surface",
                    "suggested_action": "reflect",
                    "confidence": 0.9,
                    "controls": {},
                    "data_checked": ["decisions"],
                },
            )
            if result.status == "sent":
                logger.info("decision_resolver: calibration surface sent")
            else:
                logger.info(
                    "decision_resolver: calibration surface suppressed (%s)",
                    result.reason,
                )

    return asked
