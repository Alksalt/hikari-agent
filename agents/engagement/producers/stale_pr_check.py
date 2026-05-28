"""Stale PR producer: oldest open PR with >72h age + 0 reviews."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC
from agents.engagement.triggers import TriggerCandidate

logger = logging.getLogger(__name__)


def collect() -> list[TriggerCandidate]:
    """Query GitHub for open PRs via the github subagent's MCP tools, find the
    oldest with >72h age and 0 review comments, emit one candidate.

    Implementation note: this producer doesn't directly hit GitHub — it reads
    from runtime_state where a background poll has cached recent PR data, OR
    fetches lazily via the existing github MCP wrapper.
    """
    from agents import config as cfg
    from storage import db

    if not bool(cfg.get("engagement.stale_pr_check.enabled", True)):
        return []

    # Skip if last sent within 24h (avoid daily nagging).
    last_sent = db.runtime_get("stale_pr_check_last_sent_iso")
    if last_sent:
        try:
            last_dt = datetime.fromisoformat(last_sent)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            if (datetime.now(UTC) - last_dt).total_seconds() < 86400:
                return []
        except (ValueError, TypeError):
            pass

    # Read PR cache from runtime_state (populated by a separate refresher job
    # or by the existing github subagent on-demand).
    import json
    cached = db.runtime_get("stale_pr_cache_json")
    if not cached:
        return []
    try:
        prs = json.loads(cached)
    except (ValueError, TypeError):
        return []
    if not isinstance(prs, list):
        return []

    # Filter: open, >72h since created, 0 reviews.
    cutoff = datetime.now(UTC) - timedelta(hours=72)
    candidates = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        if pr.get("state") != "open":
            continue
        if int(pr.get("review_count", 0)) > 0:
            continue
        created_at_str = pr.get("created_at", "")
        if not created_at_str:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if created_at > cutoff:
            continue
        candidates.append((created_at, pr))

    if not candidates:
        return []

    # Pick oldest.
    candidates.sort(key=lambda x: x[0])
    _, oldest = candidates[0]

    age_hours = int((datetime.now(UTC) - candidates[0][0]).total_seconds() / 3600)
    age_days = age_hours / 24

    title = str(oldest.get("title", "untitled"))[:80]
    branch = str(oldest.get("head_ref", "") or oldest.get("branch", ""))[:60]
    url = str(oldest.get("html_url", ""))[:200]

    candidate = TriggerCandidate(
        source="stale_pr_check",
        pool="user_anchored",
        pattern="notify",
        payload={
            "branch": branch,
            "title": title,
            "url": url,
            "age_hours": age_hours,
            "age_days_rounded": round(age_days, 1),
        },
        dedup_key=f"stale_pr:{branch}",
        decay_at=datetime.now(UTC) + timedelta(hours=6),
        novelty=0.7,
        actionability=0.6,
        confidence=0.85,
    )
    return [candidate]


def mark_consumed() -> None:
    """Update runtime_state so we don't re-surface within 24h."""
    from storage import db
    db.runtime_set("stale_pr_check_last_sent_iso", datetime.now(UTC).isoformat())
