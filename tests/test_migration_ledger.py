"""Tests for the Sprint 7B migration ledger (storage.migrations + schema_migrations table)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_hikari.db"


@pytest.fixture()
def isolated_db(tmp_db_path: Path):
    """Yield a fresh DB opened via storage.db._ensure_schema(), then close it."""
    import storage.db as db

    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db._DB_PATH = tmp_db_path
        db._reset_schema_sentinel()
        conn = db._get_pooled_conn()
        yield conn, db
        conn.close()
        db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# Test 1 — fresh DB inserts all ledger rows, sets user_version = 1
# ---------------------------------------------------------------------------


def test_fresh_db_inserts_all_rows(tmp_db_path: Path):
    import storage.db as db

    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db._DB_PATH = tmp_db_path
        db._reset_schema_sentinel()
        conn = db._get_pooled_conn()
        try:
            row_count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
            db._reset_schema_sentinel()

    assert row_count == len(db.KNOWN_MIGRATIONS), (
        f"Expected {len(db.KNOWN_MIGRATIONS)} ledger rows, got {row_count}"
    )
    assert user_version == 1


# ---------------------------------------------------------------------------
# Test 2 — opening the same DB twice leaves row count unchanged
# ---------------------------------------------------------------------------


def test_repeated_open_is_noop(tmp_db_path: Path):
    import storage.db as db

    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db._DB_PATH = tmp_db_path

        # First open
        db._reset_schema_sentinel()
        conn = db._get_pooled_conn()
        count_after_first = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        conn.close()

        # Second open (same path — sentinel was NOT reset, simulating same process)
        db._reset_schema_sentinel()
        conn2 = db._get_pooled_conn()
        count_after_second = conn2.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        conn2.close()

        db._reset_schema_sentinel()

    assert count_after_first == count_after_second
    assert count_after_first == len(db.KNOWN_MIGRATIONS)


# ---------------------------------------------------------------------------
# Test 3 — checksum mismatch raises RuntimeError
# ---------------------------------------------------------------------------


def test_checksum_mismatch_raises(tmp_db_path: Path):
    import storage.db as db

    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db._DB_PATH = tmp_db_path
        db._reset_schema_sentinel()

        # First open populates the ledger correctly.
        conn = db._get_pooled_conn()
        conn.close()
        db._reset_schema_sentinel()

        # Tamper: overwrite a real migration's checksum with a DIFFERENT tag
        # value. Under the tag scheme, drift = the recorded tag no longer matches
        # the call's tag (a plain hex16 would instead be treated as a legacy
        # source-hash and silently migrated — see the upgrade-path test below).
        target_name = "migrate_facts_bitemporal"
        raw = sqlite3.connect(str(tmp_db_path))
        raw.execute(
            "UPDATE schema_migrations SET checksum = 'tag:tampered_body' WHERE name = ?",
            (target_name,),
        )
        raw.commit()
        raw.close()

        # Second open must raise RuntimeError for the tampered row.
        db._reset_schema_sentinel()
        with pytest.raises(RuntimeError, match=target_name):
            db._get_pooled_conn()

        db._reset_schema_sentinel()


def test_legacy_hex_checksum_migrates_to_tag(tmp_db_path: Path):
    """Upgrade path: a live DB whose ledger holds the pre-tag 16-hex source-hash
    for a migration must, on the first boot after tag= is added at the call site,
    rewrite that row to ``tag:<name>`` WITHOUT re-running the migration body and
    WITHOUT raising drift."""
    import storage.db as db

    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db._DB_PATH = tmp_db_path

        # First boot: records tag:<name> for every migration.
        db._reset_schema_sentinel()
        conn = db._get_pooled_conn()
        conn.close()
        db._reset_schema_sentinel()

        # Simulate a pre-FIX-6 ledger row: overwrite one migration's checksum
        # with a plausible legacy 16-hex source-hash.
        target_name = "migrate_facts_bitemporal"
        raw = sqlite3.connect(str(tmp_db_path))
        raw.execute(
            "UPDATE schema_migrations SET checksum = '1234567890abcdef' WHERE name = ?",
            (target_name,),
        )
        raw.commit()
        raw.close()

        # Next boot must NOT raise and must rewrite the row to the tag-checksum.
        db._reset_schema_sentinel()
        conn2 = db._get_pooled_conn()
        try:
            recorded = conn2.execute(
                "SELECT checksum FROM schema_migrations WHERE name = ?",
                (target_name,),
            ).fetchone()[0]
        finally:
            conn2.close()
            db._reset_schema_sentinel()

    assert recorded == f"tag:{target_name}", (
        f"legacy hex checksum should migrate to tag:<name>, got {recorded!r}"
    )


def test_fresh_db_records_tag_checksums(tmp_db_path: Path):
    """Fresh boot records ``tag:<name>`` (not a source-hash) for each migration,
    so later docstring/body edits no longer brick boot with checksum drift."""
    import storage.db as db

    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db._DB_PATH = tmp_db_path
        db._reset_schema_sentinel()
        conn = db._get_pooled_conn()
        try:
            rows = conn.execute(
                "SELECT name, checksum FROM schema_migrations"
            ).fetchall()
        finally:
            conn.close()
            db._reset_schema_sentinel()

    known = set(db.KNOWN_MIGRATIONS)
    for name, checksum in rows:
        if name in known:
            assert checksum == f"tag:{name}", (
                f"{name} recorded {checksum!r}, expected tag:{name}"
            )


# ---------------------------------------------------------------------------
# Test 4 — backfill path on a populated pre-7B database
# ---------------------------------------------------------------------------


def test_backfill_path_on_populated_db(tmp_db_path: Path):
    """Simulate a pre-7B DB: all migrations applied, ledger rows wiped, then
    user_version = 1. Reopening should backfill with sentinel and NOT re-run
    any migration body."""
    import storage.db as db

    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db._DB_PATH = tmp_db_path

        # First open: runs everything, populates ledger.
        db._reset_schema_sentinel()
        conn = db._get_pooled_conn()
        conn.close()
        db._reset_schema_sentinel()

        # Wipe the ledger to simulate pre-7B state.
        raw = sqlite3.connect(str(tmp_db_path))
        raw.execute("DELETE FROM schema_migrations")
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
        raw.close()

        # Second open: backfill_if_needed should insert all names with sentinel.
        db._reset_schema_sentinel()
        conn2 = db._get_pooled_conn()
        try:
            rows = conn2.execute(
                "SELECT name, checksum FROM schema_migrations ORDER BY name"
            ).fetchall()
        finally:
            conn2.close()
            db._reset_schema_sentinel()

    names = [r[0] for r in rows]
    checksums = [r[1] for r in rows]
    assert set(names) == set(db.KNOWN_MIGRATIONS)
    assert all(cs == "<unknown-backfilled>" for cs in checksums), (
        f"All checksums should be sentinel, got: {set(checksums)}"
    )


# ---------------------------------------------------------------------------
# Test 5 — SAVEPOINT rollback on migration failure
# ---------------------------------------------------------------------------


def test_savepoint_rollback_on_failure(tmp_db_path: Path):
    """A migration that does CREATE TABLE then raises must leave no ledger row
    and no table in the DB."""
    from storage.migrations import run_once

    raw = sqlite3.connect(str(tmp_db_path))
    raw.row_factory = sqlite3.Row
    # Minimal bootstrap: create schema_migrations table.
    raw.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT 'run' CHECK(source IN ('run','backfill')))"
    )
    raw.commit()

    def _bad_migration(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE _test_rollback_target (id INTEGER PRIMARY KEY)")
        raise ValueError("simulated migration failure")

    with pytest.raises(ValueError, match="simulated migration failure"):
        run_once(raw, "migrate_bad_one", _bad_migration)

    # Ledger row must not exist.
    row = raw.execute(
        "SELECT 1 FROM schema_migrations WHERE name = 'migrate_bad_one'"
    ).fetchone()
    assert row is None, "Ledger row must not be inserted after a failed migration"

    # Table must not exist.
    tbl = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_test_rollback_target'"
    ).fetchone()
    assert tbl is None, "Table created in a failed migration must be rolled back"

    raw.close()


# ---------------------------------------------------------------------------
# Test 6 — in-fn index on an ALTER-added column still works under run_once
# ---------------------------------------------------------------------------


def test_in_fn_index_on_altered_col_still_works(tmp_db_path: Path):
    """Verify that a migration following the schema-migration-ordering rule
    (ALTER TABLE, then CREATE INDEX on the new column, inside run_once) works
    correctly and the index is visible after the migration."""
    from storage.migrations import run_once

    raw = sqlite3.connect(str(tmp_db_path))
    raw.row_factory = sqlite3.Row
    raw.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT 'run' CHECK(source IN ('run','backfill')))"
    )
    raw.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, name TEXT)")
    raw.commit()

    def _add_col_and_index(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE example ADD COLUMN score INTEGER DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_example_score ON example(score)"
        )

    ran = run_once(raw, "migrate_example_score", _add_col_and_index)
    assert ran is True

    # Index exists.
    idx = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_example_score'"
    ).fetchone()
    assert idx is not None, "Index on ALTER-added column must be visible after run_once"

    # run_once again returns False (already recorded).
    ran2 = run_once(raw, "migrate_example_score", _add_col_and_index)
    assert ran2 is False

    raw.close()


# ---------------------------------------------------------------------------
# Test 7 — run_once rejects malicious / invalid migration names
# ---------------------------------------------------------------------------


def test_run_once_rejects_malicious_name(tmp_path):
    import sqlite3

    from storage.migrations import run_once

    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT 'run' CHECK(source IN ('run','backfill')))"
    )
    with pytest.raises(ValueError, match="must match"):
        run_once(conn, "x; DROP TABLE schema_migrations; --", lambda c: None)
    with pytest.raises(ValueError):
        run_once(conn, "X_uppercase", lambda c: None)  # uppercase rejected
    with pytest.raises(ValueError):
        run_once(conn, "1_starts_with_digit", lambda c: None)


# ---------------------------------------------------------------------------
# Test 8 — sentinel only honored on backfill source
# ---------------------------------------------------------------------------


def test_sentinel_only_honored_on_backfill_source(tmp_path):
    """A DB-write attacker pre-inserting source='run' + sentinel must NOT
    suppress the migration — run_once raises tampering."""
    import sqlite3

    from storage.migrations import _now_iso, run_once

    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT 'run' CHECK(source IN ('run','backfill')))"
    )
    # Forge a sentinel on a 'run' source row (attacker tampering)
    conn.execute(
        "INSERT INTO schema_migrations(name, applied_at, checksum, source) VALUES (?, ?, ?, 'run')",
        ("migrate_attacker", _now_iso(), "<unknown-backfilled>"),
    )
    with pytest.raises(RuntimeError, match="tampering"):
        run_once(conn, "migrate_attacker", lambda c: None)
