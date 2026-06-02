"""Tests for agents.engagement.producers.anniversary_callback.collect()."""
from __future__ import annotations

import importlib
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _setup(monkeypatch, tmp_path, *, stage: int = 3, session_id: str = "sess-1"):
    """Wire a fresh temp DB, set relationship_stage, reload the producer."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    db._reset_schema_sentinel()

    # Set stage in core_blocks (the canonical store read by db.get_relationship_stage).
    db.upsert_core_block("relationship_stage", str(stage))

    # Set session id.
    db.set_session_id(session_id)

    from agents.engagement.producers import anniversary_callback
    importlib.reload(anniversary_callback)
    return anniversary_callback, db


def _date_years_ago(years: int) -> str:
    """Return ISO date string for today minus `years` years (exact MMDD match)."""
    today = date.today()
    try:
        return today.replace(year=today.year - years).isoformat()
    except ValueError:
        # Feb 29 on non-leap year → use Feb 28.
        return today.replace(year=today.year - years, day=28).isoformat()


def _date_days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_returns_empty_below_stage_3(tmp_path, monkeypatch):
    producer, _ = _setup(monkeypatch, tmp_path, stage=2)
    assert producer.collect() == []


def test_returns_empty_without_anniversaries(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    # Seed a lexicon entry whose first_seen_date is TODAY — no full year yet.
    db.lexicon_record(
        phrase="test phrase",
        source="user_coined",
        origin_kind=None,
        origin_id=None,
    )
    # Override first_seen_date to today (< 1 year ago) — should not match.
    with db._conn() as c:
        c.execute(
            "UPDATE lexicon SET first_seen_date = ? WHERE phrase = ?",
            (date.today().isoformat(), "test phrase"),
        )
    assert producer.collect() == []


def test_emits_lexicon_anniversary(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    anniversary_date = _date_years_ago(1)
    db.lexicon_record(
        phrase="the coffee ritual",
        source="user_coined",
        origin_kind=None,
        origin_id=None,
    )
    with db._conn() as c:
        c.execute(
            "UPDATE lexicon SET first_seen_date = ? WHERE phrase = ?",
            (anniversary_date, "the coffee ritual"),
        )
    candidates = producer.collect()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source == "anniversary_callback"
    assert c.pattern == "notify"
    assert c.payload["kind"] == "lexicon"
    assert "the coffee ritual" in c.payload["summary"]
    assert c.payload["years_back"] == 1
    assert c.dedup_key.startswith("anniversary:lex:")


def test_emits_significant_event_anniversary(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    anniversary_date = _date_years_ago(2)
    db.significant_event_insert(
        event_date=anniversary_date,
        summary="shipped the first version",
        kind="milestone",
    )
    candidates = producer.collect()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source == "anniversary_callback"
    assert c.payload["kind"] == "milestone"
    assert "shipped" in c.payload["summary"]
    assert c.payload["years_back"] == 2


def test_respects_window_pm3_days(tmp_path, monkeypatch):
    """Entry exactly 4 days away (in MMDD) should NOT match (window=3)."""
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    # 4 days away in MMDD, 1 year ago.
    target = date.today() - timedelta(days=4)
    try:
        anniversary_date = target.replace(year=target.year - 1).isoformat()
    except ValueError:
        anniversary_date = target.replace(year=target.year - 1, day=28).isoformat()
    db.significant_event_insert(
        event_date=anniversary_date,
        summary="something four days off",
        kind="good",
    )
    assert producer.collect() == []


def test_oldest_wins(tmp_path, monkeypatch):
    """When multiple matches exist, the one with the oldest year is returned."""
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    older_date = _date_years_ago(3)
    newer_date = _date_years_ago(1)
    db.significant_event_insert(
        event_date=newer_date,
        summary="newer event",
        kind="good",
    )
    db.significant_event_insert(
        event_date=older_date,
        summary="older event",
        kind="hard",
    )
    candidates = producer.collect()
    assert len(candidates) == 1
    assert candidates[0].payload["years_back"] == 3
    assert "older event" in candidates[0].payload["summary"]


def test_respects_per_session_cap(tmp_path, monkeypatch):
    """Second collect() with the same session_id returns []."""
    producer, db = _setup(monkeypatch, tmp_path, stage=3, session_id="sess-cap")
    anniversary_date = _date_years_ago(1)
    db.significant_event_insert(
        event_date=anniversary_date,
        summary="first shipped it",
        kind="milestone",
    )
    first = producer.collect()
    assert len(first) == 1

    # Simulate the scheduler calling mark_consumed after a successful send.
    producer.mark_consumed(first[0])

    second = producer.collect()
    assert second == []


def test_payload_contains_years_back(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    anniversary_date = _date_years_ago(2)
    db.significant_event_insert(
        event_date=anniversary_date,
        summary="two-year memory",
        kind="funny",
    )
    candidates = producer.collect()
    assert len(candidates) == 1
    assert candidates[0].payload["years_back"] == 2
    assert candidates[0].payload["anniversary_date"] == anniversary_date
