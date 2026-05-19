"""Phase 8 — 👍/👎 ground-truth for the persona-drift judge.

Covers:
  - telegram_message_id column added via idempotent migration
  - user_feedback table round-trip (record + recent)
  - rating validation (CHECK constraint)
  - feedback_compare_to_drift agree/disagree counts
  - update_last_assistant_telegram_msg_id stamps the latest assistant row
  - reaction handler maps 👍/👎 correctly and ignores other emojis + non-owner
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


# ---------- migration ----------

def test_telegram_message_id_column_present():
    """Idempotent migration adds the column."""
    with db._conn() as c:
        cols = {r["name"] for r in c.execute(
            "PRAGMA table_info(messages)"
        ).fetchall()}
    assert "telegram_message_id" in cols


def test_user_feedback_table_present():
    with db._conn() as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_feedback'"
        ).fetchall()
    assert len(rows) == 1


# ---------- round-trip ----------

def test_feedback_record_and_recent():
    db.append_message("assistant", "hi there")
    db.update_last_assistant_telegram_msg_id(42)
    fid = db.feedback_record(42, 1)
    assert fid > 0

    recent = db.feedback_recent(window_days=30)
    assert len(recent) == 1
    assert recent[0]["rating"] == 1
    assert recent[0]["content"] == "hi there"


def test_feedback_rejects_invalid_rating():
    with pytest.raises(ValueError):
        db.feedback_record(42, 0)
    with pytest.raises(ValueError):
        db.feedback_record(42, 2)


def test_update_last_assistant_msg_id_returns_none_when_no_assistant_row():
    """A user-only DB has no assistant rows — should return None, not raise."""
    db.append_message("user", "hello?")
    out = db.update_last_assistant_telegram_msg_id(99)
    assert out is None


def test_update_last_assistant_msg_id_picks_latest():
    db.append_message("assistant", "first")
    db.append_message("user", "between")
    db.append_message("assistant", "second")
    target_id = db.update_last_assistant_telegram_msg_id(777)
    assert target_id is not None
    with db._conn() as c:
        row = c.execute(
            "SELECT content, telegram_message_id FROM messages WHERE id = ?",
            (target_id,),
        ).fetchone()
    assert row["content"] == "second"
    assert row["telegram_message_id"] == 777


# ---------- compare to drift ----------

def _seed_drift(message_id: int, score: float, klass: str) -> None:
    db.drift_record(
        message_id=message_id,
        text_snippet="reply",
        score=score,
        class_label=klass,
        rubric_version=1,
        payload="",
    )


def test_feedback_compare_to_drift_agreement():
    """Judge says drifting and user says 👎 — that's agreement."""
    db.append_message("assistant", "as an AI, I cannot do that.")
    db.update_last_assistant_telegram_msg_id(11)
    with db._conn() as c:
        row = c.execute(
            "SELECT id FROM messages WHERE telegram_message_id = 11"
        ).fetchone()
    msg_id = int(row["id"])
    _seed_drift(msg_id, 0.2, "drifting")
    db.feedback_record(11, -1)

    out = db.feedback_compare_to_drift(window_days=7)
    assert out["agree"] == 1
    assert out["disagree"] == 0


def test_feedback_compare_to_drift_disagreement_example():
    """Judge says drifting but user said 👍 — rubric may be too strict."""
    db.append_message("assistant", "ugh. fine. here.")
    db.update_last_assistant_telegram_msg_id(22)
    with db._conn() as c:
        row = c.execute(
            "SELECT id FROM messages WHERE telegram_message_id = 22"
        ).fetchone()
    msg_id = int(row["id"])
    _seed_drift(msg_id, 0.3, "drifting")
    db.feedback_record(22, 1)

    out = db.feedback_compare_to_drift(window_days=7)
    assert out["disagree"] == 1
    assert out["agree"] == 0
    assert any("ugh. fine." in ex for ex in out["examples"])


def test_feedback_compare_does_not_double_count_resampled_messages():
    """Review-H5: a message may be sampled multiple times by the drift judge
    (no UNIQUE constraint on persona_drift_scores.message_id). The comparison
    must not double-count the SAME feedback row once per drift sample."""
    db.append_message("assistant", "ugh. fine. here.")
    db.update_last_assistant_telegram_msg_id(99)
    with db._conn() as c:
        row = c.execute(
            "SELECT id FROM messages WHERE telegram_message_id = 99"
        ).fetchone()
    msg_id = int(row["id"])
    # Three samples — most recent should be the one used.
    _seed_drift(msg_id, 0.2, "drifting")
    _seed_drift(msg_id, 0.3, "drifting")
    _seed_drift(msg_id, 0.85, "hikari")  # latest
    db.feedback_record(99, 1)
    out = db.feedback_compare_to_drift(window_days=7)
    # Latest sample is 0.85 ≥ 0.7 ("hikari"), user said 👍 → 1 agreement.
    # If we double-counted, we'd see 1 agree + 2 disagree (or similar).
    assert out["agree"] == 1
    assert out["disagree"] == 0


def test_feedback_compare_skips_unscored_messages():
    """If the drift judge never scored a message, feedback on it should be
    ignored by the comparison (we have nothing to compare against)."""
    db.append_message("assistant", "untouched")
    db.update_last_assistant_telegram_msg_id(33)
    db.feedback_record(33, 1)
    out = db.feedback_compare_to_drift(window_days=7)
    assert out["agree"] == 0
    assert out["disagree"] == 0


# ---------- reaction handler ----------

@pytest.mark.asyncio
async def test_reaction_handler_records_thumbs_up(monkeypatch):
    """The bridge handler must persist a +1 row keyed by the outbound msg_id."""
    from agents import telegram_bridge

    db.append_message("assistant", "reply text")
    db.update_last_assistant_telegram_msg_id(555)

    rxn = SimpleNamespace(
        user=SimpleNamespace(id=12345),
        message_id=555,
        new_reaction=[SimpleNamespace(emoji="👍")],
    )
    update = SimpleNamespace(message_reaction=rxn)

    # Stub isinstance check: bridge does isinstance(r, ReactionTypeEmoji).
    # We feed a SimpleNamespace, so monkeypatch the bridge's reference.
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    await telegram_bridge.handle_message_reaction(update, None)
    recent = db.feedback_recent(window_days=1)
    assert len(recent) == 1
    assert recent[0]["rating"] == 1


@pytest.mark.asyncio
async def test_reaction_handler_records_thumbs_down(monkeypatch):
    from agents import telegram_bridge

    rxn = SimpleNamespace(
        user=SimpleNamespace(id=12345),
        message_id=556,
        new_reaction=[SimpleNamespace(emoji="👎")],
    )
    update = SimpleNamespace(message_reaction=rxn)
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    await telegram_bridge.handle_message_reaction(update, None)
    recent = db.feedback_recent(window_days=1)
    assert len(recent) == 1
    assert recent[0]["rating"] == -1


@pytest.mark.asyncio
async def test_reaction_handler_rejects_non_owner(monkeypatch):
    from agents import telegram_bridge

    rxn = SimpleNamespace(
        user=SimpleNamespace(id=99999),  # not the owner
        message_id=557,
        new_reaction=[SimpleNamespace(emoji="👍")],
    )
    update = SimpleNamespace(message_reaction=rxn)
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    await telegram_bridge.handle_message_reaction(update, None)
    assert db.feedback_recent(window_days=1) == []


@pytest.mark.asyncio
async def test_reaction_handler_non_feedback_emoji_writes_no_feedback_row(
    monkeypatch, tmp_path,
):
    """Original Phase 8 semantic preserved: non-👍/👎 emojis never go to
    ``user_feedback``. Phase 9 added a reaction-as-turn path for those —
    suppress that here with config so this test stays focused on the
    feedback channel."""
    cfg_text = "reactions_as_turns:\n  enabled: false\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    from agents import config as _cfg
    _cfg.reload()

    from agents import telegram_bridge

    rxn = SimpleNamespace(
        user=SimpleNamespace(id=12345),
        chat=SimpleNamespace(id=12345),
        message_id=558,
        new_reaction=[SimpleNamespace(emoji="🌙")],
    )
    update = SimpleNamespace(message_reaction=rxn)
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())
    assert db.feedback_recent(window_days=1) == []


def _ctx_with_bot():
    from unittest.mock import AsyncMock
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=999)),
        send_chat_action=AsyncMock(),
    )
    return SimpleNamespace(bot=bot)


@pytest.mark.asyncio
async def test_reaction_handler_ignores_empty_reaction_removal(monkeypatch):
    """User removing a reaction sends new_reaction=[]; handler should no-op."""
    from agents import telegram_bridge

    rxn = SimpleNamespace(
        user=SimpleNamespace(id=12345),
        message_id=559,
        new_reaction=[],
    )
    update = SimpleNamespace(message_reaction=rxn)
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    await telegram_bridge.handle_message_reaction(update, None)
    assert db.feedback_recent(window_days=1) == []
