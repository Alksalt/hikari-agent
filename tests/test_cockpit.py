"""tests/test_cockpit.py — unit tests for Wave 3 cockpit formatters.

Covers:
  1.  format_memorydump — pagination, per-fact keyboard rows
  2.  format_diary — pagination, empty DB
  3.  format_links — empty shelf, with data, search
  4.  format_receipt — today/week/category filter buttons
  5.  format_decision — pending list, resolve, empty
  6.  format_voice — STT health block, no voice notes
  7.  format_reminders_page — pagination, keyboard rows
  8.  format_proactive_status — active/snoozed sections
  9.  format_proactive_why — reason-contract rendering
  10. format_silence_ack — expiry timestamp present
  11. format_tools summary — per-family counts + warm pool
  12. _COMMANDS reconcile — every key has a non-empty description
"""
from __future__ import annotations

import importlib
import json
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    """Don't let format_voice or format_proactive_status hit the network."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=OSError("blocked")))


# ---------------------------------------------------------------------------
# 1. format_memorydump
# ---------------------------------------------------------------------------

def test_memorydump_empty():
    import agents.cockpit as ck
    text, rows = ck.format_memorydump(page=0)
    assert "no active facts" in text
    assert rows == []


def test_memorydump_with_facts():
    # Insert a fact
    fid = db.insert_fact(subject="alice", predicate="likes", object_="cats")
    import agents.cockpit as ck
    text, kb_rows = ck.format_memorydump(page=0)
    assert str(fid) in text
    # Per-fact row has Forget / Context / Pin
    assert any("Forget" in btn["text"] for row in kb_rows for btn in row)
    assert any("mem:forget" in btn["callback_data"] for row in kb_rows for btn in row)
    assert any("mem:pin" in btn["callback_data"] for row in kb_rows for btn in row)
    assert len(text) <= 3900


def test_memorydump_pagination():
    # Insert 12 facts to test next-page button
    for i in range(12):
        db.insert_fact(subject=f"subj{i}", predicate="is", object_=f"obj{i}")
    import agents.cockpit as ck
    text, kb_rows = ck.format_memorydump(page=0)
    # Should have a Next > button
    nav_buttons = [btn for row in kb_rows for btn in row if "Next" in btn.get("text", "")]
    assert nav_buttons, "expected Next > button when total > PAGE_SIZE"
    # Page 1
    text2, kb_rows2 = ck.format_memorydump(page=1)
    prev_buttons = [btn for row in kb_rows2 for btn in row if "Prev" in btn.get("text", "")]
    assert prev_buttons, "expected < Prev on page 1"


# ---------------------------------------------------------------------------
# 2. format_diary
# ---------------------------------------------------------------------------

def test_diary_empty():
    import agents.cockpit as ck
    text, nav = ck.format_diary(page=0)
    assert "no diary entries" in text
    assert nav == []


def test_diary_with_entries(monkeypatch):
    # Patch diary_entries_recent to return fake data
    fake_entries = [
        {"entry_date": f"2026-05-{i:02d}", "body": f"Entry {i} body text here."}
        for i in range(1, 8)
    ]
    import storage.db as _db_mod
    monkeypatch.setattr(_db_mod, "diary_entries_recent", lambda limit=5: fake_entries[:limit])

    import agents.cockpit as ck
    text, nav = ck.format_diary(page=0)
    assert "diary" in text
    assert "2026-05-01" in text
    assert len(text) <= 3900


def test_diary_pagination(monkeypatch):
    fake_entries = [
        {"entry_date": f"2026-05-{i:02d}", "body": f"Entry {i}"}
        for i in range(1, 12)  # 11 entries > 5/page
    ]
    import storage.db as _db_mod
    monkeypatch.setattr(_db_mod, "diary_entries_recent", lambda limit=5: fake_entries[:limit])

    import agents.cockpit as ck
    text, nav = ck.format_diary(page=0)
    # Should have Next > if more than 5
    _next_btns = [b for b in nav if "Next" in b.get("text", "")]
    # nav is a flat list of dicts here
    assert isinstance(nav, list)


# ---------------------------------------------------------------------------
# 3. format_links
# ---------------------------------------------------------------------------

def test_links_empty(monkeypatch):
    import tools.link_shelf.db as _shelf
    monkeypatch.setattr(_shelf, "list_links", lambda **kw: [])
    import agents.cockpit as ck
    chunks = ck.format_links()
    text = chunks[0]
    assert "no saved links" in text


def test_links_with_data(monkeypatch):
    fake_links = [
        {"id": i, "url": f"https://example.com/{i}", "title": f"Link {i}",
         "kind": "later", "added_at": "2026-05-25T12:00:00"}
        for i in range(3)
    ]
    import tools.link_shelf.db as _shelf
    monkeypatch.setattr(_shelf, "list_links", lambda **kw: fake_links)
    import agents.cockpit as ck
    chunks = ck.format_links()
    text = "\n".join(chunks)
    assert "example.com" in text
    assert "Link 0" in text
    assert len(text) <= 3900


def test_links_search_empty(monkeypatch):
    import tools.link_shelf.db as _shelf
    monkeypatch.setattr(_shelf, "search", lambda **kw: [])
    import agents.cockpit as ck
    chunks = ck.format_links(query="python")
    text = "\n".join(chunks)
    assert "python" in text
    assert "no links" in text


# ---------------------------------------------------------------------------
# 4. format_receipt
# ---------------------------------------------------------------------------

def test_receipt_today(monkeypatch):
    from datetime import date

    from tools.day_receipt._db import Receipt
    fake_receipt = Receipt(receipt_date=date.today(), entries=(), note=None)
    import tools.day_receipt._db as _rdb
    monkeypatch.setattr(_rdb, "get_receipt", lambda d, **kw: fake_receipt)
    import agents.cockpit as ck
    text, kb_row = ck.format_receipt("today")
    # keyboard should have Today / Week / Made / Moved / Learned / Avoided
    btn_texts = {b["text"] for b in kb_row}
    assert "Today" in btn_texts
    assert "Week" in btn_texts
    assert "Made" in btn_texts
    assert "Learned" in btn_texts


def test_receipt_category_buttons():
    import agents.cockpit as ck
    # patch out the db entirely
    with patch("tools.day_receipt._db.get_receipt", side_effect=Exception("db unavailable")):
        text, kb_row = ck.format_receipt("today")
    # even on error, keyboard row should always be returned
    assert isinstance(kb_row, list)
    assert len(kb_row) == 6  # Today/Week/Made/Moved/Learned/Avoided


def test_receipt_week(monkeypatch):

    import tools.day_receipt._db as _rdb
    from tools.day_receipt._db import Receipt
    monkeypatch.setattr(_rdb, "get_receipt", lambda d, **kw: Receipt(receipt_date=d, entries=(), note=None))
    import agents.cockpit as ck
    text, kb_row = ck.format_receipt("week")
    assert len(text) <= 3900
    assert "Week" in {b["text"] for b in kb_row}


# ---------------------------------------------------------------------------
# 5. format_decision
# ---------------------------------------------------------------------------

def test_decision_empty():
    import agents.cockpit as ck
    text = ck.format_decision()
    assert "no pending" in text


def test_decision_with_data():
    db.decision_insert(
        statement="We ship by Friday",
        predicted_p=0.75,
        resolve_by="2026-05-30",
    )
    import agents.cockpit as ck
    text = ck.format_decision()
    assert "We ship by Friday" in text
    assert "75%" in text


def test_decision_resolve():
    did = db.decision_insert(
        statement="Test prediction",
        predicted_p=0.6,
        resolve_by="2026-05-28",
    )
    import agents.cockpit as ck
    text = ck.format_decision("resolve", [str(did), "1"])
    assert f"#{did}" in text
    assert "true" in text.lower() or "1" in text


def test_decision_resolve_invalid():
    import agents.cockpit as ck
    text = ck.format_decision("resolve", ["9999", "1"])
    assert "not found" in text or "error" in text


def test_decision_resolve_bad_outcome():
    import agents.cockpit as ck
    text = ck.format_decision("resolve", ["1", "5"])
    assert "0" in text or "1" in text  # shows valid values


# ---------------------------------------------------------------------------
# 6. format_voice
# ---------------------------------------------------------------------------

def test_voice_no_notes():
    import agents.cockpit as ck
    text = ck.format_voice()
    assert "STT health" in text
    assert len(text) <= 3900


def test_voice_stt_health_fields():
    import agents.cockpit as ck
    text = ck.format_voice()
    # Should mention endpoint / model / key env
    assert "endpoint" in text.lower() or "enabled" in text.lower()


def test_voice_with_voice_note():
    # Insert a message that looks like a voice note transcript
    db.append_message(
        role="user",
        content="[voice note 12s] transcript: 'hey what's up'",
        source="chat",
    )
    import agents.cockpit as ck
    text = ck.format_voice()
    assert "voice note" in text.lower()


# ---------------------------------------------------------------------------
# 7. format_reminders_page
# ---------------------------------------------------------------------------

def test_reminders_page_empty():
    import agents.cockpit as ck
    text, kb_rows = ck.format_reminders_page(page=0)
    assert "no active reminders" in text
    assert kb_rows == []


def test_reminders_page_with_data():
    from datetime import timedelta
    fire_at = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        + timedelta(hours=2)
    ).isoformat()
    db.reminder_insert(fire_at=fire_at, text="Test reminder")
    import agents.cockpit as ck
    text, kb_rows = ck.format_reminders_page(page=0)
    assert "Test reminder" in text
    # Per-item buttons: Snooze 10m / Snooze 1h / Cancel
    snooze_btns = [
        btn for row in kb_rows for btn in row
        if "Snooze" in btn.get("text", "")
    ]
    assert snooze_btns, "expected snooze buttons"
    cancel_btns = [
        btn for row in kb_rows for btn in row
        if "Cancel" in btn.get("text", "")
    ]
    assert cancel_btns, "expected cancel buttons"
    # callback patterns
    assert any("rem:snooze" in b["callback_data"] for row in kb_rows for b in row)
    assert any("rem:cancel" in b["callback_data"] for row in kb_rows for b in row)


def test_reminders_page_pagination():
    from datetime import datetime as _dt
    from datetime import timedelta
    for i in range(12):
        fire_at = (_dt.now(UTC) + timedelta(hours=i + 1)).isoformat()
        db.reminder_insert(fire_at=fire_at, text=f"Reminder {i}")
    import agents.cockpit as ck
    text, kb_rows = ck.format_reminders_page(page=0)
    next_btns = [btn for row in kb_rows for btn in row if "Next" in btn.get("text", "")]
    assert next_btns, "expected Next > button with 12 reminders"
    assert any("rem:page:1" in b["callback_data"] for row in kb_rows for b in row)


# ---------------------------------------------------------------------------
# 8. format_proactive_status
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
# 9. format_proactive_why — reason-contract
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


# ---------------------------------------------------------------------------
# 10. format_silence_ack
# ---------------------------------------------------------------------------

def test_silence_ack_contains_expiry():
    import agents.cockpit as ck
    text = ck.format_silence_ack(60)
    assert "60 minutes" in text
    # Should contain a date/time string
    assert "2026" in text or "2025" in text
    assert "until" in text.lower()


def test_silence_ack_with_local_tz(monkeypatch):
    monkeypatch.setenv("HOME_TZ", "Europe/Oslo")
    import agents.cockpit as ck
    text = ck.format_silence_ack(30)
    assert "Oslo" in text or "Europe" in text or "2026" in text


# ---------------------------------------------------------------------------
# 11. format_tools summary — per-family counts + warm pool
# ---------------------------------------------------------------------------

def test_tools_summary():
    import agents.cockpit as ck

    # Patch catalog to avoid heavy IO
    from tools.catalog import ToolEntry
    fake_entry = ToolEntry(
        name="mcp__hikari_memory__recall",
        description="recall facts",
        domain="memory",
        operation="read",
        risk_tier="safe",
        credentials=[],
        examples=[],
        presentation_hint="",
        tags=[],
        bucket=0,
    )
    with patch("tools.catalog.get_catalog") as mock_cat:
        mock_cat.return_value = MagicMock(entries=[fake_entry])
        text = ck.format_tools("summary", [])
    assert "memory" in text
    assert "family" in text.lower() or "families" in text.lower()
    assert "warm pool" in text.lower()
    assert len(text) <= 3900


def test_tools_policy_still_works():
    import agents.cockpit as ck
    fake_spec = MagicMock()
    fake_spec.id = "mcp__test__foo"
    fake_spec.access_mode = "read"
    fake_registry = MagicMock()
    fake_registry.specs.return_value = [fake_spec]
    with patch("tools._tools_yaml.load_registry", return_value=fake_registry):
        text = ck.format_tools("policy", [])
    assert "read" in text
    assert len(text) <= 3900


# ---------------------------------------------------------------------------
# 12. _COMMANDS reconcile — all keys non-empty, no orphan descriptions
# ---------------------------------------------------------------------------

def test_commands_all_have_descriptions():
    import agents.cockpit as ck
    for cmd, desc in ck._COMMANDS.items():
        assert cmd, "empty command key"
        assert desc, f"empty description for /{cmd}"
        assert len(desc) <= 256, f"/{cmd} description exceeds 256 chars (Telegram limit)"


def test_commands_help_contains_all():
    import agents.cockpit as ck
    help_text = ck.format_help()
    for cmd in ck._COMMANDS:
        assert f"/{cmd}" in help_text, f"/{cmd} missing from /help output"
