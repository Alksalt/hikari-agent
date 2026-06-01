"""Phase T: belief_journal DB helpers, regex detectors, and belief_resurface producer."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers: fresh in-memory DB wired to db.py helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Spin up a fresh isolated DB for each test."""
    db_path = tmp_path / "test_belief.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))

    # Force db module to re-initialise with the new path.
    import importlib

    import storage.db as db_mod
    importlib.reload(db_mod)

    yield db_mod

    importlib.reload(db_mod)  # restore defaults after test


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def test_insert_factual_belief(tmp_db):
    row_id = tmp_db.belief_journal_insert(
        statement="i will stop working weekends by june",
        claim_type="factual",
    )
    assert row_id > 0
    with tmp_db._conn() as c:
        row = c.execute(
            "SELECT claim_type, resolved_bool, resurface_at FROM belief_journal WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["claim_type"] == "factual"
    assert row["resolved_bool"] == 0
    resurface = datetime.fromisoformat(row["resurface_at"])
    expected = datetime.now(UTC) + timedelta(days=90)
    # Allow ±60s tolerance for slow CI
    assert abs((resurface - expected).total_seconds()) < 60


def test_insert_identity_belief(tmp_db):
    row_id = tmp_db.belief_journal_insert(
        statement="i'm someone who ships on fridays",
        claim_type="identity",
    )
    assert row_id > 0
    with tmp_db._conn() as c:
        row = c.execute(
            "SELECT claim_type FROM belief_journal WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["claim_type"] == "identity"


def test_insert_invalid_claim_type_raises(tmp_db):
    with pytest.raises(ValueError, match="invalid claim_type"):
        tmp_db.belief_journal_insert(
            statement="whatever",
            claim_type="opinion",
        )


def test_due_returns_matured(tmp_db):
    # Insert a belief that is already past due (resurface in the past).
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with tmp_db._conn() as c:
        c.execute(
            "INSERT INTO belief_journal (stated_at, statement, claim_type, resurface_at, resolved_bool) "
            "VALUES (?, ?, ?, ?, 0)",
            (tmp_db._now(), "i'll exercise every day", "factual", past),
        )
        c.commit()

    due = tmp_db.belief_journal_due()
    assert len(due) == 1
    assert due[0]["statement"] == "i'll exercise every day"


def test_due_skips_resolved(tmp_db):
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with tmp_db._conn() as c:
        c.execute(
            "INSERT INTO belief_journal (stated_at, statement, claim_type, resurface_at, resolved_bool) "
            "VALUES (?, ?, ?, ?, 1)",
            (tmp_db._now(), "resolved belief", "factual", past),
        )
        c.commit()

    due = tmp_db.belief_journal_due()
    assert due == []


def test_resolve_marks_resolved(tmp_db):
    row_id = tmp_db.belief_journal_insert(
        statement="i plan to learn rust",
        claim_type="factual",
    )
    tmp_db.belief_journal_resolve(row_id, note="checked: no")
    with tmp_db._conn() as c:
        row = c.execute(
            "SELECT resolved_bool, resolution_note FROM belief_journal WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["resolved_bool"] == 1
    assert row["resolution_note"] == "checked: no"


# ---------------------------------------------------------------------------
# Regex detectors
# ---------------------------------------------------------------------------

def test_future_tense_regex_matches_i_will():
    from agents.belief_frame import detect_future_belief
    hit, fragment = detect_future_belief("i will ship by friday")
    assert hit
    assert fragment is not None


def test_future_tense_regex_matches_going_to():
    from agents.belief_frame import detect_future_belief
    hit, fragment = detect_future_belief("i'm going to quit coffee")
    assert hit
    assert fragment is not None


def test_future_tense_regex_matches_plan_to():
    from agents.belief_frame import detect_future_belief
    hit, fragment = detect_future_belief("i plan to refactor the codebase")
    assert hit


def test_future_tense_regex_skips_past_tense():
    from agents.belief_frame import detect_future_belief
    hit, _ = detect_future_belief("i thought about quitting coffee")
    assert not hit


def test_future_tense_regex_skips_going_to_bed():
    from agents.belief_frame import detect_future_belief
    hit, _ = detect_future_belief("gonna sleep early tonight")
    assert not hit


def test_identity_regex_matches_im_someone_who():
    from agents.belief_frame import detect_identity_claim
    hit, fragment = detect_identity_claim("i'm someone who ships on fridays")
    assert hit
    assert fragment is not None


def test_identity_regex_matches_i_dont():
    from agents.belief_frame import detect_identity_claim
    hit, fragment = detect_identity_claim("i don't procrastinate on important work")
    assert hit


def test_identity_regex_matches_i_never():
    from agents.belief_frame import detect_identity_claim
    hit, _ = detect_identity_claim("i never miss a deadline")
    assert hit


def test_identity_regex_skips_questions():
    from agents.belief_frame import detect_identity_claim
    hit, _ = detect_identity_claim("am i someone who gives up easily?")
    assert not hit


def test_identity_regex_skips_second_person():
    from agents.belief_frame import detect_identity_claim
    hit, _ = detect_identity_claim("you're someone who ships fast")
    assert not hit


# ---------------------------------------------------------------------------
# belief_resurface producer
# ---------------------------------------------------------------------------

def test_belief_resurface_producer_returns_empty_below_stage(tmp_db, monkeypatch):
    """Stage < 3 → producer emits nothing."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(Path(tmp_db._DB_PATH)))
    # Set stage to 2.
    tmp_db.runtime_set("relationship_stage", "2")

    from agents.engagement.producers import belief_resurface
    candidates = belief_resurface.collect()
    assert candidates == []


def test_belief_resurface_producer_emits_matured(tmp_db, monkeypatch):
    """Seed a past-due belief at stage 3 → producer emits a TriggerCandidate."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(Path(tmp_db._DB_PATH)))
    tmp_db.runtime_set("relationship_stage", "3")
    # Clear any existing session so per-session cap doesn't fire.
    tmp_db.runtime_set("belief_resurface_last_session_id", "")

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with tmp_db._conn() as c:
        c.execute(
            "INSERT INTO belief_journal (stated_at, statement, claim_type, resurface_at, resolved_bool) "
            "VALUES (?, ?, ?, ?, 0)",
            (tmp_db._now(), "i will stop working weekends", "factual", past),
        )
        c.commit()

    # Force producer module to see the new DB path.
    import importlib

    import agents.engagement.producers.belief_resurface as br_mod
    importlib.reload(br_mod)

    candidates = br_mod.collect()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source == "belief_resurface"
    assert "i will stop working weekends" in c.payload["statement"]
    assert c.payload["claim_type"] == "factual"
