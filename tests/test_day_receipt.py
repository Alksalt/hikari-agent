"""day_receipt — in-process port of the standalone MCP server.

Covers the eight tool handlers (add / today / get / print / week /
search / set_note / delete) end-to-end through their ``@tool``
wrappers, kind validation, schema-bootstrap idempotency, and DB
isolation via ``DAY_RECEIPT_DB``.

Each test gets a fresh SQLite file under ``tmp_path`` via the env
override and resets the per-process ``_SCHEMA_INITIALIZED`` sentinel so
the bootstrap actually runs against the new DB.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_receipt_db(tmp_path: Path, monkeypatch):
    """Each test gets its own SQLite file under tmp_path.

    The day_receipt feature has a process-level ``_SCHEMA_INITIALIZED``
    sentinel — without resetting it, the second test in a run would
    reuse the stale "already initialized" flag against the new DB and
    skip the bootstrap, leaving the new file empty.
    """
    db_file = tmp_path / "test_receipt.db"
    monkeypatch.setenv("DAY_RECEIPT_DB", str(db_file))
    from tools.day_receipt import _db as receipt_db
    receipt_db._reset_schema_sentinel()
    yield db_file
    receipt_db._reset_schema_sentinel()


# ---------- add ----------


@pytest.mark.asyncio
async def test_add_happy_path(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add

    r = await receipt_add.handler({
        "category": "made",
        "text": "shipped the prototype",
        "tags": ["work", "ship"],
    })
    assert "logged" in r["content"][0]["text"]
    assert r["data"]["ok"] is True
    assert r["data"]["category"] == "made"
    assert r["data"]["date"] == date.today().isoformat()
    assert isinstance(r["data"]["id"], int) and r["data"]["id"] > 0


@pytest.mark.asyncio
async def test_add_rejects_invalid_category(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add

    r = await receipt_add.handler({"category": "schemed", "text": "doesn't fit"})
    assert "refused" in r["content"][0]["text"].lower()
    assert "made" in r["content"][0]["text"]
    assert "avoided" in r["content"][0]["text"]


@pytest.mark.asyncio
async def test_add_accepts_all_four_categories(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add

    for cat in ("made", "moved", "learned", "avoided"):
        r = await receipt_add.handler({"category": cat, "text": f"a {cat} thing"})
        assert r["data"]["ok"] is True, f"category {cat} failed: {r}"


@pytest.mark.asyncio
async def test_add_refuses_empty_text(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add

    r = await receipt_add.handler({"category": "made", "text": "   "})
    assert "refused" in r["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_add_with_date_string(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add

    r = await receipt_add.handler({
        "category": "moved", "text": "back-dated", "date": "yesterday",
    })
    expected = (date.today() - timedelta(days=1)).isoformat()
    assert r["data"]["date"] == expected


# ---------- today ----------


@pytest.mark.asyncio
async def test_today_after_adds(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_today

    await receipt_add.handler({"category": "made", "text": "a"})
    await receipt_add.handler({"category": "made", "text": "b"})
    await receipt_add.handler({"category": "avoided", "text": "c"})

    r = await receipt_today.handler({})
    payload = r["data"]
    assert payload["date"] == date.today().isoformat()
    assert payload["counts"]["made"] == 2
    assert payload["counts"]["avoided"] == 1
    assert payload["counts"]["moved"] == 0
    assert payload["counts"]["learned"] == 0
    assert len(payload["entries"]) == 3


@pytest.mark.asyncio
async def test_today_empty(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_today

    r = await receipt_today.handler({})
    assert r["data"]["counts"] == {"made": 0, "moved": 0, "learned": 0, "avoided": 0}
    assert r["data"]["entries"] == []
    assert r["data"]["note"] is None


# ---------- get ----------


@pytest.mark.asyncio
async def test_get_specific_date(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_get

    target = (date.today() - timedelta(days=3)).isoformat()
    await receipt_add.handler({
        "category": "learned", "text": "vector indexes are fiddly",
        "date": target,
    })
    r = await receipt_get.handler({"date": target})
    assert r["data"]["date"] == target
    assert r["data"]["counts"]["learned"] == 1
    assert r["data"]["entries"][0]["text"] == "vector indexes are fiddly"


@pytest.mark.asyncio
async def test_get_rejects_garbage_date(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_get

    r = await receipt_get.handler({"date": "not-a-date"})
    assert "refused" in r["content"][0]["text"].lower()


# ---------- week ----------


@pytest.mark.asyncio
async def test_week_includes_only_non_empty(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_week

    await receipt_add.handler({"category": "made", "text": "today thing"})
    await receipt_add.handler({
        "category": "moved", "text": "two days ago",
        "date": (date.today() - timedelta(days=2)).isoformat(),
    })
    r = await receipt_week.handler({"days": 5})
    assert r["data"]["non_empty_days"] == 2
    text = r["content"][0]["text"]
    assert "today thing" in text
    assert "two days ago" in text


@pytest.mark.asyncio
async def test_week_default_seven_days(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_week

    r = await receipt_week.handler({})
    assert r["data"]["days"] == 7
    # No entries at all → render_week emits the "no receipts in range." stub.
    assert "no receipts" in r["content"][0]["text"]


# ---------- search ----------


@pytest.mark.asyncio
async def test_search_matches_text_and_tags(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_search

    await receipt_add.handler({"category": "made", "text": "wrote essay draft",
                               "tags": ["writing"]})
    await receipt_add.handler({"category": "learned", "text": "indexes",
                               "tags": ["db"]})

    r = await receipt_search.handler({"query": "essay"})
    assert r["data"]["count"] == 1
    assert r["data"]["matches"][0]["text"] == "wrote essay draft"

    r2 = await receipt_search.handler({"query": "db"})
    assert r2["data"]["count"] == 1
    assert r2["data"]["matches"][0]["category"] == "learned"


@pytest.mark.asyncio
async def test_search_refuses_empty_query(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_search

    r = await receipt_search.handler({"query": "   "})
    assert "refused" in r["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_search_no_hits(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_search

    await receipt_add.handler({"category": "made", "text": "only one"})
    r = await receipt_search.handler({"query": "nope"})
    assert r["data"]["count"] == 0
    assert "no entries" in r["content"][0]["text"].lower()


# ---------- set_note ----------


@pytest.mark.asyncio
async def test_set_and_clear_note(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_set_note, receipt_today

    r = await receipt_set_note.handler({"text": "focused"})
    assert r["data"]["cleared"] is False
    assert r["data"]["date"] == date.today().isoformat()

    snap = await receipt_today.handler({})
    assert snap["data"]["note"] == "focused"

    cleared = await receipt_set_note.handler({"text": ""})
    assert cleared["data"]["cleared"] is True

    snap2 = await receipt_today.handler({})
    assert snap2["data"]["note"] is None


@pytest.mark.asyncio
async def test_set_note_refuses_missing_text(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_set_note

    r = await receipt_set_note.handler({})
    assert "refused" in r["content"][0]["text"].lower()


# ---------- print ----------


@pytest.mark.asyncio
async def test_print_renders_ascii(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_print

    await receipt_add.handler({"category": "made", "text": "shipped X"})
    r = await receipt_print.handler({})
    text = r["content"][0]["text"]
    assert "DAY RECEIPT" in text
    assert "MADE" in text
    assert "shipped X" in text
    assert r["data"]["width"] == 46


@pytest.mark.asyncio
async def test_print_respects_width(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_print

    long_text = "a" * 200
    await receipt_add.handler({"category": "made", "text": long_text})
    r = await receipt_print.handler({"width": 40})
    assert r["data"]["width"] == 40
    for line in r["content"][0]["text"].splitlines():
        assert len(line) <= 40, line


# ---------- delete ----------


@pytest.mark.asyncio
async def test_delete_happy_path(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_add, receipt_delete, receipt_today

    added = await receipt_add.handler({"category": "made", "text": "kill me"})
    entry_id = added["data"]["id"]

    deleted = await receipt_delete.handler({"entry_id": entry_id})
    assert deleted["data"]["ok"] is True
    assert deleted["data"]["id"] == entry_id

    snap = await receipt_today.handler({})
    assert snap["data"]["counts"]["made"] == 0


@pytest.mark.asyncio
async def test_delete_unknown_id(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_delete

    r = await receipt_delete.handler({"entry_id": 999_999})
    assert r["data"]["ok"] is False


@pytest.mark.asyncio
async def test_delete_refuses_missing_id(_isolated_receipt_db: Path):
    from tools.day_receipt import receipt_delete

    r = await receipt_delete.handler({})
    assert "refused" in r["content"][0]["text"].lower()


# ---------- isolation + bootstrap ----------


@pytest.mark.asyncio
async def test_db_isolation_per_test_part_1(_isolated_receipt_db: Path):
    """First half of a pair: write an entry, expect to find it."""
    from tools.day_receipt import receipt_add, receipt_today

    await receipt_add.handler({"category": "made", "text": "isolation marker"})
    snap = await receipt_today.handler({})
    assert snap["data"]["counts"]["made"] == 1


@pytest.mark.asyncio
async def test_db_isolation_per_test_part_2(_isolated_receipt_db: Path):
    """Second half: a fresh DAY_RECEIPT_DB path means no carryover."""
    from tools.day_receipt import receipt_today

    snap = await receipt_today.handler({})
    assert snap["data"]["counts"] == {"made": 0, "moved": 0, "learned": 0, "avoided": 0}
    assert snap["data"]["entries"] == []


@pytest.mark.asyncio
async def test_schema_bootstrap_idempotent(_isolated_receipt_db: Path):
    """Calling _ensure_schema twice should not raise — the sentinel
    short-circuits the second pass, and even if it didn't, the SQL is
    all ``CREATE TABLE IF NOT EXISTS``."""
    import sqlite3

    from tools.day_receipt import _db as receipt_db

    conn = sqlite3.connect(_isolated_receipt_db)
    try:
        receipt_db._ensure_schema(conn)
        receipt_db._ensure_schema(conn)  # second call: sentinel path
        receipt_db._reset_schema_sentinel()
        receipt_db._ensure_schema(conn)  # third call: full re-run path
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_env_override_routes_writes_to_chosen_path(tmp_path, monkeypatch):
    """An explicit DAY_RECEIPT_DB env override must land writes at that
    exact file — proves the resolver is read on every call, not cached
    at import time."""
    custom = tmp_path / "subdir" / "elsewhere.db"
    monkeypatch.setenv("DAY_RECEIPT_DB", str(custom))
    from tools.day_receipt import _db as receipt_db
    from tools.day_receipt import receipt_add
    receipt_db._reset_schema_sentinel()

    r = await receipt_add.handler({"category": "made", "text": "lands here"})
    assert r["data"]["ok"] is True
    assert custom.exists(), "schema bootstrap should have created the file"
