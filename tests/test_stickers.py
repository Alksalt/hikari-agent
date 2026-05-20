"""Phase-7 Stage B-1 tests: sticker scaffold (mirror of reactions tests).

Pool ships empty by default — exercise the empty-pool guard, the mood gate,
the cooldown gate, and the disabled flag.
"""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import config, stickers
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


def _write_cfg(tmp_path: Path, monkeypatch, yaml_text: str) -> None:
    p = tmp_path / "engagement.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()


def test_stickers_empty_pool_returns_false_and_none(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  probability_per_reply: 1.0\n"
        "  cooldown_min_messages: 0\n"
        "  mood_blocklist: []\n"
        "  pool: []\n"
    )
    assert stickers.pick_sticker_file_id() is None
    assert not stickers.should_send_sticker(now_counter=100)


def test_stickers_with_pool_and_no_cooldown_fires(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  probability_per_reply: 1.0\n"   # force-on
        "  cooldown_min_messages: 5\n"
        "  mood_blocklist: []\n"
        "  pool: ['CAACAgEAAxkBAAEFAKE_FILE_ID']\n"
    )
    # No prior send recorded → cooldown doesn't apply yet.
    assert stickers.should_send_sticker(now_counter=1)


def test_stickers_cooldown_blocks(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  probability_per_reply: 1.0\n"
        "  cooldown_min_messages: 5\n"
        "  mood_blocklist: []\n"
        "  pool: ['CAACAgEAAxkBAAEFAKE_FILE_ID']\n"
    )
    # First call fires.
    assert stickers.should_send_sticker(now_counter=1)
    # Record send at counter=1.
    db.runtime_set("stickers_last_at_counter", 1)
    # Cooldown blocks counters 2-5.
    assert not stickers.should_send_sticker(now_counter=3)
    assert not stickers.should_send_sticker(now_counter=5)
    # Counter=6 is past the cooldown window.
    assert stickers.should_send_sticker(now_counter=6)


def test_stickers_mood_blocklist_irritable_blocks(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  probability_per_reply: 1.0\n"
        "  cooldown_min_messages: 0\n"
        "  mood_blocklist: ['irritable']\n"
        "  pool: ['CAACAgEAAxkBAAEFAKE_FILE_ID']\n"
    )
    db.upsert_core_block("mood_today", "irritable")
    assert not stickers.should_send_sticker(now_counter=100)
    # Sanity: flip mood to focused → it would now fire.
    db.upsert_core_block("mood_today", "focused")
    assert stickers.should_send_sticker(now_counter=100)


def test_stickers_disabled_returns_false(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: false\n"
        "  probability_per_reply: 1.0\n"
        "  cooldown_min_messages: 0\n"
        "  mood_blocklist: []\n"
        "  pool: ['CAACAgEAAxkBAAEFAKE_FILE_ID']\n"
    )
    assert not stickers.should_send_sticker(now_counter=100)


def test_stickers_pick_file_id_from_pool(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  pool: ['ID_A', 'ID_B']\n"
    )
    for _ in range(20):
        fid = stickers.pick_sticker_file_id()
        assert fid in ("ID_A", "ID_B")


def test_stickers_bump_outbound_counter(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch, "stickers:\n  enabled: true\n  pool: []\n")
    # Counter starts at 0.
    assert db.runtime_get_int(db.OUTBOUND_MSG_COUNTER_KEY, 0) == 0
    assert stickers._bump_outbound_counter() == 1
    assert stickers._bump_outbound_counter() == 2
    assert db.runtime_get_int(db.OUTBOUND_MSG_COUNTER_KEY, 0) == 2


# ---------- force_send_sticker (image_gen-down fallback) ----------

@pytest.mark.asyncio
async def test_force_send_sticker_sends_when_pool_nonempty(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  probability_per_reply: 0.0\n"  # would normally block — force ignores it
        "  cooldown_min_messages: 999\n"   # would normally block — force ignores it
        "  mood_blocklist: ['irritable']\n"
        "  pool: ['abc123']\n"
    )
    mock_bot = AsyncMock()
    fid = await stickers.force_send_sticker(mock_bot, 999)
    assert fid == "abc123"
    mock_bot.send_sticker.assert_awaited_once_with(chat_id=999, sticker="abc123")


@pytest.mark.asyncio
async def test_force_send_sticker_returns_none_when_pool_empty(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  pool: []\n"
    )
    mock_bot = AsyncMock()
    fid = await stickers.force_send_sticker(mock_bot, 999)
    assert fid is None
    mock_bot.send_sticker.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_send_sticker_ignores_mood_and_cooldown(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  probability_per_reply: 0.0\n"
        "  cooldown_min_messages: 999\n"
        "  mood_blocklist: ['irritable']\n"
        "  pool: ['xyz']\n"
    )
    # Mood that the regular gate would block on.
    db.upsert_core_block("mood_today", "irritable")
    # Cooldown that the regular gate would block on (last send "just now").
    db.runtime_set("stickers_last_at_counter", 999_999)

    mock_bot = AsyncMock()
    fid = await stickers.force_send_sticker(mock_bot, 42)
    assert fid == "xyz"
    mock_bot.send_sticker.assert_awaited_once_with(chat_id=42, sticker="xyz")
    # Forced send must NOT reset the regular cooldown counter.
    assert db.runtime_get_int("stickers_last_at_counter", 0) == 999_999


# ---------- generate_photo failure path sets runtime flag ----------

@pytest.mark.asyncio
async def test_generate_photo_sets_failure_ts_on_flux_failure(monkeypatch, tmp_path):
    """When _call_flux returns None, generate_photo stamps a recent ISO ts
    into runtime_state under the agreed key — that's the bridge's signal."""
    from tools.photos import generate as gen_mod

    # Make sure mood doesn't refuse and cap isn't hit.
    db.upsert_core_block("mood_today", "focused")

    async def _fake_flux_none(prompt, model):
        return None

    monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux_none)

    # Ensure flag is clear pre-call.
    db.runtime_set("image_gen_last_failure_ts", None)
    assert db.runtime_get("image_gen_last_failure_ts") is None

    # The SDK @tool decorator wraps; call the underlying coroutine via .__wrapped__
    # if present, else direct call. The decorator returns an SdkMcpTool whose
    # .handler is the original async fn. Use that for direct invocation.
    handler = getattr(gen_mod.generate_photo, "handler", gen_mod.generate_photo)
    await handler({"mood": "focused"})

    ts = db.runtime_get("image_gen_last_failure_ts")
    assert ts is not None
    # Verify it parses as a recent ISO timestamp.
    parsed = datetime.fromisoformat(ts)
    assert parsed is not None


@pytest.mark.asyncio
async def test_generate_photo_failure_text_contains_image_gen_down(monkeypatch, tmp_path):
    """The text returned to the LLM must contain the ``image_gen_down`` token
    so the LLM knows to stay silent about image generation."""
    from tools.photos import generate as gen_mod

    db.upsert_core_block("mood_today", "focused")

    async def _fake_flux_none(prompt, model):
        return None

    monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux_none)

    handler = getattr(gen_mod.generate_photo, "handler", gen_mod.generate_photo)
    result = await handler({"mood": "focused"})

    # SDK tool result shape: {"content": [{"type": "text", "text": "..."}]}
    text = result["content"][0]["text"]
    assert "image_gen_down" in text
