"""tests/test_memorydump.py — unit tests for agents/cockpit.py:format_memorydump.

Test matrix:
  1. Empty facts table → "no active facts" message + no keyboard rows
  2. ≤10 facts → all shown on page=0 with per-fact Forget/Context/Pin buttons
  3. >10 facts → page=0 returns first 10, page=1 returns next batch
  4. Pagination nav row: page=0 has Next >, page=1 has < Prev + Next >, last page no Next
  5. page beyond total → "no more facts" message
  6. Per-fact keyboard data uses correct mem: prefixes
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


# ---------------------------------------------------------------------------
# Fixtures
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


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _all_buttons(kb_rows):
    return [btn for row in kb_rows for btn in row]


# ---------------------------------------------------------------------------
# 1. Empty facts table → "no active facts" + no keyboard rows
# ---------------------------------------------------------------------------

def test_memorydump_empty_returns_no_facts_and_no_rows():
    import agents.cockpit as ck
    text, kb_rows = ck.format_memorydump(page=0)
    assert "no active facts" in text
    assert kb_rows == []


# ---------------------------------------------------------------------------
# 2. ≤10 facts → page 0 shows all, each has 3 buttons
# ---------------------------------------------------------------------------

def test_memorydump_few_facts_all_shown():
    import agents.cockpit as ck

    for i in range(5):
        db.insert_fact(subject=f"user", predicate=f"likes", object_=f"thing_{i}")

    text, kb_rows = ck.format_memorydump(page=0)
    assert "memory dump" in text

    buttons = _all_buttons(kb_rows)
    forget_btns = [b for b in buttons if b.get("text") == "Forget"]
    context_btns = [b for b in buttons if b.get("text") == "Context"]
    pin_btns = [b for b in buttons if b.get("text") == "Pin"]

    assert len(forget_btns) == 5
    assert len(context_btns) == 5
    assert len(pin_btns) == 5


# ---------------------------------------------------------------------------
# 3. >10 facts → first 10 on page=0, next batch on page=1
# ---------------------------------------------------------------------------

def test_memorydump_pagination_first_page():
    import agents.cockpit as ck

    ids = []
    for i in range(15):
        fid = db.insert_fact(subject=f"u", predicate=f"has", object_=f"fact_{i}")
        ids.append(fid)

    text0, kb0 = ck.format_memorydump(page=0)
    # 10 per-fact rows + 1 nav row = 11 rows
    per_fact_rows = [row for row in kb0 if any(b.get("text") == "Forget" for b in row)]
    assert len(per_fact_rows) == 10


def test_memorydump_pagination_second_page():
    import agents.cockpit as ck

    for i in range(15):
        db.insert_fact(subject=f"u", predicate=f"has", object_=f"fact_{i}")

    text1, kb1 = ck.format_memorydump(page=1)
    per_fact_rows = [row for row in kb1 if any(b.get("text") == "Forget" for b in row)]
    assert len(per_fact_rows) == 5  # remaining 5


# ---------------------------------------------------------------------------
# 4. Navigation buttons: Next on page 0, Prev on page 1, no Next on last page
# ---------------------------------------------------------------------------

def test_memorydump_nav_next_on_page0():
    import agents.cockpit as ck

    for i in range(12):
        db.insert_fact(subject="u", predicate="has", object_=f"x{i}")

    _, kb = ck.format_memorydump(page=0)
    buttons = _all_buttons(kb)
    assert any("Next" in b.get("text", "") for b in buttons), "page 0 should have Next >"


def test_memorydump_nav_prev_on_page1():
    import agents.cockpit as ck

    for i in range(12):
        db.insert_fact(subject="u", predicate="has", object_=f"x{i}")

    _, kb = ck.format_memorydump(page=1)
    buttons = _all_buttons(kb)
    assert any("Prev" in b.get("text", "") for b in buttons), "page 1 should have < Prev"


def test_memorydump_no_next_on_last_page():
    import agents.cockpit as ck

    for i in range(12):
        db.insert_fact(subject="u", predicate="has", object_=f"x{i}")

    # Last page is page 1 (10 + 2)
    _, kb = ck.format_memorydump(page=1)
    buttons = _all_buttons(kb)
    assert not any("Next" in b.get("text", "") for b in buttons), "last page should not have Next >"


# ---------------------------------------------------------------------------
# 5. page beyond total → "no more facts" message, empty rows
# ---------------------------------------------------------------------------

def test_memorydump_page_beyond_total():
    import agents.cockpit as ck

    db.insert_fact(subject="u", predicate="has", object_="single")

    text, kb = ck.format_memorydump(page=5)
    assert "no more facts" in text
    assert kb == []


# ---------------------------------------------------------------------------
# 6. Per-fact keyboard callback_data uses correct mem: prefixes
# ---------------------------------------------------------------------------

def test_memorydump_callback_data_prefixes():
    import agents.cockpit as ck

    fid = db.insert_fact(subject="alice", predicate="knows", object_="python")
    _, kb = ck.format_memorydump(page=0)

    buttons = _all_buttons(kb)
    forget_btn = next((b for b in buttons if b.get("text") == "Forget"), None)
    context_btn = next((b for b in buttons if b.get("text") == "Context"), None)
    pin_btn = next((b for b in buttons if b.get("text") == "Pin"), None)

    assert forget_btn is not None and f"mem:forget:{fid}" == forget_btn["callback_data"]
    assert context_btn is not None and f"mem:context:{fid}" == context_btn["callback_data"]
    assert pin_btn is not None and f"mem:pin:{fid}" == pin_btn["callback_data"]


# ---------------------------------------------------------------------------
# 7. Text is capped at 3900 chars
# ---------------------------------------------------------------------------

def test_memorydump_text_length_capped():
    import agents.cockpit as ck

    # Insert facts with long object values to stress the truncation
    for i in range(10):
        db.insert_fact(
            subject="user",
            predicate="has_very_long_predicate_value",
            object_="x" * 100,
        )

    text, _ = ck.format_memorydump(page=0)
    assert len(text) <= 3900
