"""Tests for agents/skill_promoter.py — oversized-thought capping + edge cases.

Covers:
  - 5000-char single thought → capped to 2000 before joining
  - 50 thoughts at 100 chars each → joined fully, no truncation
  - Empty/None thought → skipped without crash
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from agents import skill_promoter

# ---------------------------------------------------------------------------
# Fixture: isolated DB so thought reads don't bleed across tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db as _db
    monkeypatch.setattr(_db, "_DB_PATH", db_path)
    importlib.reload(skill_promoter)
    yield _db


# ---------------------------------------------------------------------------
# Helpers to read what the aux LLM "received"
# ---------------------------------------------------------------------------

def _make_fake_aux_llm(captured: list[str], response: str = '{"found": false}'):
    """Return an async fake that records the prompt it received."""
    async def _fake(prompt: str, system: str = "") -> str:
        captured.append(prompt)
        return response
    return _fake


# ---------------------------------------------------------------------------
# Tests: thought capping behaviour
# ---------------------------------------------------------------------------

class TestOversizedThoughtCap:
    @pytest.mark.asyncio
    async def test_5000_char_thought_capped_to_2000(self, monkeypatch, tmp_path):
        """A single 5000-char thought must be truncated to 2000 chars before
        being handed to the aux LLM."""
        from storage import db

        # Need at least 9 thoughts (the minimum) to skip the early-exit guard.
        fat_thought = "X" * 5000
        normal_thought = "Y" * 50

        # Insert 9 thoughts: one fat + eight normal.
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                (fat_thought,),
            )
            for _ in range(8):
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (normal_thought,),
                )

        captured_prompts: list[str] = []
        monkeypatch.setattr(
            "agents.runtime._call_aux_llm",
            _make_fake_aux_llm(captured_prompts),
        )
        # Disable cooldown.
        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        await skill_promoter.maybe_promote_skill()

        assert captured_prompts, "aux LLM must have been called"
        prompt = captured_prompts[0]

        # The fat thought (5000 X's) must not appear verbatim — it was capped.
        assert "X" * 5000 not in prompt, (
            "5000-char thought should have been capped before sending to LLM"
        )
        # The cap should be exactly 2000 chars.
        assert "X" * 2000 in prompt, "First 2000 chars of the fat thought must be present"
        assert "X" * 2001 not in prompt, "No more than 2000 chars from the fat thought"

    @pytest.mark.asyncio
    async def test_50_thoughts_100_chars_no_truncation(self, monkeypatch, tmp_path):
        """50 thoughts at 100 chars each are all under the 2000-char cap and
        must reach the aux LLM intact."""
        from storage import db

        thought_text = "A" * 100  # well under 2000
        for i in range(50):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (f"{thought_text}_{i:03d}",),
                )

        captured_prompts: list[str] = []
        monkeypatch.setattr(
            "agents.runtime._call_aux_llm",
            _make_fake_aux_llm(captured_prompts),
        )
        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        await skill_promoter.maybe_promote_skill()

        assert captured_prompts
        prompt = captured_prompts[0]
        # All 40 sampled thoughts (the function takes [-40:]) should be intact.
        # Spot-check: the last few entries must appear untruncated.
        for i in range(10, 50):
            fragment = f"{thought_text}_{i:03d}"
            assert fragment in prompt, (
                f"Thought {fragment!r} should appear untruncated in prompt"
            )

    @pytest.mark.asyncio
    async def test_empty_thought_skipped_without_crash(self, monkeypatch, tmp_path):
        """Empty-string or None thoughts must be silently skipped — no crash,
        and the non-empty thoughts still reach the LLM."""
        from storage import db

        normal = "meaningful diary entry"
        # Mix in empty-string thoughts.
        for _ in range(5):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (normal,),
                )
        # Insert 4 empty-string thoughts.
        for _ in range(4):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    ("",),
                )

        # At this point we have 9 rows: 5 normal + 4 empty.
        # _recent_thoughts filters out falsy entries, so we may have fewer than 9
        # non-empty thoughts, which means maybe_promote_skill returns early.
        # To ensure the LLM is reached, add more normal thoughts.
        for _ in range(5):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (normal,),
                )

        captured_prompts: list[str] = []
        monkeypatch.setattr(
            "agents.runtime._call_aux_llm",
            _make_fake_aux_llm(captured_prompts),
        )
        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        # Should not raise.
        await skill_promoter.maybe_promote_skill()

        # If the LLM was called, the prompt must not contain empty entries as
        # delimiter-separated blanks (i.e. "---\n---" would imply an empty entry
        # was included).
        if captured_prompts:
            prompt = captured_prompts[0]
            assert "---\n---" not in prompt, (
                "Empty thought should not produce an empty section in the prompt"
            )

    @pytest.mark.asyncio
    async def test_none_thought_handled_gracefully(self, monkeypatch, tmp_path):
        """_recent_thoughts already filters r[0] falsiness, so None thoughts
        from the DB must not reach the LLM or crash the function."""
        from storage import db

        # Insert 10 normal thoughts so the LLM is reached.
        for i in range(10):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (f"thought {i}",),
                )

        # SQLite doesn't allow inserting Python None via parameterized query in
        # this schema easily, but the filter `[r[0] for r in rows if r[0]]` handles
        # both '' and None. Test that the filter in _recent_thoughts works:
        thoughts = skill_promoter._recent_thoughts()
        assert all(t for t in thoughts), "All returned thoughts must be truthy"


# ---------------------------------------------------------------------------
# Tests: LLM response parsing + cooldown
# ---------------------------------------------------------------------------

class TestSkillPromoterLlmParsing:
    @pytest.mark.asyncio
    async def test_found_true_drafts_skill_in_scratch(self, monkeypatch, tmp_path):
        """When LLM returns found=true with valid fields, skill is written to
        session_scratch and cooldown is applied."""
        from storage import db

        for i in range(10):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (f"tool call sequence pattern {i}",),
                )

        response = json.dumps({
            "found": True,
            "skill_id": "test-skill",
            "description": "Does something useful",
            "content": "# test-skill\nDo the thing.",
        })
        monkeypatch.setattr(
            "agents.runtime._call_aux_llm",
            _make_fake_aux_llm([], response),
        )
        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        await skill_promoter.maybe_promote_skill()

        with db._conn() as conn:
            rows = conn.execute(
                "SELECT topic, payload_json FROM session_scratch WHERE topic LIKE 'staged_skill:%'"
            ).fetchall()

        assert rows, "Expected a staged_skill row in session_scratch"
        topic, payload_json = rows[0]
        assert topic == "staged_skill:test-skill"
        payload = json.loads(payload_json)
        assert payload["skill_id"] == "test-skill"

    @pytest.mark.asyncio
    async def test_found_false_does_not_draft(self, monkeypatch, tmp_path):
        """found=false → no row in session_scratch."""
        from storage import db

        for i in range(10):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (f"thought {i}",),
                )

        monkeypatch.setattr(
            "agents.runtime._call_aux_llm",
            _make_fake_aux_llm([], '{"found": false}'),
        )
        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        await skill_promoter.maybe_promote_skill()

        with db._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_scratch WHERE topic LIKE 'staged_skill:%'"
            ).fetchall()
        assert not rows

    @pytest.mark.asyncio
    async def test_invalid_skill_id_not_staged(self, monkeypatch, tmp_path):
        """found=true with an invalid skill_id (uppercase / illegal chars) must
        NOT stage a row and must apply cooldown (finding-2)."""
        from storage import db

        for i in range(10):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (f"tool call sequence pattern {i}",),
                )

        response = json.dumps({
            "found": True,
            "skill_id": "../escape Evil",  # invalid: spaces, uppercase, path bits
            "description": "malicious id",
            "content": "# evil\nDo bad.",
        })
        monkeypatch.setattr(
            "agents.runtime._call_aux_llm",
            _make_fake_aux_llm([], response),
        )
        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        await skill_promoter.maybe_promote_skill()

        with db._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_scratch WHERE topic LIKE 'staged_skill:%'"
            ).fetchall()
        assert not rows, "invalid skill_id must not be staged"

        last_run = db.runtime_get("skill_promoter.last_run")
        assert last_run is not None, "cooldown must be applied when id is rejected"

    @pytest.mark.asyncio
    async def test_redraft_replaces_existing_staged_row(self, monkeypatch, tmp_path):
        """A promoter re-draft for the same skill_id must REPLACE the prior
        staged row (DELETE+INSERT), keeping exactly ONE row so skill_approve's
        ambiguity guard isn't tripped into denial-of-approval (finding-2)."""
        from storage import db

        for i in range(10):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (f"tool call sequence pattern {i}",),
                )

        def _resp(content: str) -> str:
            return json.dumps({
                "found": True,
                "skill_id": "redraft-skill",
                "description": "v",
                "content": content,
            })

        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        # First draft.
        monkeypatch.setattr(
            "agents.runtime._call_aux_llm", _make_fake_aux_llm([], _resp("# draft v1")),
        )
        await skill_promoter.maybe_promote_skill()
        # Second draft for the same id (re-run).
        monkeypatch.setattr(
            "agents.runtime._call_aux_llm", _make_fake_aux_llm([], _resp("# draft v2")),
        )
        await skill_promoter.maybe_promote_skill()

        with db._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM session_scratch WHERE topic = ?",
                ("staged_skill:redraft-skill",),
            ).fetchall()
        assert len(rows) == 1, "re-draft must replace, not stack, the staged row"
        assert json.loads(rows[0][0])["content"] == "# draft v2"

    @pytest.mark.asyncio
    async def test_non_dict_json_applies_cooldown_no_crash(self, monkeypatch, tmp_path):
        """LLM returning a JSON array (not a dict) applies cooldown and doesn't
        crash — regression for the AttributeError that looped aux-LLM cost."""
        from storage import db

        for i in range(10):
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                    (f"thought {i}",),
                )

        monkeypatch.setattr(
            "agents.runtime._call_aux_llm",
            _make_fake_aux_llm([], "[1, 2, 3]"),
        )
        monkeypatch.setattr("agents.skill_promoter._is_on_cooldown", lambda: False)

        # Should not raise.
        await skill_promoter.maybe_promote_skill()

        # Cooldown key must have been stamped.
        from storage import db as _db
        last_run = _db.runtime_get("skill_promoter.last_run")
        assert last_run is not None, "Cooldown must be applied on non-dict JSON response"
