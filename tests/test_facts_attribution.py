"""facts.attribution column — pure-additive enum for tagging where a fact
came from. NULL = legacy/unknown. Tested values: user_stated, user_observed,
hikari_inferred, subagent_extracted, external_source.

The column lands via a migration fn (not _SCHEMA) because tests use fresh DBs
and the project's MEMORY.md schema-migration-ordering rule requires
ALTER-added columns to be applied in migrations, not the schema bootstrap."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    """Fresh per-test DB. Mirrors tests/test_facts_recall_decay.py:23-39."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def test_facts_has_attribution_column():
    """Fresh DB after migration chain has facts.attribution."""
    # Trigger schema/migration application via a no-op _conn() open.
    with db._conn() as c:
        cols = {row["name"] for row in c.execute("PRAGMA table_info(facts)").fetchall()}
    assert "attribution" in cols


def test_facts_attribution_migration_idempotent():
    """Running migrations twice doesn't blow up (e.g. duplicate ALTER)."""
    with db._conn() as c:
        cols = [row["name"] for row in c.execute("PRAGMA table_info(facts)").fetchall()]
    assert cols.count("attribution") == 1
    # Force re-run of schema bootstrap path.
    db._reset_schema_sentinel()
    with db._conn() as c:
        cols2 = [row["name"] for row in c.execute("PRAGMA table_info(facts)").fetchall()]
    assert cols2.count("attribution") == 1


def test_insert_fact_accepts_attribution():
    """insert_fact takes an optional attribution kwarg and stores it.
    Uses the autouse _isolated fixture above for a fresh DB."""
    fact_id = db.insert_fact(
        subject="user", predicate="likes", object_="cold rice",
        attribution="user_stated",
    )
    with db._conn() as c:
        row = c.execute(
            "SELECT attribution FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert row["attribution"] == "user_stated"


def test_insert_fact_attribution_defaults_null():
    """Without attribution kwarg, the column is NULL (preserves legacy behavior)."""
    fact_id = db.insert_fact(
        subject="user", predicate="likes", object_="something",
    )
    with db._conn() as c:
        row = c.execute(
            "SELECT attribution FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert row["attribution"] is None


def test_remember_tool_tags_user_stated():
    """The remember tool stores facts with attribution='user_stated'.
    Uses the autouse _isolated fixture above."""
    import asyncio
    from tools.memory.remember import remember
    # The @tool decorator wraps the fn as SdkMcpTool; call via .handler.
    handler = getattr(remember, "handler", remember)
    result = asyncio.run(handler({
        "subject": "user", "predicate": "owns", "object": "macbook m3",
    }))
    fact_id = result["data"]["fact_id"]
    with db._conn() as c:
        row = c.execute(
            "SELECT attribution FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert row["attribution"] == "user_stated"


def test_reflection_call_sites_pass_attribution():
    """Each db.insert_fact call in agents/reflection.py passes
    attribution='hikari_inferred' — reflection extracts facts via Hikari's
    own LLM pass, not from a direct user statement."""
    import re
    src = open("agents/reflection.py").read()
    # Find all insert_fact( ... ) call bodies up to closing paren depth 0.
    # Each must include attribution='hikari_inferred' (or "hikari_inferred").
    call_starts = [m.start() for m in re.finditer(r"db\.insert_fact\(", src)]
    assert len(call_starts) >= 3, f"expected ≥3 insert_fact calls, found {len(call_starts)}"
    for start in call_starts:
        # Walk paren depth.
        depth = 0
        i = start + len("db.insert_fact")
        while i < len(src):
            if src[i] == "(": depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call_body = src[start:i + 1]
        assert "hikari_inferred" in call_body, (
            f"insert_fact call at offset {start} missing "
            f"attribution='hikari_inferred':\n{call_body}"
        )
