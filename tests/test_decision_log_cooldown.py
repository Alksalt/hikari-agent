"""Cooldown filtering for decisions_unresolved_due."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_unresolved_due_excludes_recently_asked():
    from storage import db
    did = db.decision_insert("past", 0.6, "2026-01-01")
    db.decision_mark_asked(did)
    rows = db.decisions_unresolved_due(cooldown_days=14)
    assert not any(r["id"] == did for r in rows)


def test_unresolved_due_includes_stale_asked():
    from storage import db
    did = db.decision_insert("past", 0.6, "2026-01-01")
    db.decision_mark_asked(did)
    with db._conn() as c:
        c.execute(
            "UPDATE decisions SET asked_at = datetime('now', '-30 days') WHERE id = ?",
            (did,),
        )
    rows = db.decisions_unresolved_due(cooldown_days=14)
    assert any(r["id"] == did for r in rows)


def test_unresolved_due_includes_never_asked():
    from storage import db
    did = db.decision_insert("past", 0.6, "2026-01-01")
    rows = db.decisions_unresolved_due(cooldown_days=14)
    assert any(r["id"] == did for r in rows)
