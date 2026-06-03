"""Tests for the kuzu corruption-quarantine guard in storage.graph (2026-06-03).

The guard must fire ONLY on a clear on-disk corruption signature — never on the
transient same-process lock conflict that the teardown path already recovers from
— and must move the corrupt DB (and its WAL) aside so the next open rebuilds empty.
"""
from __future__ import annotations

from pathlib import Path


def test_looks_like_corruption_true_signatures():
    from storage import graph
    for msg in (
        "Storage corruption detected",
        "checksum mismatch in page 4",
        "malformed database file",
        "IO error while reading",
        "bad magic number",
        "file is not a valid database",
    ):
        assert graph._looks_like_corruption(Exception(msg)) is True, msg


def test_looks_like_corruption_excludes_transient_lock():
    from storage import graph
    # The known same-process lock conflict must NOT be treated as corruption.
    assert graph._looks_like_corruption(
        Exception("Database path cannot be a directory")
    ) is False
    assert graph._looks_like_corruption(Exception("could not acquire lock")) is False
    assert graph._looks_like_corruption(Exception("some unrelated error")) is False


def test_quarantine_moves_db_and_wal(tmp_path: Path):
    from storage import graph
    db = tmp_path / "hikari.kuzu"
    db.write_text("corrupt-bytes")
    wal = tmp_path / "hikari.kuzu.wal"
    wal.write_text("wal-bytes")

    dest = graph._quarantine_corrupt_graph(db)

    assert dest is not None
    assert not db.exists(), "corrupt db should have been renamed away"
    assert dest.exists() and dest.name.startswith("hikari.kuzu.corrupt.")
    # WAL moved alongside.
    assert not wal.exists()
    assert (tmp_path / (dest.name + ".wal")).exists()


def test_quarantine_missing_file_is_noop(tmp_path: Path):
    from storage import graph
    assert graph._quarantine_corrupt_graph(tmp_path / "nope.kuzu") is None
