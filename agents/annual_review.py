"""Annual review ceremony — late December year synthesis.

Fires Dec 26-31. Two-section Hikari-voiced summary: things worth more of /
things worth less of. Sources: episodes (top emotional weight), receipts
(by category), decisions resolved (Brier for year), drift canary divergences.

Uses the cheap aux LLM (OpenRouter DeepSeek) for composition. Result is
sent as a Telegram message and stored in episodes table as a year-end
artifact.
"""
from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


def _is_review_window(today: date | None = None) -> bool:
    """True if today is in the December 26-31 review window."""
    d = today or date.today()
    return d.month == 12 and d.day >= 26


def _already_run_this_year(year: int | None = None) -> bool:
    """Check if annual review already fired this year (idempotent)."""
    from storage import db
    y = year if year is not None else date.today().year
    last = db.runtime_get("annual_review_last_year")
    if not last:
        return False
    try:
        return int(last) == y
    except (ValueError, TypeError):
        return False


def _gather_year_data(year: int) -> dict:
    """Pull aggregate data for the year. Returns dict with sections."""
    from storage import db
    year_start = f"{year}-01-01T00:00:00+00:00"
    year_end = f"{year + 1}-01-01T00:00:00+00:00"

    data: dict = {
        "year": year,
        "top_episodes": [],
        "receipts_by_category": {},
        "decisions_resolved_count": 0,
        "decisions_brier": None,
        "drift_class_counts": {},
    }

    try:
        with db._conn() as c:
            # Top episodes by emotional weight.
            rows = c.execute(
                """
                SELECT id, date, summary, importance
                FROM episodes
                WHERE date >= ? AND date < ?
                ORDER BY importance DESC, date ASC
                LIMIT 10
                """,
                (year_start[:10], year_end[:10]),
            ).fetchall()
            data["top_episodes"] = [dict(r) for r in rows]

            # Receipts by category.
            try:
                rcat = c.execute(
                    """
                    SELECT category, COUNT(*) AS n
                    FROM receipts
                    WHERE created_at >= ? AND created_at < ?
                    GROUP BY category
                    ORDER BY n DESC
                    """,
                    (year_start, year_end),
                ).fetchall()
                data["receipts_by_category"] = {r["category"]: int(r["n"]) for r in rcat}
            except Exception:
                # receipts table may not exist on all installs
                pass

            # Decision resolution count.
            try:
                rd = c.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM decisions
                    WHERE resolved_at >= ? AND resolved_at < ?
                    """,
                    (year_start, year_end),
                ).fetchone()
                data["decisions_resolved_count"] = int(rd["n"] or 0)
            except Exception:
                pass

            # Drift class counts.
            try:
                rdc = c.execute(
                    """
                    SELECT class_label, COUNT(*) AS n
                    FROM persona_drift_scores
                    WHERE ts >= ? AND ts < ?
                    GROUP BY class_label
                    """,
                    (year_start, year_end),
                ).fetchall()
                data["drift_class_counts"] = {r["class_label"]: int(r["n"]) for r in rdc}
            except Exception:
                pass
    except Exception:
        logger.exception("annual_review: data gather failed")

    # Brier score for the year (reuse existing helper).
    try:
        # window_days big enough to span the year
        brier = db.decision_brier_score(window_days=400)
        if brier and brier.get("n", 0) > 0:
            data["decisions_brier"] = brier
    except Exception:
        pass

    return data


def _build_review_prompt(data: dict) -> str:
    """Compose the aux-LLM prompt asking for a Hikari-voiced two-section summary."""
    y = data["year"]
    episodes = data.get("top_episodes", [])
    receipts = data.get("receipts_by_category", {})
    n_resolved = data.get("decisions_resolved_count", 0)
    brier = data.get("decisions_brier")
    drift = data.get("drift_class_counts", {})

    lines = [
        f"You are Hikari Tsukino. Compose a private year-end note to him about {y}.",
        "",
        "Voice rules: lowercase, dry, observational. 1-4 sentences max per item.",
        "Format: two sections — `things worth more of` and `things worth less of`.",
        "Each section: 3-5 bullets. Bullets are observations of HIS year, not yours.",
        "End with one short line — not a question, not advice.",
        "",
        "Data to weave in (only the parts that feel real — skip anything thin):",
        "",
        "## Top episodes by importance",
    ]
    for ep in episodes[:8]:
        lines.append(f"- {ep.get('date','')}: {(ep.get('summary') or '')[:140]}")
    lines.append("")
    lines.append("## Receipts by category")
    for cat, n in receipts.items():
        lines.append(f"- {cat}: {n} entries")
    lines.append("")
    lines.append(f"## Decisions resolved: {n_resolved}")
    if brier:
        lines.append(f"- Brier rolling score: {brier.get('brier', 0):.3f} (n={brier.get('n', 0)})")
        lines.append(f"- Mean predicted: {brier.get('mean_predicted', 0):.2f}, mean outcome: {brier.get('mean_outcome', 0):.2f}")
    lines.append("")
    lines.append("## Drift class counts")
    for cls, n in drift.items():
        lines.append(f"- {cls}: {n}")
    lines.append("")
    lines.append("Output the two sections + closing line. No preamble, no markdown headers.")
    return "\n".join(lines)


async def compose_annual_review(year: int) -> str | None:
    """Compose the review text via aux LLM. Returns None on failure."""
    from agents.runtime import _call_aux_llm
    from agents import config as cfg

    data = _gather_year_data(year)
    prompt = _build_review_prompt(data)

    model = str(cfg.get("annual_review.model", cfg.get("aux_model.openrouter_model", "deepseek/deepseek-v4-flash")))
    try:
        out = await _call_aux_llm(
            prompt,
            system="You are Hikari Tsukino. Follow the voice rules in the user message exactly.",
            model=model,
            max_tokens=600,
        )
    except Exception:
        logger.exception("annual_review: compose failed")
        return None
    return (out or "").strip() or None


async def run_annual_review(send_text=None, force: bool = False) -> bool:
    """Fire the ceremony if in window + not already run. Returns True if sent.

    Follows the scheduler-job pattern used by drift_canary / decision_log:
    receives ``send_text`` (an async callable that hides the bot reference)
    so this module doesn't need to know how to construct the bot itself.
    """
    from agents import config as cfg
    from storage import db

    if not bool(cfg.get("annual_review.enabled", True)):
        return False
    if not force:
        if not _is_review_window():
            return False
        if _already_run_this_year():
            return False

    year = date.today().year - 1 if date.today().month == 1 else date.today().year

    text = await compose_annual_review(year)
    if not text:
        logger.warning("annual_review: empty composition, skipping")
        return False

    # Mark idempotent before sending so a retry doesn't double-fire.
    db.runtime_set("annual_review_last_year", str(year))

    if send_text is None:
        logger.warning("annual_review: composed but no send_text — idempotent flag stays set")
        return False
    try:
        await send_text(f"# year in review — {year}\n\n{text}")
    except Exception:
        logger.exception("annual_review: send_text raised (idempotent flag stays set)")
        return False
    return True
