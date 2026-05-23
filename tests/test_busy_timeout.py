"""Verify that PRAGMA busy_timeout is applied on fresh connections.

The pool sets busy_timeout from config key sqlite.busy_timeout_ms (default 5000).
A fresh connection must report the configured value when queried.
"""
from __future__ import annotations

import importlib
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
    yield


def test_busy_timeout_applied_default():
    """Default busy_timeout_ms=5000 must be set on a fresh connection."""
    conn = db._get_pooled_conn()
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    # SQLite returns the timeout in milliseconds.
    assert row[0] == 5000


def test_busy_timeout_from_config(monkeypatch, tmp_path: Path):
    """Config override respected: set sqlite.busy_timeout_ms=2000."""
    monkeypatch.setattr(
        db, "_cfg_get", lambda key, default: 2000 if key == "sqlite.busy_timeout_ms" else default
    )
    db._reset_schema_sentinel()
    new_path = tmp_path / "custom.db"
    monkeypatch.setattr(db, "_DB_PATH", new_path)
    conn = db._get_pooled_conn()
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 2000
