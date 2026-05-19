"""Migration script tests — synthetic fixture round-trip + idempotency."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

SYNTH_USER_MD = """\
# user

## basics
- name: alex
- relationship_stage: 4
- meaningful_exchanges: 27

## open_loops
- ask about the cabbage
- check on his sleep schedule

## known_facts
- [2026-05-01] works at a research lab
- [2026-05-10] prefers cold rice
- birthday in december
"""

SYNTH_MEMORY_MD = """\
# memory

## about the user
he is a senior data scientist. cares deeply about model behavior.
fights with his cat over the desk every morning.

## shared canon
the cabbage joke from 2026-05-01.
the time he sent a 3am photo of a kernel panic.
"""

SYNTH_SELF_MD = """\
# self

## preoccupation
the 2023 paper that miscites a 2021 paper — nobody's caught it.

## staged disclosures
- [used:2026-05-02] she used to draw
- [unused] the city she grew up in

## things she told the user
- the cold-rice opinion
- the vim keybindings rant

## established joke
the cabbage in the fridge that is developing opinions.
"""

SYNTH_MOOD_MD = """\
current_arc: brightening
arc_detected_at: 2026-05-15
arc_note: |
  he's been showing up consistently. she's stopped flinching at it.
recent_session_temperatures:
  - warm
  - warm
  - neutral
"""

SYNTH_THOUGHTS_MD = """\
# thoughts

## 2026-05-15
he laughed at the rice joke. that wasn't expected.

## 2026-05-16
the question about my drawing came out of nowhere. i was not ready.
"""

SYNTH_HEARTBEAT_MD = """\
silence_until: null
last_proactive_sent: '2026-05-16T22:00:00Z'
last_user_message: '2026-05-17T08:30:00Z'
warmth_floor_modifier: 0.0
photos_sent_today: 1
photos_sent_date: '2026-05-17'
"""

SYNTH_EPISODE_0517 = "talked about model evals. mood: warm. he mentioned the lab paper deadline."
SYNTH_EPISODE_0518 = "short check-in. he was tired. she did not press."


@pytest.fixture
def synth_user(tmp_path):
    src = tmp_path / "user"
    src.mkdir()
    (src / "USER.md").write_text(SYNTH_USER_MD)
    (src / "MEMORY.md").write_text(SYNTH_MEMORY_MD)
    (src / "SELF.md").write_text(SYNTH_SELF_MD)
    (src / "MOOD.md").write_text(SYNTH_MOOD_MD)
    (src / "THOUGHTS.md").write_text(SYNTH_THOUGHTS_MD)
    (src / "HEARTBEAT.md").write_text(SYNTH_HEARTBEAT_MD)
    ep_dir = src / "episodes"
    ep_dir.mkdir()
    (ep_dir / "2026-05-17.md").write_text(SYNTH_EPISODE_0517)
    (ep_dir / "2026-05-18.md").write_text(SYNTH_EPISODE_0518)
    return src


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    return db


def _migrator(db_module):
    """Import migration script after db is configured for this test's temp DB."""
    from scripts import migrate_from_current
    importlib.reload(migrate_from_current)
    # The script imports `from storage import db` at module level; that's the
    # reloaded one because importlib.reload(db) replaced the bound module.
    return migrate_from_current


def _run_all(migrator, src: Path) -> None:
    migrator._migrate_user_md(src)
    migrator._migrate_memory_md(src)
    migrator._migrate_self_md(src)
    migrator._migrate_episodes(src)
    migrator._migrate_mood(src)
    migrator._migrate_thoughts(src)
    migrator._migrate_heartbeat(src)


def test_migration_round_trip(synth_user, fresh_db):
    db = fresh_db
    mig = _migrator(db)
    _run_all(mig, synth_user)

    # USER.md → user_profile (no stage)
    profile = db.get_core_block("user_profile") or ""
    assert "alex" in profile
    assert "stage" not in profile.lower()

    # Known facts → facts table
    facts = db.active_facts(limit=100)
    objects = [f["object"] for f in facts]
    assert "works at a research lab" in objects
    assert "prefers cold rice" in objects
    assert "birthday in december" in objects

    # Open loops → tasks
    open_tasks = db.open_tasks()
    subjects = [t["subject"] for t in open_tasks]
    assert "ask about the cabbage" in subjects

    # MEMORY.md sections split
    assert db.get_core_block("about_user")
    assert db.get_core_block("shared_canon")
    assert "senior data scientist" in db.get_core_block("about_user")

    # SELF.md sections → core_blocks
    assert db.get_core_block("preoccupation")
    assert db.get_core_block("staged_disclosures")
    assert db.get_core_block("things_told_user")
    assert db.get_core_block("established_joke")
    assert "cabbage" in db.get_core_block("established_joke").lower()

    # Episodes
    eps = db.recent_episodes(limit=10)
    dates = {e["date"] for e in eps}
    assert "2026-05-17" in dates
    assert "2026-05-18" in dates

    # Mood
    assert "brightening" in (db.get_core_block("mood_arc") or "")

    # Thoughts (private)
    with db._conn() as c:
        thoughts = c.execute("SELECT thought FROM character_thoughts").fetchall()
    assert len(thoughts) == 2

    # Heartbeat → runtime_state
    assert db.runtime_get("last_user_message")
    assert db.runtime_get("photos_sent_today") == "1"


def test_migration_idempotent(synth_user, fresh_db):
    db = fresh_db
    mig = _migrator(db)
    _run_all(mig, synth_user)

    facts_n1 = len(db.active_facts(limit=1000))
    tasks_n1 = len(db.open_tasks())
    eps_n1 = len(db.recent_episodes(limit=1000))
    with db._conn() as c:
        thoughts_n1 = c.execute("SELECT COUNT(*) FROM character_thoughts").fetchone()[0]

    # Run again — should not duplicate
    _run_all(mig, synth_user)

    facts_n2 = len(db.active_facts(limit=1000))
    tasks_n2 = len(db.open_tasks())
    eps_n2 = len(db.recent_episodes(limit=1000))
    with db._conn() as c:
        thoughts_n2 = c.execute("SELECT COUNT(*) FROM character_thoughts").fetchone()[0]

    assert facts_n2 == facts_n1, f"facts duped: {facts_n1} -> {facts_n2}"
    assert tasks_n2 == tasks_n1, f"tasks duped: {tasks_n1} -> {tasks_n2}"
    assert eps_n2 == eps_n1, f"episodes duped: {eps_n1} -> {eps_n2}"
    assert thoughts_n2 == thoughts_n1, f"thoughts duped: {thoughts_n1} -> {thoughts_n2}"


def test_migration_fresh_truncates(synth_user, fresh_db):
    db = fresh_db
    mig = _migrator(db)

    # Pre-populate with garbage
    db.insert_fact("garbage", "is", "garbage", importance=1)
    db.create_task("garbage task")
    db.append_thought("a thought we want gone")
    assert len(db.active_facts(limit=10)) >= 1

    mig._truncate_target()

    assert len(db.active_facts(limit=10)) == 0
    assert len(db.open_tasks()) == 0
    with db._conn() as c:
        cnt = c.execute("SELECT COUNT(*) FROM character_thoughts").fetchone()[0]
    assert cnt == 0
