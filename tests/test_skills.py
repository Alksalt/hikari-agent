"""Tests for Phase 7 skills system.

Verifies:
- skill_list returns the 5 existing skills from .agents/skills/
- skill_read returns content
- skill_create stages to session_scratch
- skill_approve promotes from scratch to disk
- run_skill calls run_internal_control with skill content
- skill_promoter cooldown gate prevents repeated runs
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# skill_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skill_list_returns_existing_skills():
    from tools.skills.core import skill_list
    result = await skill_list.handler({})
    text = result["content"][0]["text"]
    ids = json.loads(text)
    assert isinstance(ids, list)
    # The repo ships 5 skills under .agents/skills/
    assert len(ids) >= 5
    assert "recall-memory" in ids


@pytest.mark.asyncio
async def test_skill_list_empty_when_no_dir(tmp_path, monkeypatch):
    import tools.skills.core as sc
    monkeypatch.setattr(sc, "_SKILLS_ROOT", tmp_path / "no_such_dir")
    result = await sc.skill_list.handler({})
    assert json.loads(result["content"][0]["text"]) == []


# ---------------------------------------------------------------------------
# skill_read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skill_read_returns_content():
    from tools.skills.core import skill_read
    result = await skill_read.handler({"skill_id": "recall-memory"})
    text = result["content"][0]["text"]
    assert "recall" in text.lower()


@pytest.mark.asyncio
async def test_skill_read_missing_skill():
    from tools.skills.core import skill_read
    result = await skill_read.handler({"skill_id": "does-not-exist-xyz"})
    assert "error" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# skill_create + skill_approve (integration via temp DB)
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(_db_mod, "_DB_PATH", db_path)
    yield _db_mod
    importlib.reload(_db_mod)


@pytest.mark.asyncio
async def test_skill_create_writes_to_session_scratch(isolated_db):
    """skill_create should write a staged_skill row to session_scratch."""
    _db = isolated_db
    from tools.skills.core import skill_create
    result = await skill_create.handler({
        "skill_id": "test-skill",
        "description": "A test skill",
        "content": "# Test\nDo X.",
    })
    text = result["content"][0]["text"]
    assert "staged" in text

    with _db._conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM session_scratch WHERE topic = ?",
            ("staged_skill:test-skill",),
        ).fetchone()
    assert row is not None
    data = json.loads(row[0])
    assert data["skill_id"] == "test-skill"
    assert data["content"] == "# Test\nDo X."


@pytest.mark.asyncio
async def test_skill_approve_promotes_to_disk(tmp_path, isolated_db, monkeypatch):
    """skill_approve should write SKILL.md and clean up session_scratch."""
    _db = isolated_db
    skills_root = tmp_path / ".agents" / "skills"
    import tools.skills.core as sc
    monkeypatch.setattr(sc, "_SKILLS_ROOT", skills_root)

    from tools.skills.core import skill_approve, skill_create
    # First, stage
    await skill_create.handler({
        "skill_id": "my-skill",
        "description": "desc",
        "content": "# My Skill\nDo Y.",
    })

    # Then, approve
    result = await skill_approve.handler({"skill_id": "my-skill"})
    text = result["content"][0]["text"]
    assert "saved" in text

    skill_file = skills_root / "my-skill" / "SKILL.md"
    assert skill_file.exists()
    assert "My Skill" in skill_file.read_text()

    # session_scratch row should be cleaned up
    with _db._conn() as conn:
        row = conn.execute(
            "SELECT id FROM session_scratch WHERE topic = ?",
            ("staged_skill:my-skill",),
        ).fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# run_skill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_skill_calls_run_internal_control(monkeypatch):
    """run_skill should read skill content and pass it to run_internal_control."""
    from tools.skills.core import run_skill

    captured: list[str] = []

    async def _fake_ric(prompt, **_kwargs):
        captured.append(prompt)
        return "skill result"

    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)
    result = await run_skill.handler({"skill_id": "recall-memory", "args": {}})
    text = result["content"][0]["text"]
    assert "skill result" in text
    assert len(captured) == 1
    # The skill content should be in the prompt
    assert "recall" in captured[0].lower()


@pytest.mark.asyncio
async def test_run_skill_missing_returns_error():
    from tools.skills.core import run_skill
    result = await run_skill.handler({"skill_id": "nonexistent-xyz", "args": {}})
    assert "error" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# skill_promoter cooldown
# ---------------------------------------------------------------------------

def test_skill_promoter_cooldown_gate(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from agents import skill_promoter

    three_days_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()

    import storage.db as _db
    monkeypatch.setattr(_db, "runtime_get", lambda key: three_days_ago if key == "skill_promoter.last_run" else None)
    assert skill_promoter._is_on_cooldown() is True


def test_skill_promoter_cooldown_expired(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from agents import skill_promoter

    ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()

    import storage.db as _db
    monkeypatch.setattr(_db, "runtime_get", lambda key: ten_days_ago if key == "skill_promoter.last_run" else None)
    assert skill_promoter._is_on_cooldown() is False
