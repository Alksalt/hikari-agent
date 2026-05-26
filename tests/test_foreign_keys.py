"""Verify PRAGMA foreign_keys = ON is honored for tables with ON DELETE CASCADE.

SQLite does NOT enforce foreign-key constraints by default — each connection must
execute ``PRAGMA foreign_keys = ON``.  These tests confirm that when the PRAGMA
is enabled, ON DELETE CASCADE propagates correctly for the three pairs in the
schema that declare it:

  1. work_packets → work_packet_steps  (ON DELETE CASCADE on packet_id)
  2. facts → fact_entities             (ON DELETE CASCADE on fact_id)
  3. entities → entity_aliases         (ON DELETE CASCADE on entity_id)

They also confirm the gap: without the PRAGMA the deletes do NOT cascade, which
documents that production code is responsible for enabling the PRAGMA (or for
cleaning up child rows manually).
"""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    # Bootstrap schema — triggers _ensure_schema which runs all migrations.
    db.upsert_core_block("_boot", "_boot")
    yield
    db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enable_fk(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")


def _make_work_packet() -> int:
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO work_packets (user_turn_id, status, created_at) "
            "VALUES ('turn-fk-test', 'running', unixepoch())"
        )
        return cur.lastrowid


def _add_step(packet_id: int, idx: int) -> int:
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO work_packet_steps "
            "(packet_id, step_index, tool_name, status, created_at) "
            "VALUES (?, ?, 'tool', 'pending', unixepoch())",
            (packet_id, idx),
        )
        return cur.lastrowid


_entity_counter = 0


def _make_entity(name: str | None = None) -> int:
    global _entity_counter
    _entity_counter += 1
    canonical = name or f"test-person-{_entity_counter}"
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO entities (kind, canonical_name, created_at, last_seen_at) "
            "VALUES ('person', ?, unixepoch(), unixepoch())",
            (canonical,),
        )
        return cur.lastrowid


def _add_alias(entity_id: int, alias: str) -> None:
    with db._conn() as c:
        c.execute(
            "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (entity_id, alias),
        )


def _make_fact() -> int:
    return db.insert_fact("subj", "pred", "obj")


def _link_fact_entity(fact_id: int, entity_id: int) -> None:
    with db._conn() as c:
        c.execute(
            "INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
            (fact_id, entity_id),
        )


# ---------------------------------------------------------------------------
# 1. work_packet_steps cascade-deletes when parent work_packet is deleted
# ---------------------------------------------------------------------------

class TestWorkPacketsCascade:
    def test_cascade_with_fk_enabled(self):
        """Deleting a work_packet removes all its steps when FK enforcement is on."""
        pid = _make_work_packet()
        _add_step(pid, 0)
        _add_step(pid, 1)
        _add_step(pid, 2)

        with db._conn() as c:
            _enable_fk(c)
            c.execute("DELETE FROM work_packets WHERE id = ?", (pid,))

        with db._conn() as c:
            remaining = c.execute(
                "SELECT COUNT(*) AS n FROM work_packet_steps WHERE packet_id = ?",
                (pid,),
            ).fetchone()["n"]
        assert remaining == 0, "steps should have cascade-deleted"

    def test_no_cascade_without_fk_pragma(self):
        """Without PRAGMA foreign_keys = ON, child rows are orphaned after parent delete."""
        pid = _make_work_packet()
        _add_step(pid, 0)
        _add_step(pid, 1)

        with db._conn() as c:
            # FK enforcement is OFF by default — no _enable_fk call.
            c.execute("DELETE FROM work_packets WHERE id = ?", (pid,))

        with db._conn() as c:
            orphans = c.execute(
                "SELECT COUNT(*) AS n FROM work_packet_steps WHERE packet_id = ?",
                (pid,),
            ).fetchone()["n"]
        # Gap: orphan rows remain because the PRAGMA was not set.
        assert orphans == 2, "without PRAGMA fk=ON, orphan steps survive"

    def test_cascade_does_not_affect_other_packets(self):
        """Cascade is scoped — deleting one packet only removes its own steps."""
        pid_a = _make_work_packet()
        pid_b = _make_work_packet()
        _add_step(pid_a, 0)
        _add_step(pid_b, 0)
        _add_step(pid_b, 1)

        with db._conn() as c:
            _enable_fk(c)
            c.execute("DELETE FROM work_packets WHERE id = ?", (pid_a,))

        with db._conn() as c:
            remaining_b = c.execute(
                "SELECT COUNT(*) AS n FROM work_packet_steps WHERE packet_id = ?",
                (pid_b,),
            ).fetchone()["n"]
        assert remaining_b == 2


# ---------------------------------------------------------------------------
# 2. fact_entities cascade-deletes when parent fact is deleted
# ---------------------------------------------------------------------------

class TestFactEntitiesCascade:
    def test_cascade_with_fk_enabled(self):
        """Deleting a fact removes its fact_entities rows when FK enforcement is on."""
        fid = _make_fact()
        eid = _make_entity()
        _link_fact_entity(fid, eid)

        with db._conn() as c:
            _enable_fk(c)
            c.execute("DELETE FROM facts WHERE id = ?", (fid,))

        with db._conn() as c:
            remaining = c.execute(
                "SELECT COUNT(*) AS n FROM fact_entities WHERE fact_id = ?",
                (fid,),
            ).fetchone()["n"]
        assert remaining == 0

    def test_entity_row_survives_fact_deletion(self):
        """The entity itself is not deleted when a linked fact is deleted."""
        fid = _make_fact()
        eid = _make_entity()
        _link_fact_entity(fid, eid)

        with db._conn() as c:
            _enable_fk(c)
            c.execute("DELETE FROM facts WHERE id = ?", (fid,))

        with db._conn() as c:
            entity_still_there = c.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE id = ?", (eid,)
            ).fetchone()["n"]
        assert entity_still_there == 1

    def test_no_cascade_without_fk_pragma(self):
        """Without PRAGMA foreign_keys = ON, fact_entities rows are orphaned."""
        fid = _make_fact()
        eid = _make_entity()
        _link_fact_entity(fid, eid)

        with db._conn() as c:
            c.execute("DELETE FROM facts WHERE id = ?", (fid,))

        with db._conn() as c:
            orphans = c.execute(
                "SELECT COUNT(*) AS n FROM fact_entities WHERE fact_id = ?",
                (fid,),
            ).fetchone()["n"]
        assert orphans == 1, "without PRAGMA fk=ON, fact_entities orphan row survives"


# ---------------------------------------------------------------------------
# 3. entity_aliases cascade-deletes when parent entity is deleted
# ---------------------------------------------------------------------------

class TestEntityAliasesCascade:
    def test_cascade_with_fk_enabled(self):
        """Deleting an entity removes its aliases when FK enforcement is on."""
        eid = _make_entity()
        _add_alias(eid, "alias-one")
        _add_alias(eid, "alias-two")

        with db._conn() as c:
            _enable_fk(c)
            c.execute("DELETE FROM entities WHERE id = ?", (eid,))

        with db._conn() as c:
            remaining = c.execute(
                "SELECT COUNT(*) AS n FROM entity_aliases WHERE entity_id = ?",
                (eid,),
            ).fetchone()["n"]
        assert remaining == 0

    def test_no_cascade_without_fk_pragma(self):
        """Without PRAGMA foreign_keys = ON, aliases are orphaned after entity delete."""
        eid = _make_entity()
        _add_alias(eid, "alias-orphan")

        with db._conn() as c:
            c.execute("DELETE FROM entities WHERE id = ?", (eid,))

        with db._conn() as c:
            orphans = c.execute(
                "SELECT COUNT(*) AS n FROM entity_aliases WHERE entity_id = ?",
                (eid,),
            ).fetchone()["n"]
        assert orphans == 1

    def test_cascade_scoped_to_deleted_entity(self):
        """Cascade only removes aliases of the deleted entity, not others."""
        eid_a = _make_entity()
        eid_b = _make_entity()
        _add_alias(eid_a, "a-alias")
        _add_alias(eid_b, "b-alias-1")
        _add_alias(eid_b, "b-alias-2")

        with db._conn() as c:
            _enable_fk(c)
            c.execute("DELETE FROM entities WHERE id = ?", (eid_a,))

        with db._conn() as c:
            remaining_b = c.execute(
                "SELECT COUNT(*) AS n FROM entity_aliases WHERE entity_id = ?",
                (eid_b,),
            ).fetchone()["n"]
        assert remaining_b == 2
