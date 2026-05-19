"""Phase 9 Stage C — non-verbal reply modes (sticker-only + reaction-only)."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import config, nonverbal as nonverbal_mod
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
    config.reload()
    yield


def _force_config(monkeypatch, tmp_path: Path, body: str) -> None:
    p = tmp_path / "engagement.yaml"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()


# ---------- decision: maybe_nonverbal_reply ----------

def test_substantive_message_always_falls_through(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: true\n  pool: ['fid']\n  "
                  "solo_reply_probability: 1.0\n"
                  "reactions:\n  enabled: true\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 1.0\n"
                  "nonverbal:\n  max_per_day: 99\n  min_text_for_substantive: 60\n")
    # Question always forces a real reply.
    assert nonverbal_mod.maybe_nonverbal_reply("what time is it?", "focused") is None
    # Long message also forces.
    long_msg = "x" * 80
    assert nonverbal_mod.maybe_nonverbal_reply(long_msg, "focused") is None


def test_substantive_openers_force_real_reply(monkeypatch, tmp_path):
    """Phase 9 review-F4: short imperative/conversational openers without a
    literal ``?`` should still get a real reply, not a sticker."""
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: true\n  pool: ['fid']\n  "
                  "solo_reply_probability: 1.0\n"
                  "reactions:\n  enabled: true\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 1.0\n"
                  "nonverbal:\n  max_per_day: 99\n  min_text_for_substantive: 60\n")
    substantive_short = [
        "can you check that",
        "could you take a look",
        "would you do this",
        "explain that paper",
        "tell me more",
        "help me with that",
        "show me the output",
        "what about the bug",
        "what's the eta",
        "how do i fix it",
        "why is it slow",
        "remind me about cabbage",
        "did i mention X",
    ]
    for msg in substantive_short:
        kind = nonverbal_mod.maybe_nonverbal_reply(msg, "focused")
        assert kind is None, f"opener {msg!r} should be substantive, got {kind!r}"


def test_sticker_only_fires_when_pool_present_and_probability_one(
    monkeypatch, tmp_path,
):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: true\n  pool: ['fid']\n  "
                  "solo_reply_probability: 1.0\n  mood_blocklist: []\n"
                  "reactions:\n  enabled: true\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 0.0\n"
                  "nonverbal:\n  max_per_day: 99\n")
    kind = nonverbal_mod.maybe_nonverbal_reply("hey", "focused")
    assert kind == "sticker"


def test_reaction_only_fires_when_sticker_empty_pool(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: true\n  pool: []\n  "
                  "solo_reply_probability: 1.0\n"
                  "reactions:\n  enabled: true\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 1.0\n"
                  "nonverbal:\n  max_per_day: 99\n")
    kind = nonverbal_mod.maybe_nonverbal_reply("hey", "focused")
    assert kind == "reaction"


def test_mood_blocklist_blocks_sticker_mode(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: true\n  pool: ['fid']\n  "
                  "solo_reply_probability: 1.0\n"
                  "  mood_blocklist: ['irritable']\n"
                  "reactions:\n  enabled: true\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 1.0\n"
                  "nonverbal:\n  max_per_day: 99\n")
    # Sticker blocked; reaction still possible.
    kind = nonverbal_mod.maybe_nonverbal_reply("hey", "irritable")
    assert kind == "reaction"


def test_daily_cap_short_circuits(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: true\n  pool: ['fid']\n  "
                  "solo_reply_probability: 1.0\n"
                  "reactions:\n  enabled: true\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 1.0\n"
                  "nonverbal:\n  max_per_day: 1\n")
    # Bump count manually to exceed cap.
    nonverbal_mod._bump_count()
    kind = nonverbal_mod.maybe_nonverbal_reply("hey", "focused")
    assert kind is None


def test_empty_user_text_returns_none(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: true\n  pool: ['fid']\n  "
                  "solo_reply_probability: 1.0\n"
                  "reactions:\n  enabled: true\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 1.0\n"
                  "nonverbal:\n  max_per_day: 99\n")
    assert nonverbal_mod.maybe_nonverbal_reply("", "focused") is None
    assert nonverbal_mod.maybe_nonverbal_reply("   ", "focused") is None


def test_both_disabled_returns_none(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  enabled: false\n  pool: ['fid']\n  "
                  "solo_reply_probability: 1.0\n"
                  "reactions:\n  enabled: false\n  pool: ['🌙']\n  "
                  "solo_reaction_probability: 1.0\n"
                  "nonverbal:\n  max_per_day: 99\n")
    assert nonverbal_mod.maybe_nonverbal_reply("hey", "focused") is None


# ---------- ship helpers ----------

@pytest.mark.asyncio
async def test_send_sticker_only_writes_marker(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  pool: ['fid_xyz']\n  enabled: true\n")
    bot = SimpleNamespace(send_sticker=AsyncMock())
    fid = await nonverbal_mod.send_sticker_only(bot, chat_id=12345)
    assert fid == "fid_xyz"
    bot.send_sticker.assert_awaited_once()

    with db._conn() as c:
        row = c.execute(
            "SELECT content FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["content"] == "[sticker-only]"


@pytest.mark.asyncio
async def test_send_sticker_only_empty_pool_returns_none(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  pool: []\n  enabled: true\n")
    bot = SimpleNamespace(send_sticker=AsyncMock())
    assert await nonverbal_mod.send_sticker_only(bot, 12345) is None
    bot.send_sticker.assert_not_called()


@pytest.mark.asyncio
async def test_send_reaction_only_writes_marker(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "reactions:\n  pool: ['🌙']\n  enabled: true\n")
    bot = SimpleNamespace(set_message_reaction=AsyncMock())
    emoji = await nonverbal_mod.send_reaction_only(bot, 12345, 999)
    assert emoji == "🌙"
    bot.set_message_reaction.assert_awaited_once()

    with db._conn() as c:
        row = c.execute(
            "SELECT content FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert "[reaction-only:" in row["content"]
    assert "🌙" in row["content"]


@pytest.mark.asyncio
async def test_send_sticker_only_tolerates_telegram_failure(monkeypatch, tmp_path):
    _force_config(monkeypatch, tmp_path,
                  "stickers:\n  pool: ['fid']\n  enabled: true\n")
    bot = SimpleNamespace(send_sticker=AsyncMock(side_effect=RuntimeError("api down")))
    result = await nonverbal_mod.send_sticker_only(bot, 12345)
    assert result is None
    # Marker NOT written when send fails.
    with db._conn() as c:
        rows = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    assert rows["n"] == 0


@pytest.mark.asyncio
async def test_bump_count_resets_on_new_day(monkeypatch):
    """A new UTC date resets the counter back to 1."""
    nonverbal_mod._bump_count()
    nonverbal_mod._bump_count()
    assert nonverbal_mod._peek_count() == 2

    # Pretend it's a new day by clearing the date key.
    db.runtime_set(nonverbal_mod._DAY_KEY, "2020-01-01")
    new = nonverbal_mod._bump_count()
    assert new == 1
