"""Sprint 5B — /memory command handler tests (mock-based, pytest-asyncio).

Three-route surface:
  - /memory (no args) → recent facts list
  - /memory <freetext> → combined fact + session search
  - /memory fact|forget|correct <id> [<new>] → single-fact operations

Cases:
  1.  /memory (no args) → recent facts list (#id visible)
  2.  /memory <freetext> → combined search (match visible or "no matches")
  3.  /memory fact <id> → includes entity line
  4.  /memory forget <id> → fact marked invalid + voice confirm
  5.  /memory correct <id> <new> → new fact with attribution=user_corrected + entities linked + old superseded
  6.  null ts — /memory fact on pruned-source message (no crash)
  7.  non-owner user → silent (reply_text NOT called)
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from storage import db

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh per-test DB."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _make_update(user_id: int, args: list[str] | None = None):
    """Build a fake Update + Context pair with reply_text as AsyncMock."""
    message = MagicMock()
    message.reply_text = AsyncMock()

    user = MagicMock()
    user.id = user_id

    update = MagicMock()
    update.effective_user = user
    update.message = message
    update.effective_chat = MagicMock()
    update.effective_chat.id = user_id

    context = MagicMock()
    context.args = list(args) if args is not None else []

    return update, context


def _owner_id() -> int:
    """Return a stable fake owner id."""
    return 42


# Patch owner_id() for all tests in this module.
@pytest.fixture(autouse=True)
def _patch_owner(monkeypatch):
    monkeypatch.setattr(
        "agents.telegram_bridge.owner_id", _owner_id
    )


# Seed DB with 2 facts + entity links, return (fact_id_1, fact_id_2, entity_id).
@pytest.fixture()
def seeded_db():
    eid = db.entity_upsert("person", "Mochi")
    fid1 = db.insert_fact("user", "likes", "kabocha soup", importance=7,
                           confidence=0.9, attribution="user_stated")
    db.fact_entities_link(fid1, [eid])
    fid2 = db.insert_fact("user", "dislikes", "cold emails", importance=5,
                           confidence=0.8, attribution="hikari_inferred")
    db.append_message("user", "i love kabocha soup")
    db.append_message("assistant", "noted, kabocha it is.")
    return fid1, fid2, eid


# ---------------------------------------------------------------------------
# 1. no args → recent facts list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_no_args_returns_recent(seeded_db):
    """/memory with no args should return the recent facts list."""
    from agents.telegram_bridge import cmd_memory
    update, context = _make_update(_owner_id(), args=[])
    await cmd_memory(update, context)
    text = update.message.reply_text.call_args[0][0]
    fid1, fid2, _ = seeded_db
    # Either #fid1 or #fid2 should appear in the recent list
    assert f"#{fid1}" in text or f"#{fid2}" in text


# ---------------------------------------------------------------------------
# 2. freetext → combined search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_freetext_routes_to_search(seeded_db):
    """/memory <freetext> should route to combined fact + session search."""
    from agents.telegram_bridge import cmd_memory
    update, context = _make_update(_owner_id(), args=["kabocha"])
    await cmd_memory(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "kabocha" in text.lower()


# ---------------------------------------------------------------------------
# 3. fact <id> → includes entity line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_fact_includes_entity(seeded_db):
    fid1, fid2, eid = seeded_db
    from agents.telegram_bridge import cmd_memory
    update, context = _make_update(_owner_id(), args=["fact", str(fid1)])
    await cmd_memory(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert f"#{fid1}" in text
    # Entity line should include 'Mochi'
    assert "Mochi" in text


# ---------------------------------------------------------------------------
# 4. forget <id> → fact marked invalid + voice confirm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_forget_marks_invalid(seeded_db):
    fid1, fid2, eid = seeded_db
    from agents.telegram_bridge import cmd_memory
    update, context = _make_update(_owner_id(), args=["forget", str(fid1)])
    await cmd_memory(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert f"forgot {fid1}" in text
    # Verify the fact is now invalid in the DB.
    row = db.fact_by_id(fid1)
    assert row is not None
    assert row["status"] == "invalid"


# ---------------------------------------------------------------------------
# 5. correct <id> <new> → new fact attributed user_corrected + entities + old superseded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_correct_creates_replacement(seeded_db):
    fid1, fid2, eid = seeded_db
    from agents.telegram_bridge import cmd_memory
    update, context = _make_update(_owner_id(), args=["correct", str(fid1), "pumpkin curry"])
    await cmd_memory(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert f"corrected {fid1}" in text

    # Old fact should be superseded.
    old = db.fact_by_id(fid1)
    assert old["status"] == "superseded"

    # Find the new fact (supersedes the old one).
    new_id_str = text.split("→ new fact #")[1].split(".")[0].strip()
    new_id = int(new_id_str)
    new_fact = db.fact_by_id(new_id)
    assert new_fact is not None
    assert new_fact["object"] == "pumpkin curry"
    assert new_fact["attribution"] == "user_corrected"

    # Entity links should be preserved.
    with db._conn() as c:
        row = c.execute(
            "SELECT entity_id FROM fact_entities WHERE fact_id=? AND entity_id=?",
            (new_id, eid)
        ).fetchone()
    assert row is not None, "entity link not copied to replacement fact"


# ---------------------------------------------------------------------------
# 6. null ts — /memory fact on pruned-source message (no crash)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_fact_null_ts_no_crash():
    """Same null-ts guard for /memory fact <id>."""
    from agents.telegram_bridge import cmd_memory

    fid = db.insert_fact("user", "likes", "void", source_message_id=999,
                         attribution="user_stated")

    update, context = _make_update(_owner_id(), args=["fact", str(fid)])
    await cmd_memory(update, context)
    update.message.reply_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. non-owner user → silent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_non_owner_silent():
    from agents.telegram_bridge import cmd_memory
    update, context = _make_update(user_id=999, args=[])
    await cmd_memory(update, context)
    update.message.reply_text.assert_not_awaited()
