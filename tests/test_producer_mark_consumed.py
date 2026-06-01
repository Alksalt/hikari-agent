"""tests/test_producer_mark_consumed.py — Phase 3: producer mark_consumed signature fix.

Verifies that the three previously-mismatched producers:
  1. Accept mark_consumed(candidate: TriggerCandidate) without raising.
  2. Write the expected side-effect on the database.

Regression guard: simulates the scheduler call pattern
  getattr(mod, "mark_consumed")(candidate)
to confirm no TypeError is raised.
"""
from __future__ import annotations

import importlib
import logging
from datetime import UTC, datetime, timedelta

import pytest

from agents.engagement.triggers import TriggerCandidate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Fresh DB + runtime_state isolated to this test."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as db_mod
    importlib.reload(db_mod)
    db_mod._reset_schema_sentinel()
    db_mod.get_session_id()
    yield db_mod


def _make_candidate(source: str, payload: dict) -> TriggerCandidate:
    return TriggerCandidate(
        source=source,
        pool="agent_spontaneous",
        pattern="notify",
        payload=payload,
        dedup_key=f"{source}:test",
        decay_at=datetime.now(UTC) + timedelta(hours=1),
        novelty=0.8,
        actionability=0.5,
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# research_callback
# ---------------------------------------------------------------------------


def test_research_callback_mark_consumed_sets_surfaced_at(isolated_db):
    """mark_consumed writes research_surfaced_at for the given task_id."""
    import storage.db as db

    task_id = db.create_task(
        subject="Research the best coffee",
        research_intent=True,
    )
    # Seed required columns so the row matches what collect() would find.
    with db._conn() as c:
        c.execute(
            "UPDATE tasks SET research_summary = 'Great coffee exists.' "
            "WHERE id = ?",
            (task_id,),
        )

    import agents.engagement.producers.research_callback as mod
    importlib.reload(mod)

    candidate = _make_candidate("research_callback", {"task_id": task_id, "subject": "x"})
    mod.mark_consumed(candidate)

    with db._conn() as c:
        row = c.execute(
            "SELECT research_surfaced_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert row is not None
    assert row["research_surfaced_at"] is not None, "research_surfaced_at must be set"


def test_research_callback_mark_consumed_no_raise_missing_task_id(isolated_db, caplog):
    """Missing task_id: no raise, no marker, and an observable ERROR (not silent)."""
    import agents.engagement.producers.research_callback as mod
    importlib.reload(mod)

    candidate = _make_candidate("research_callback", {})
    with caplog.at_level(logging.ERROR, logger="agents.engagement.producers.research_callback"):
        mod.mark_consumed(candidate)  # must not raise
    assert any("task_id missing" in r.message for r in caplog.records), \
        "missing task_id must log at ERROR, not silently no-op"


def test_research_callback_scheduler_call_pattern(isolated_db):
    """Scheduler calls getattr(mod, 'mark_consumed')(candidate) — must not TypeError."""
    import agents.engagement.producers.research_callback as mod
    import storage.db as db
    importlib.reload(mod)

    task_id = db.create_task(subject="Scheduler pattern test", research_intent=True)
    candidate = _make_candidate("research_callback", {"task_id": task_id})
    getattr(mod, "mark_consumed")(candidate)  # must not raise


# ---------------------------------------------------------------------------
# belief_resurface
# ---------------------------------------------------------------------------


def test_belief_resurface_mark_consumed_resolves_belief(isolated_db):
    """mark_consumed resolves the belief_journal row and sets the session marker."""
    import storage.db as db

    # Insert a matured (overdue) belief.
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    belief_id = db.belief_journal_insert(
        statement="I will stop working weekends.",
        claim_type="identity",
    )
    # Force resurface_at into the past so it counts as due.
    with db._conn() as c:
        c.execute(
            "UPDATE belief_journal SET resurface_at = ? WHERE id = ?",
            (past, belief_id),
        )

    # Seed a session so the session marker can be written.
    db.set_session_id("test-session-belief")

    import agents.engagement.producers.belief_resurface as mod
    importlib.reload(mod)

    candidate = _make_candidate(
        "belief_resurface",
        {
            "belief_id": belief_id,
            "statement": "I will stop working weekends.",
            "claim_type": "identity",
            "stated_at": past,
        },
    )
    mod.mark_consumed(candidate)

    # Belief row must now be resolved.
    with db._conn() as c:
        row = c.execute(
            "SELECT resolved_bool, resolution_note FROM belief_journal WHERE id = ?",
            (belief_id,),
        ).fetchone()

    assert row is not None
    assert row["resolved_bool"] == 1, "belief must be marked resolved"
    assert row["resolution_note"] == "surfaced"

    # Session marker must also be set.
    marker = db.runtime_get("belief_resurface_last_session_id")
    assert marker == "test-session-belief"


def test_belief_resurface_mark_consumed_no_raise_missing_belief_id(isolated_db, caplog):
    """Missing belief_id: session marker still set, belief NOT resolved, observable ERROR."""
    import storage.db as db
    db.set_session_id("test-session-no-belief")

    import agents.engagement.producers.belief_resurface as mod
    importlib.reload(mod)

    candidate = _make_candidate("belief_resurface", {})
    with caplog.at_level(logging.ERROR, logger="agents.engagement.producers.belief_resurface"):
        mod.mark_consumed(candidate)  # must not raise
    assert any("belief_id missing" in r.message for r in caplog.records), \
        "missing belief_id must log at ERROR, not silently no-op"

    # Session marker is still written (gates within-session re-fire).
    marker = db.runtime_get("belief_resurface_last_session_id")
    assert marker == "test-session-no-belief"


def test_belief_resurface_scheduler_call_pattern(isolated_db):
    """Scheduler calls getattr(mod, 'mark_consumed')(candidate) — must not TypeError."""
    import agents.engagement.producers.belief_resurface as mod
    import storage.db as db
    importlib.reload(mod)

    belief_id = db.belief_journal_insert(
        statement="I will exercise daily.", claim_type="factual"
    )
    candidate = _make_candidate("belief_resurface", {"belief_id": belief_id})
    getattr(mod, "mark_consumed")(candidate)  # must not raise


# ---------------------------------------------------------------------------
# anniversary_callback
# ---------------------------------------------------------------------------


def test_anniversary_callback_mark_consumed_sets_session_marker(isolated_db):
    """mark_consumed writes the per-session marker to runtime_state."""
    import storage.db as db
    db.set_session_id("test-session-anniv")

    import agents.engagement.producers.anniversary_callback as mod
    importlib.reload(mod)

    candidate = _make_candidate(
        "anniversary_callback",
        {
            "anniversary_date": "2023-06-01",
            "years_back": 3,
            "kind": "lexicon",
            "summary": "Met for coffee",
        },
    )
    mod.mark_consumed(candidate)

    marker = db.runtime_get("anniversary_callback_last_session_id")
    assert marker == "test-session-anniv"


def test_anniversary_callback_mark_consumed_no_raise_no_session(isolated_db, monkeypatch, caplog):
    """No active session: no raise, marker not written, observable WARNING."""
    import agents.engagement.producers.anniversary_callback as mod
    import storage.db as db
    importlib.reload(mod)

    monkeypatch.setattr(db, "get_session_id", lambda: "")
    candidate = _make_candidate("anniversary_callback", {})
    with caplog.at_level(logging.WARNING, logger="agents.engagement.producers.anniversary_callback"):
        mod.mark_consumed(candidate)  # must not raise
    assert any("no active session_id" in r.message for r in caplog.records), \
        "no-session must log at WARNING, not silently no-op"
    assert db.runtime_get("anniversary_callback_last_session_id") is None


def test_anniversary_callback_scheduler_call_pattern(isolated_db):
    """Scheduler calls getattr(mod, 'mark_consumed')(candidate) — must not TypeError."""
    import agents.engagement.producers.anniversary_callback as mod
    import storage.db as db
    importlib.reload(mod)

    db.set_session_id("test-session-sched")
    candidate = _make_candidate("anniversary_callback", {"years_back": 2})
    getattr(mod, "mark_consumed")(candidate)  # must not raise

    marker = db.runtime_get("anniversary_callback_last_session_id")
    assert marker == "test-session-sched"
