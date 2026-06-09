"""tests/test_cockpit.py — unit tests for the surviving cockpit formatters.

Phase 5b (useful-agent pivot) removed the slash-command surface; cockpit
keeps only the formatters consumed by inline-keyboard callbacks and the
set_proactive_source tool.

Covers:
  1. format_proactive_status — active/snoozed sections
  2. format_proactive_why — reason-contract rendering
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from storage import db

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Don't let any formatter hit the network."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=OSError("blocked")))


# ---------------------------------------------------------------------------
# 1. format_proactive_status
# ---------------------------------------------------------------------------

def test_proactive_status_no_snooze(monkeypatch):
    # Patch out producers import
    monkeypatch.setattr(
        "agents.engagement.producers",
        type("M", (), {"ALL_PRODUCER_IDS": ["wiki"], "DEFAULT_ENABLED_SOURCES": ["wiki"]})(),
        raising=False,
    )
    import agents.cockpit as ck
    text = ck.format_proactive_status()
    assert "next ping window" in text
    assert "active sources" in text
    assert "snoozed sources" in text
    assert len(text) <= 3900


def test_proactive_status_with_snooze():
    from datetime import timedelta
    future = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        + timedelta(hours=3)
    ).isoformat()
    db.runtime_set("proactive_snooze_until", json.dumps({"wiki": future}))
    import agents.cockpit as ck
    text = ck.format_proactive_status()
    assert "wiki" in text
    # snoozed section should mention expiry
    assert "expire" in text.lower() or "h" in text


# ---------------------------------------------------------------------------
# 2. format_proactive_why — reason-contract
# ---------------------------------------------------------------------------

def test_proactive_why_not_found():
    import agents.cockpit as ck
    text = ck.format_proactive_why(9999)
    assert "not found" in text


def test_proactive_why_renders_fields():
    # Insert a proactive event via the DB helper
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO proactive_events (sent_at, source, pattern, payload_json, status) "
            "VALUES (datetime('now'), 'wiki_new_file', 'notify', '{}', 'sent')"
        )
        eid = cur.lastrowid
    import agents.cockpit as ck
    text = ck.format_proactive_why(eid)
    assert f"#{eid}" in text
    assert "wiki_new_file" in text
    assert "source" in text.lower()
    assert len(text) <= 3900
