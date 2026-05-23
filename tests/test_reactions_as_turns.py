"""Phase 9 Stage A — reactions as conversational turns.

Channels:
  - 👍 / 👎  → silent ``user_feedback`` row (no reply)
  - other emoji → triggers a Hikari turn

Covers:
  - 👍 records feedback and does NOT trigger a turn (default config)
  - 👍 records feedback AND triggers a turn when feedback_emojis_also_reply=true
  - 🌙 triggers a turn (no feedback row)
  - cooldown blocks back-to-back reaction-turns
  - daily cap blocks further turns
  - irritable mood skips a fraction of turns
  - the synthetic prompt includes the previous message text + the emoji
  - missing prev assistant row falls back gracefully
  - non-owner reactions are rejected
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    config.reload()
    yield


def _reaction_update(emoji: str, message_id: int, user_id: int = 12345,
                     chat_id: int = 12345):
    rxn = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        new_reaction=[SimpleNamespace(emoji=emoji)],
    )
    return SimpleNamespace(message_reaction=rxn)


def _ctx_with_bot():
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=999)),
        send_chat_action=AsyncMock(),
    )
    return SimpleNamespace(bot=bot)


def _seed_prev_assistant(text: str, telegram_message_id: int) -> int:
    """Insert an assistant message + stamp it with a telegram_message_id."""
    db.append_message("assistant", text)
    db.update_last_assistant_telegram_msg_id(telegram_message_id)
    return telegram_message_id


@pytest.mark.asyncio
async def test_thumbs_up_records_feedback_and_does_not_reply(monkeypatch):
    from agents import telegram_bridge

    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)
    captured_replies: list = []

    async def fake_send(*a, **kw):
        captured_replies.append((a, kw))
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    _seed_prev_assistant("reply text", 555)
    update = _reaction_update("👍", 555)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    # Feedback row written.
    recent = db.feedback_recent(1)
    assert len(recent) == 1
    assert recent[0]["rating"] == 1
    # No reaction-turn reply.
    assert captured_replies == []


@pytest.mark.asyncio
async def test_other_emoji_triggers_turn_without_feedback(monkeypatch):
    from agents import telegram_bridge

    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    async def fake_respond(prompt):
        # Verify prompt carries context.
        assert "🌙" in prompt
        assert "previous message text" in prompt
        return "ugh. fine. hello."
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_respond)

    sends = []
    async def fake_send(bot, chat_id, text, *, elapsed_real=0.0):
        sends.append((chat_id, text))
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    _seed_prev_assistant("hikari said something", 600)
    update = _reaction_update("🌙", 600)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    assert sends == [(12345, "ugh. fine. hello.")]
    # NO feedback row written for non-👍/👎 emoji.
    assert db.feedback_recent(1) == []


@pytest.mark.asyncio
async def test_feedback_emojis_also_reply_when_configured(monkeypatch, tmp_path):
    cfg_text = (
        "reactions_as_turns:\n"
        "  enabled: true\n"
        "  feedback_emojis_also_reply: true\n"
        "  cooldown_sec: 0\n"
        "  max_per_day: 99\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    async def fake_respond(prompt):
        return "hm. ok."
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_respond)

    sends = []
    async def fake_send(bot, chat_id, text, *, elapsed_real=0.0):
        sends.append((chat_id, text))
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    _seed_prev_assistant("she said x", 700)
    update = _reaction_update("👍", 700)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    # Feedback AND a reply this time.
    assert len(db.feedback_recent(1)) == 1
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_cooldown_blocks_back_to_back_turns(monkeypatch, tmp_path):
    cfg_text = (
        "reactions_as_turns:\n"
        "  enabled: true\n"
        "  cooldown_sec: 60\n"
        "  max_per_day: 99\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    async def fake_respond(prompt):
        return "reply"
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_respond)

    sends = []
    async def fake_send(bot, chat_id, text, *, elapsed_real=0.0):
        sends.append(text)
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    _seed_prev_assistant("a", 800)
    update = _reaction_update("👀", 800)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    # First one fires, second blocked by cooldown.
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_daily_cap_blocks_after_threshold(monkeypatch, tmp_path):
    cfg_text = (
        "reactions_as_turns:\n"
        "  enabled: true\n"
        "  cooldown_sec: 0\n"
        "  max_per_day: 2\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    async def fake_respond(prompt):
        return "ok"
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_respond)

    sends = []
    async def fake_send(bot, chat_id, text, *, elapsed_real=0.0):
        sends.append(text)
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    _seed_prev_assistant("a", 900)
    update = _reaction_update("👀", 900)
    for _ in range(4):
        await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    # cap=2 should let two through.
    assert len(sends) == 2


@pytest.mark.asyncio
async def test_irritable_mood_skip_probability_one(monkeypatch, tmp_path):
    """When skip probability is 1.0, irritable mood always skips."""
    cfg_text = (
        "reactions_as_turns:\n"
        "  enabled: true\n"
        "  cooldown_sec: 0\n"
        "  max_per_day: 99\n"
        "  irritable_skip_probability: 1.0\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    db.upsert_core_block("mood_today", "irritable")

    async def fake_respond(prompt):
        return "should not be called"
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_respond)

    sends = []
    async def fake_send(*a, **kw):
        sends.append((a, kw))
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    _seed_prev_assistant("a", 1000)
    update = _reaction_update("👀", 1000)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())
    assert sends == []


@pytest.mark.asyncio
async def test_missing_prev_message_uses_fallback_prompt(monkeypatch):
    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    captured_prompts = []
    async def fake_respond(prompt):
        captured_prompts.append(prompt)
        return "ok"
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_respond)

    async def fake_send(*a, **kw):
        return None
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    # No seeded assistant row — telegram_message_id 9999 has no match.
    update = _reaction_update("🌙", 9999)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    assert len(captured_prompts) == 1
    assert "isn't in memory" in captured_prompts[0]


@pytest.mark.asyncio
async def test_non_owner_reaction_rejected(monkeypatch):
    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    async def fake_respond(prompt):
        return "should not be called"
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_respond)

    sends = []
    async def fake_send(*a, **kw):
        sends.append((a, kw))
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    update = _reaction_update("🌙", 1100, user_id=99999)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())
    assert sends == []
    # No feedback row from non-owner either.
    assert db.feedback_recent(1) == []


@pytest.mark.asyncio
async def test_empty_new_reaction_is_noop(monkeypatch):
    """Removing a reaction (empty new_reaction list) does nothing."""
    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    sends = []
    async def fake_send(*a, **kw):
        sends.append((a, kw))
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    rxn = SimpleNamespace(
        user=SimpleNamespace(id=12345),
        chat=SimpleNamespace(id=12345),
        message_id=1200,
        new_reaction=[],
    )
    update = SimpleNamespace(message_reaction=rxn)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())
    assert sends == []


@pytest.mark.asyncio
async def test_disabled_via_config(monkeypatch, tmp_path):
    cfg_text = (
        "reactions_as_turns:\n"
        "  enabled: false\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import telegram_bridge
    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    sends = []
    async def fake_send(*a, **kw):
        sends.append((a, kw))
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send)

    _seed_prev_assistant("a", 1300)
    update = _reaction_update("🌙", 1300)
    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())
    assert sends == []
