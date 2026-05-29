"""Migration ledger for hikari-agent.

Provides two public functions:

- ``run_once(conn, name, fn)`` — idempotent migration wrapper.  Runs ``fn``
  exactly once, recording the call in ``schema_migrations``.  Returns True if
  ``fn`` ran this call, False if it was already recorded.  Raises RuntimeError
  if the recorded checksum differs from the current source (drift detection).

- ``backfill_if_needed(conn, known_migrations)`` — mass-inserts all known
  migration names with a sentinel checksum when the ledger is empty but the DB
  is already populated (pre-7B upgrade path).
"""
from __future__ import annotations

import hashlib
import inspect
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _checksum_for(fn: Callable, tag: str | None = None) -> str:
    if tag is not None:
        return f"tag:{tag}"
    return hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()[:16]


def run_once(
    conn: sqlite3.Connection,
    name: str,
    fn: Callable[[sqlite3.Connection], None],
    *,
    checksum: str | None = None,
    tag: str | None = None,
) -> bool:
    """Idempotent migration wrapper. Returns True if fn ran, False if skipped.

    Raises RuntimeError if a recorded checksum differs from current source,
    unless the recorded checksum is the backfill sentinel.

    When ``tag`` is provided the checksum stored in the ledger is ``"tag:<tag>"``
    rather than a sha256 of the function source.  This lets you edit the
    migration body (docstrings, comments, refactors) without triggering drift as
    long as you bump the tag.

    Backward-compat ledger-migrate: if a migration was previously recorded with
    a legacy 16-hex source-hash and is now called with a ``tag``, the ledger row
    is silently updated to the new tag-checksum and the migration is NOT re-run.
    This is a one-time transition; subsequent calls with the same tag are normal
    no-ops.
    """
    if not _SAFE_NAME.match(name):
        raise ValueError(
            f"run_once: migration name {name!r} must match [a-z][a-z0-9_]{{0,63}}"
        )
    BACKFILL_SENTINEL = "<unknown-backfilled>"
    actual = checksum or _checksum_for(fn, tag)

    row = conn.execute(
        "SELECT checksum, source FROM schema_migrations WHERE name = ?",
        (name,),
    ).fetchone()
    if row is not None:
        recorded, source = row[0], row[1]
        if recorded == BACKFILL_SENTINEL:
            if source != "backfill":
                raise RuntimeError(
                    f"schema_migrations: sentinel checksum on non-backfill row "
                    f"for {name!r} — DB tampering suspected"
                )
            return False
        if recorded == actual:
            return False
        # Backward-compat ledger-migrate: legacy 16-hex source-hash → tag-checksum.
        # When the recorded value is a plain hex16 (old source-hash) and the
        # caller is now supplying a tag, update the ledger without re-running fn.
        if tag is not None and _HEX16.match(recorded):
            conn.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE name = ?",
                (actual, name),
            )
            return False
        raise RuntimeError(
            f"schema_migrations checksum drift for {name!r}: "
            f"recorded={recorded} actual={actual}"
        )

    conn.execute(f"SAVEPOINT mig_{name}")
    try:
        fn(conn)
    except Exception:
        # fn raised — try to roll back to our savepoint.  If the fn already
        # committed (e.g. via conn.commit()), the savepoint is gone; in that
        # case we can't undo the migration body, but at least we don't insert
        # the ledger row so the next boot will retry.
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT mig_{name}")
            conn.execute(f"RELEASE SAVEPOINT mig_{name}")
        except Exception:
            pass
        raise
    # fn succeeded — insert the ledger row.  If fn called conn.commit()
    # internally, the savepoint is already gone; we insert the ledger row in a
    # fresh implicit transaction and then release (no-op if gone).
    conn.execute(
        "INSERT INTO schema_migrations(name, applied_at, checksum, source) VALUES (?, ?, ?, 'run')",
        (name, _now_iso(), actual),
    )
    try:
        conn.execute(f"RELEASE SAVEPOINT mig_{name}")
    except Exception:
        # Savepoint already released by an internal conn.commit() in fn.
        # The ledger INSERT above is already committed or will be committed by
        # the caller's transaction — either way we're consistent.
        pass
    return True


def backfill_if_needed(
    conn: sqlite3.Connection,
    known_migrations: list[str],
) -> int:
    """Backfill the ledger for pre-7B databases.

    If ``schema_migrations`` is empty AND ``user_version > 0``, mass-inserts
    all known migration names with the backfill sentinel so that subsequent
    ``run_once`` calls skip them.  Sets ``user_version = 1`` for fresh DBs.
    Returns the number of rows inserted.
    """
    BACKFILL_SENTINEL = "<unknown-backfilled>"
    existing = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    if existing > 0:
        return 0

    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if user_version == 0:
        conn.execute("PRAGMA user_version = 1")
        return 0

    ts = _now_iso()
    rows = [(name, ts, BACKFILL_SENTINEL) for name in known_migrations]
    conn.executemany(
        "INSERT INTO schema_migrations(name, applied_at, checksum, source) "
        "VALUES (?, ?, ?, 'backfill')",
        rows,
    )
    return len(rows)
