"""capability_offers table + helpers (Task 7) and offer engine (Task 8)."""
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


def test_offer_roundtrip_and_outcomes():
    rid = db.capability_offer_insert(offer_id="day_receipt", telegram_message_id=111)
    assert rid > 0
    assert db.capability_offer_recent_outcomes("day_receipt") == ["shown"]
    db.capability_offer_mark_tapped(rid)
    assert db.capability_offer_recent_outcomes("day_receipt") == ["tapped"]


def test_stale_shown_rows_marked_ignored():
    db.capability_offer_insert(offer_id="a", telegram_message_id=None)
    db.capability_offer_insert(offer_id="b", telegram_message_id=None)
    n = db.capability_offer_mark_stale_ignored()
    assert n == 2
    assert db.capability_offer_recent_outcomes("a") == ["ignored"]


def test_today_count_and_last_shown():
    assert db.capability_offers_today_count() == 0
    db.capability_offer_insert(offer_id="a", telegram_message_id=None)
    assert db.capability_offers_today_count() == 1
    assert db.capability_offer_last_shown("a") is not None
    assert db.capability_offer_last_shown("never") is None


def test_tool_calls_aggregations():
    db.tool_calls_insert(tool_id="mcp__x__one", duration_ms=5, success=True,
                         error_class=None, output_size=10)
    used = db.tool_calls_used_since("2000-01-01T00:00:00+00:00")
    assert "mcp__x__one" in used
    last = db.tool_calls_last_used(["mcp__x__one", "mcp__x__never"])
    assert "mcp__x__one" in last and "mcp__x__never" not in last
