"""T3.3 — daily consolidation pass.

Covers:
  - Topic-clustered episode summaries land in ``episode_summaries`` (one row
    per topic with the right episode ids).
  - Co-occurrence edges are written into ``fact_relations``.
  - Consolidation failures don't roll back the rest of reflection.
  - DB helpers (``episode_summary_insert``, ``fact_relation_insert``,
    ``fact_relations_for``) behave sanely on edge cases.

LLM calls are stubbed via ``monkeypatch`` so the tests are deterministic
and don't need network / API keys.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config, reflection
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


# ---------- DB helper tests ----------


def test_episode_summary_insert_round_trip():
    """Helper writes a row + serializes episode_ids as JSON; reader parses it back."""
    ep_a = db.insert_episode("2026-05-18", "talked about transformer attention")
    ep_b = db.insert_episode("2026-05-18", "argued about cabbage")
    sid = db.episode_summary_insert(
        topic="code",
        episode_ids=[ep_a, ep_b],
        summary_text="discussion of attention papers and one weird cabbage tangent.",
    )
    assert sid > 0
    rows = db.episode_summaries_recent(topic="code", limit=10)
    assert len(rows) == 1
    item = rows[0]
    assert item["topic"] == "code"
    assert sorted(item["episode_ids"]) == sorted([ep_a, ep_b])
    assert "cabbage" in item["summary_text"]


def test_episode_summary_insert_rejects_empty():
    with pytest.raises(ValueError):
        db.episode_summary_insert(topic="", episode_ids=[], summary_text="x")
    with pytest.raises(ValueError):
        db.episode_summary_insert(topic="work", episode_ids=[], summary_text="")


def test_episode_summaries_recent_filters_by_topic():
    db.insert_episode("2026-05-18", "stand-up notes")
    db.episode_summary_insert(topic="work", episode_ids=[1], summary_text="work s")
    db.episode_summary_insert(topic="code", episode_ids=[1], summary_text="code s")
    work = db.episode_summaries_recent(topic="work")
    code = db.episode_summaries_recent(topic="code")
    assert len(work) == 1 and work[0]["topic"] == "work"
    assert len(code) == 1 and code[0]["topic"] == "code"


def test_fact_relation_insert_round_trip():
    a = db.insert_fact("user", "lives_in", "Oslo")
    b = db.insert_fact("user", "works_at", "research lab")
    eid = db.fact_relation_insert(a, "co_occurs_with", b)
    assert eid > 0
    edges = db.fact_relations_for(a)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["predicate"] == "co_occurs_with"
    assert edge["subject_fact_id"] == a
    assert edge["object_fact_id"] == b
    assert edge["direction"] == "out"
    # Reverse lookup — same edge, marked as 'in' from b's perspective.
    edges_b = db.fact_relations_for(b)
    assert len(edges_b) == 1
    assert edges_b[0]["direction"] == "in"


def test_fact_relation_rejects_self_edge_and_bad_ids():
    a = db.insert_fact("user", "lives_in", "Oslo")
    with pytest.raises(ValueError):
        db.fact_relation_insert(a, "co_occurs_with", a)
    with pytest.raises(ValueError):
        db.fact_relation_insert(0, "x", 1)
    with pytest.raises(ValueError):
        db.fact_relation_insert(1, "", 2)


# ---------- consolidation pass tests ----------


@pytest.mark.asyncio
async def test_reflection_writes_episode_summaries_by_topic(monkeypatch):
    """Stub the LLM to assign two topics across five episodes; expect two
    summary rows, with the right episode ids grouped under each topic."""
    # Five fresh episodes (created_at defaults to now), three 'work' and
    # two 'feelings' per the stubbed tagger below.
    ep_ids = [
        db.insert_episode("2026-05-19", "stand-up — sprint plan landed"),
        db.insert_episode("2026-05-19", "debugged transformer attention bug"),
        db.insert_episode("2026-05-19", "code review for new connector"),
        db.insert_episode("2026-05-19", "talked through anxiety about the demo"),
        db.insert_episode("2026-05-19", "missed his old cat, felt heavy for a bit"),
    ]
    work_set = set(ep_ids[:3])
    feel_set = set(ep_ids[3:])

    async def fake_tag(eps):
        # Group the first three as 'work', the rest as 'feelings'.
        out: dict[int, str] = {}
        for e in eps:
            out[int(e["id"])] = "work" if int(e["id"]) in work_set else "feelings"
        return out

    summarized_topics: list[str] = []

    async def fake_summarize(topic, eps):
        summarized_topics.append(topic)
        return f"summary of {topic} ({len(eps)} episodes)"

    monkeypatch.setattr(reflection, "_tag_topics", fake_tag)
    monkeypatch.setattr(reflection, "_summarize_topic", fake_summarize)

    stats = await reflection._consolidate_yesterday()

    assert stats["topics"] == 2
    assert stats["summaries"] == 2
    assert set(summarized_topics) == {"work", "feelings"}

    work_rows = db.episode_summaries_recent(topic="work")
    feel_rows = db.episode_summaries_recent(topic="feelings")
    assert len(work_rows) == 1
    assert len(feel_rows) == 1
    assert set(work_rows[0]["episode_ids"]) == work_set
    assert set(feel_rows[0]["episode_ids"]) == feel_set


@pytest.mark.asyncio
async def test_reflection_writes_cooccurrence_edges(monkeypatch):
    """Two new facts in the 24h window → one ``co_occurs_with`` edge."""
    # No episodes in window → consolidation skips the summary loop, exercises
    # only the edge + dedup branches.
    a = db.insert_fact("user", "lives_in", "Oslo")
    b = db.insert_fact("user", "works_at", "research lab")

    async def fake_tag(_eps):
        return {}

    async def fake_summarize(_topic, _eps):
        return ""

    monkeypatch.setattr(reflection, "_tag_topics", fake_tag)
    monkeypatch.setattr(reflection, "_summarize_topic", fake_summarize)

    stats = await reflection._consolidate_yesterday()
    assert stats["edges"] == 1

    edges = db.fact_relations_for(a)
    assert len(edges) == 1
    assert edges[0]["predicate"] == "co_occurs_with"
    assert {edges[0]["subject_fact_id"], edges[0]["object_fact_id"]} == {a, b}


@pytest.mark.asyncio
async def test_consolidation_failure_does_not_break_reflection(monkeypatch):
    """If consolidation throws, the rest of reflection's writes must remain.

    We mock the LLM call AND the consolidation helper to raise; the
    reflection should still complete and return the True signal because
    the other extractions wrote rows.
    """
    # Seed at least one episode + fact so the reflection has something to
    # work with.
    db.insert_episode("2026-05-19", "stand-up")

    # Stub the reflection LLM to return a YAML doc that triggers writes.
    async def fake_run_reflection_call(_prompt):
        return (
            "new_facts:\n"
            "  - {subject: 'user', predicate: 'works_at', object: 'lab', "
            "importance: 7, confidence: 0.9}\n"
            "thought: |\n"
            "  this is fine.\n"
        )

    monkeypatch.setattr(reflection, "run_reflection_call", fake_run_reflection_call)

    # Make the consolidation step raise on entry.
    async def boom():
        raise RuntimeError("boom — consolidation explodes")

    monkeypatch.setattr(reflection, "_consolidate_yesterday", boom)

    # Embedding too — skip the model load.
    async def noop_embed(_fact_id, _s, _p, _o):
        return None

    monkeypatch.setattr(reflection, "_embed_fact", noop_embed)

    # Skip morning dispatch (touches the wiki path which doesn't exist in tests).
    monkeypatch.setattr(reflection, "_write_morning_dispatch",
                        lambda *a, **k: None)

    result = await reflection.run_daily_reflection()
    assert result is True
    # The fact landed.
    active = db.active_facts_matching("user", "works_at")
    assert len(active) == 1
    assert active[0]["object"] == "lab"


def test_episode_summary_insert_handles_non_int_ids():
    """Garbage in episode_ids gets filtered, not raised."""
    db.insert_episode("2026-05-18", "test")
    db.episode_summary_insert(
        topic="other",
        episode_ids=[1, "not an int", 3.14, "5"],
        summary_text="mixed ids",
    )
    rows = db.episode_summaries_recent(topic="other")
    assert len(rows) == 1
    # Only the int-castable entries survive — 1 and "5".
    assert sorted(rows[0]["episode_ids"]) == [1, 5]
