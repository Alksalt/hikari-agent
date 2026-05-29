"""tests/test_dialectic.py — unit tests for agents/dialectic.py:extract_post_turn.

Test matrix:
  1. Mocked aux LLM returns known insight → peer_insights row inserted
  2. LLM returns empty array → no row inserted
  3. LLM returns JSON parse error → no row inserted, returns 0
  4. LLM call itself raises → no row inserted, returns 0
  5. Multiple insights → all inserted (up to 3)
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


_SAMPLE_WINDOW = [
    {"role": "user", "content": "i don't want to talk about my job right now"},
    {"role": "assistant", "content": "okay. what else is going on?"},
    {"role": "user", "content": "just tired. my dad used to say sleep fixes everything"},
]


# ---------------------------------------------------------------------------
# 1. Known insight returned → row inserted
# ---------------------------------------------------------------------------

async def test_single_insight_inserted(monkeypatch):
    from agents import dialectic
    from storage import db

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        return json.dumps(["user tends to deflect about work"])

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn(_SAMPLE_WINDOW)
    assert count == 1

    rows = db.peer_insights_unsurfaced(limit=10)
    assert len(rows) == 1
    assert rows[0]["observation"] == "user tends to deflect about work"
    assert rows[0]["source"] == "dialectic"


# ---------------------------------------------------------------------------
# 2. LLM returns empty array → no row inserted
# ---------------------------------------------------------------------------

async def test_empty_array_no_row(monkeypatch):
    from agents import dialectic
    from storage import db

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        return "[]"

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn(_SAMPLE_WINDOW)
    assert count == 0
    assert db.peer_insights_unsurfaced(limit=10) == []


# ---------------------------------------------------------------------------
# 3. LLM returns malformed JSON → no row, returns 0
# ---------------------------------------------------------------------------

async def test_bad_json_returns_zero(monkeypatch):
    from agents import dialectic
    from storage import db

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        return "not valid json {{"

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn(_SAMPLE_WINDOW)
    assert count == 0
    assert db.peer_insights_unsurfaced(limit=10) == []


# ---------------------------------------------------------------------------
# 4. LLM call raises exception → returns 0, no crash
# ---------------------------------------------------------------------------

async def test_llm_exception_returns_zero(monkeypatch):
    from agents import dialectic
    from storage import db

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        raise RuntimeError("network failure")

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn(_SAMPLE_WINDOW)
    assert count == 0
    assert db.peer_insights_unsurfaced(limit=10) == []


# ---------------------------------------------------------------------------
# 5. Multiple insights (3) → all three rows inserted
# ---------------------------------------------------------------------------

async def test_multiple_insights_all_inserted(monkeypatch):
    from agents import dialectic
    from storage import db

    insights = [
        "tends to deflect about work",
        "brought up his father twice this week",
        "consistently frames problems as external",
    ]

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        return json.dumps(insights)

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn(_SAMPLE_WINDOW)
    assert count == 3

    rows = db.peer_insights_unsurfaced(limit=10)
    stored = {r["observation"] for r in rows}
    assert stored == set(insights)


# ---------------------------------------------------------------------------
# 6. LLM returns more than 3 → only first 3 inserted
# ---------------------------------------------------------------------------

async def test_more_than_3_capped_at_3(monkeypatch):
    from agents import dialectic
    from storage import db

    many = [f"insight {i}" for i in range(5)]

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        return json.dumps(many)

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn(_SAMPLE_WINDOW)
    assert count == 3  # capped at 3

    rows = db.peer_insights_unsurfaced(limit=10)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# 7. Empty message window → returns 0 without calling LLM
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 8. Fenced JSON with stray backtick inside a string value → fully parsed
#    (regression for fragile split("```") approach that discarded tail content)
# ---------------------------------------------------------------------------

async def test_fenced_json_with_stray_backtick_parses_fully(monkeypatch):
    """A backtick inside a JSON string value must not truncate the payload.

    The old split('```')[1] approach splits on the stray backtick and discards
    everything after it, causing partial or empty insight lists. The splitlines-
    based fence-strip only removes the first (opening) and last (closing) fence
    lines, leaving the body intact.
    """
    from agents import dialectic
    from storage import db

    # Fenced JSON where one string value contains a stray backtick
    stray_backtick_payload = (
        "```json\n"
        '["user avoids `direct` confrontation", "brings up deadlines under stress"]\n'
        "```"
    )

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        return stray_backtick_payload

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn(_SAMPLE_WINDOW)
    assert count == 2, f"expected 2 insights, got {count} — stray backtick may have truncated the payload"

    rows = db.peer_insights_unsurfaced(limit=10)
    observations = {r["observation"] for r in rows}
    assert "user avoids `direct` confrontation" in observations
    assert "brings up deadlines under stress" in observations

async def test_empty_window_returns_zero(monkeypatch):
    from agents import dialectic

    called = []

    async def _fake_aux(prompt, *, system=None, max_tokens=256):
        called.append(True)
        return "[]"

    monkeypatch.setattr("agents.dialectic.run_aux_composition", _fake_aux)

    count = await dialectic.extract_post_turn([])
    assert count == 0
    assert not called, "LLM should not be called for empty message window"
