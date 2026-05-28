"""Phase H: stale_pr_check producer tests."""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    from agents import config
    config.reload()


def _make_pr(
    *,
    state: str = "open",
    review_count: int = 0,
    age_hours: float = 96.0,
    branch: str = "refactor/session-store",
    title: str = "Refactor session store",
    html_url: str = "https://github.com/owner/repo/pull/1",
) -> dict:
    created_at = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    return {
        "state": state,
        "review_count": review_count,
        "created_at": created_at,
        "head_ref": branch,
        "title": title,
        "html_url": html_url,
    }


def test_returns_empty_when_no_cache():
    """No stale_pr_cache_json in runtime_state → empty list."""
    from agents.engagement.producers import stale_pr_check
    result = stale_pr_check.collect()
    assert result == []


def test_returns_empty_when_no_old_prs():
    """Cache has PRs but all created <72h ago → empty list."""
    from storage import db
    from agents.engagement.producers import stale_pr_check

    prs = [_make_pr(age_hours=24), _make_pr(age_hours=48, branch="feat/other")]
    db.runtime_set("stale_pr_cache_json", json.dumps(prs))

    result = stale_pr_check.collect()
    assert result == []


def test_emits_oldest_pr_with_zero_reviews():
    """Cache has 3 PRs: one <72h, one >72h with reviews, one >72h with 0 reviews → 1 candidate."""
    from storage import db
    from agents.engagement.producers import stale_pr_check

    prs = [
        _make_pr(age_hours=24, branch="feat/new"),          # too fresh
        _make_pr(age_hours=100, review_count=2, branch="fix/bug"),  # has reviews
        _make_pr(age_hours=96, review_count=0, branch="refactor/session-store"),  # should fire
    ]
    db.runtime_set("stale_pr_cache_json", json.dumps(prs))

    result = stale_pr_check.collect()
    assert len(result) == 1
    c = result[0]
    assert c.source == "stale_pr_check"
    assert c.payload["branch"] == "refactor/session-store"


def test_filters_out_prs_with_reviews():
    """Old PR with review_count > 0 → not selected."""
    from storage import db
    from agents.engagement.producers import stale_pr_check

    prs = [_make_pr(age_hours=120, review_count=1, branch="fix/stale-with-review")]
    db.runtime_set("stale_pr_cache_json", json.dumps(prs))

    result = stale_pr_check.collect()
    assert result == []


def test_respects_24h_cooldown():
    """stale_pr_check_last_sent_iso set <24h ago → returns empty immediately."""
    from storage import db
    from agents.engagement.producers import stale_pr_check

    # Put a valid stale PR in the cache
    prs = [_make_pr(age_hours=96)]
    db.runtime_set("stale_pr_cache_json", json.dumps(prs))

    # Simulate a send 6h ago
    recent = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    db.runtime_set("stale_pr_check_last_sent_iso", recent)

    result = stale_pr_check.collect()
    assert result == []


def test_payload_contains_branch_title_url_age():
    """Candidate payload must include branch, title, url, age_hours, age_days_rounded."""
    from storage import db
    from agents.engagement.producers import stale_pr_check

    pr = _make_pr(
        age_hours=96,
        branch="refactor/session-store",
        title="Refactor session store",
        html_url="https://github.com/owner/repo/pull/42",
    )
    db.runtime_set("stale_pr_cache_json", json.dumps([pr]))

    result = stale_pr_check.collect()
    assert len(result) == 1
    p = result[0].payload
    assert p["branch"] == "refactor/session-store"
    assert p["title"] == "Refactor session store"
    assert p["url"] == "https://github.com/owner/repo/pull/42"
    assert p["age_hours"] >= 96
    assert p["age_days_rounded"] >= 4.0
