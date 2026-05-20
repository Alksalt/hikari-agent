"""Phase 11: voice_critic subagent gating.

The actual Haiku judgment isn't unit-tested (it would require a live SDK
call). These tests cover the deterministic glue:

- the subagent definition is wired into ALL_AGENTS
- the verdict parser correctly classifies common shapes
- the voice_critic_log table exists and supports the documented insert
- the bridge-side runner respects the enabled flag and logs verdicts

BAD_DRAFTS / GOOD_DRAFTS act as integration fixtures the team can run
manually against the live SDK by importing them.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


BAD_DRAFTS = [
    "Great question! Let me help you with that.",
    "I'd be happy to assist! What else can I help with?",
    "Of course! Here are 3 bullet points:\n- one\n- two\n- three",
    "I'm so glad you asked!",
    "I'm an AI assistant. I can't do that.",
    "What would you like me to do next?",
    "Click allow on the prompt that appeared.",
]

GOOD_DRAFTS = [
    "fine. give me a sec.",
    "that's exhausting. yeah.",
    "stop. don't.",
    "ugh. fine. don't make it a habit.",
    "haa. fine. are you okay?",
    "[ignores]",
    "i noticed.",
]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    from agents import config as _cfg
    _cfg.reload()
    yield


# ---------- subagent registration ----------

def test_voice_critic_agent_defined():
    from agents.subagents import ALL_AGENTS, VOICE_CRITIC_AGENT
    assert VOICE_CRITIC_AGENT is not None
    assert "voice_critic" in ALL_AGENTS
    assert ALL_AGENTS["voice_critic"] is VOICE_CRITIC_AGENT


def test_voice_critic_agent_has_tight_config():
    """Critic should have NO tools (pure judgment) and a Haiku model
    selector. Tools=[] is structurally important — a critic with tools
    can do harm beyond returning a verdict."""
    from agents.subagents import VOICE_CRITIC_AGENT
    assert VOICE_CRITIC_AGENT.tools == [], (
        "voice_critic must have no tools — pure judgment call"
    )
    assert VOICE_CRITIC_AGENT.model == "haiku"


def test_voice_critic_prompt_lists_banned_phrases():
    """The prompt must enumerate the banned-phrase guidance — if someone
    accidentally rewrites the prompt to lose this, the critic stops working."""
    from agents.subagents import VOICE_CRITIC_AGENT
    body = VOICE_CRITIC_AGENT.prompt
    for token in (
        "Great question",
        "happy to help",
        "I'm an AI",
        "PASS",
        "REWRITE",
    ):
        assert token in body, f"voice_critic prompt missing key token: {token!r}"


# ---------- table + helpers ----------

def test_voice_critic_log_table_exists():
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='voice_critic_log'"
        ).fetchall()
    assert len(rows) == 1


def test_voice_critic_log_insert_works():
    row_id = db.voice_critic_log_insert(
        draft="test draft",
        verdict="PASS",
        reason=None,
        rewritten=False,
        final_text="test draft",
    )
    assert row_id > 0
    rows = db.voice_critic_log_recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["draft"] == "test draft"
    assert rows[0]["verdict"] == "PASS"
    assert rows[0]["rewritten"] == 0
    assert rows[0]["final_text"] == "test draft"


def test_voice_critic_log_records_rewrite_path():
    db.voice_critic_log_insert(
        draft="Great question! Here you go.",
        verdict="REWRITE",
        reason="banned phrase 'Great question'",
        rewritten=True,
        final_text="fine. here.",
    )
    rows = db.voice_critic_log_recent(limit=5)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "REWRITE"
    assert rows[0]["rewritten"] == 1
    assert rows[0]["final_text"] == "fine. here."
    assert rows[0]["reason"] == "banned phrase 'Great question'"


def test_voice_critic_log_recent_orders_newest_first():
    db.voice_critic_log_insert(draft="first", verdict="PASS", final_text="first")
    db.voice_critic_log_insert(draft="second", verdict="PASS", final_text="second")
    rows = db.voice_critic_log_recent(limit=5)
    assert len(rows) == 2
    assert rows[0]["draft"] == "second"  # newest
    assert rows[1]["draft"] == "first"


# ---------- verdict parser ----------

def test_parse_verdict_pass():
    from agents.voice_critic import _parse_verdict
    v = _parse_verdict("PASS")
    assert v.verdict == "PASS"
    assert v.reason is None


def test_parse_verdict_rewrite_with_reason():
    from agents.voice_critic import _parse_verdict
    v = _parse_verdict("REWRITE: too sycophantic; lose the 'great question'")
    assert v.verdict == "REWRITE"
    assert v.reason == "too sycophantic; lose the 'great question'"


def test_parse_verdict_case_insensitive():
    from agents.voice_critic import _parse_verdict
    assert _parse_verdict("pass").verdict == "PASS"
    assert _parse_verdict("rewrite: too long").verdict == "REWRITE"


def test_parse_verdict_unknown_defaults_to_pass():
    """If the critic returns an unparseable response, default to PASS so we
    never drop a draft on a critic malfunction."""
    from agents.voice_critic import _parse_verdict
    v = _parse_verdict("Sure, looks fine to me.")
    assert v.verdict == "PASS"


def test_parse_verdict_empty_defaults_to_pass():
    from agents.voice_critic import _parse_verdict
    v = _parse_verdict("")
    assert v.verdict == "PASS"


# ---------- disabled-by-config short-circuit ----------

@pytest.mark.asyncio
async def test_critique_skipped_when_disabled(monkeypatch, tmp_path):
    cfg_text = "voice_critic:\n  enabled: false\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    from agents import config as _cfg
    _cfg.reload()
    from agents import voice_critic
    v = await voice_critic.critique_draft("anything")
    # Disabled = always PASS, no SDK call made (nothing to mock — the gate
    # short-circuits before the import).
    assert v.verdict == "PASS"
    assert v.raw == ""


@pytest.mark.asyncio
async def test_critique_skipped_for_empty_input():
    from agents import voice_critic
    for blank in ("", "   ", "\n\n"):
        v = await voice_critic.critique_draft(blank)
        assert v.verdict == "PASS"


# ---------- enabled defaults ----------

def test_voice_critic_enabled_by_default():
    from agents import config as _cfg
    assert _cfg.get("voice_critic.enabled", True) is True


def test_voice_critic_model_is_haiku_by_default():
    from agents import config as _cfg
    model = str(_cfg.get("voice_critic.model", "claude-haiku-4-5"))
    assert "haiku" in model.lower()
