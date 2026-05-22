"""Tests for the proactive_events table and db.proactive_event_insert()."""
from __future__ import annotations

import importlib
import json


def _fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    db._reset_schema_sentinel()
    return db


def test_proactive_event_insert_writes_row(tmp_path, monkeypatch):
    db = _fresh_db(monkeypatch, tmp_path)
    row_id = db.proactive_event_insert(
        source="wiki_new_file",
        pattern="question",
        payload_json=json.dumps({"filename": "test.md"}),
    )
    assert row_id > 0

    with db._conn() as c:
        rows = c.execute("SELECT * FROM proactive_events WHERE id = ?", (row_id,)).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["source"] == "wiki_new_file"
    assert row["pattern"] == "question"
    assert "test.md" in row["payload_json"]
    assert row["sent_at"]
    assert row["telegram_message_id"] is None


def test_proactive_event_indexes_created(tmp_path, monkeypatch):
    db = _fresh_db(monkeypatch, tmp_path)
    # Trigger schema init by accessing the DB
    _ = db.proactive_event_insert(
        source="test",
        pattern="notify",
        payload_json="{}",
    )

    with db._conn() as c:
        index_names = {
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='proactive_events'"
            ).fetchall()
        }
    assert "idx_proactive_events_sent_at" in index_names
    assert "idx_proactive_events_source_sent" in index_names
