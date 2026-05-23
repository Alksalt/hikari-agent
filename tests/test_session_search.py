"""Sprint 5B — session_search FTS5 tool tests.

Five cases:
  1. query='kabocha' with 5 messages, 2 containing the token → 2 hits
  2. role='user' filter — only user-role rows returned
  3. since_iso filter — older messages excluded
  4. empty query → empty result, no crash
  5. wrapped output contains the untrusted-content envelope marker
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh per-test DB — mirrors test_entities_and_provenance.py."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


# ---------------------------------------------------------------------------
# 1. basic FTS hit count
# ---------------------------------------------------------------------------

def test_fts_basic_hit_count():
    """5 messages seeded, 2 contain 'kabocha' → exactly 2 hits returned."""
    db.append_message("user", "i love kabocha soup")
    db.append_message("assistant", "noted.")
    db.append_message("user", "what do you think about kabocha")
    db.append_message("assistant", "i'm indifferent.")
    db.append_message("user", "anyway let's talk about ramen")

    hits = db.messages_fts_search("kabocha", limit=10)
    assert len(hits) == 2
    contents = {h["content"] for h in hits}
    assert "i love kabocha soup" in contents
    assert "what do you think about kabocha" in contents


# ---------------------------------------------------------------------------
# 2. role filter
# ---------------------------------------------------------------------------

def test_fts_role_filter():
    """role='user' only returns user messages."""
    db.append_message("user", "kabocha is the best vegetable")
    db.append_message("assistant", "kabocha? really.")

    hits_user = db.messages_fts_search("kabocha", limit=10, role="user")
    assert len(hits_user) == 1
    assert hits_user[0]["role"] == "user"

    hits_asst = db.messages_fts_search("kabocha", limit=10, role="assistant")
    assert len(hits_asst) == 1
    assert hits_asst[0]["role"] == "assistant"


# ---------------------------------------------------------------------------
# 3. since_iso filter
# ---------------------------------------------------------------------------

def test_fts_since_iso_filter():
    """since_iso='2099-01-01' excludes all current rows."""
    db.append_message("user", "kabocha is wonderful")
    db.append_message("assistant", "kabocha verdict: yes")

    hits = db.messages_fts_search("kabocha", limit=10, since_iso="2099-01-01T00:00:00")
    assert hits == []


# ---------------------------------------------------------------------------
# 4. empty query returns empty, no crash
# ---------------------------------------------------------------------------

def test_fts_empty_query_no_crash():
    """Empty query string returns [] without raising."""
    db.append_message("user", "kabocha")
    hits = db.messages_fts_search("", limit=10)
    assert hits == []


# ---------------------------------------------------------------------------
# 5. session_search output wrapped in untrusted envelope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_search_tool_wraps_output():
    """session_search tool response body contains the untrusted-content envelope."""
    from tools.memory.session_search import session_search  # noqa: PLC0415

    db.append_message("user", "kabocha squash is underrated")

    result = await session_search.handler({"query": "kabocha"})
    text = result["content"][0]["text"]
    # The untrusted wrapper delimiter must be present.
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in text, (
        "session_search output must be wrapped in the untrusted-content envelope"
    )
    # Ensure hit data is attached.
    assert result.get("data", {}).get("hits")
    assert len(result["data"]["hits"]) == 1
