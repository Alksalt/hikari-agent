"""apply_new_facts is the extracted new-facts loop from run_daily_reflection.

Uses a local ``fresh_db`` fixture (the repo has no shared fixture of that
name in conftest.py; mirrors the reload + _reset_schema_sentinel pattern
established in tests/test_proactive_backoff.py / tests/test_daily_brief_collect.py).
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from agents.reflection import apply_new_facts
from storage import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield db
    db._reset_schema_sentinel()


def test_apply_new_facts_inserts(fresh_db, monkeypatch):
    async def fake_embed(*a, **kw):
        return None
    monkeypatch.setattr("agents.reflection._embed_fact", fake_embed)
    data = {
        "new_facts": [
            {"subject": "owner", "predicate": "has", "object": "english class fridays 13:00",
             "importance": 6, "confidence": 0.9, "source_text": "англ в пт на 13",
             "category": "event"},
            {"subject": "", "predicate": "x", "object": "y"},  # guarded: skipped
        ],
        "entities": [],
    }
    applied = asyncio.run(apply_new_facts(data))
    assert applied == 1


def test_apply_new_facts_empty_data_returns_zero(fresh_db, monkeypatch):
    async def fake_embed(*a, **kw):
        return None
    monkeypatch.setattr("agents.reflection._embed_fact", fake_embed)
    assert asyncio.run(apply_new_facts({})) == 0


def test_apply_new_facts_malformed_fact_skipped(fresh_db, monkeypatch):
    async def fake_embed(*a, **kw):
        return None
    monkeypatch.setattr("agents.reflection._embed_fact", fake_embed)
    # Missing required "subject" key entirely -> KeyError caught internally.
    data = {"new_facts": [{"predicate": "x", "object": "y"}]}
    assert asyncio.run(apply_new_facts(data)) == 0
