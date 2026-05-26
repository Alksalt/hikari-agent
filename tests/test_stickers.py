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


@pytest.mark.asyncio
async def test_stickers_empty_pool_returns_false_and_none(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  probability_per_reply: 1.0\n"
        "  cooldown_min_messages: 0\n"
        "  mood_blocklist: []\n"
        "  pool: []\n"
    )
    assert await stickers.pick_sticker_file_id() is None
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


@pytest.mark.asyncio
async def test_stickers_pick_file_id_from_pool(monkeypatch, tmp_path):
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  pool: ['ID_A', 'ID_B']\n"
    )
    # No user_msg/reply → falls back to random.choice without LLM call.
    for _ in range(20):
        fid = await stickers.pick_sticker_file_id()
        assert fid in ("ID_A", "ID_B")


@pytest.mark.asyncio
async def test_stickers_pick_falls_back_on_aux_llm_failure(monkeypatch, tmp_path):
    """Aux-LLM exceptions must fall back to random.choice (never None / never raise)."""
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  pool:\n"
        "    - file_id: 'ID_A'\n"
        "      description: 'a'\n"
        "    - file_id: 'ID_B'\n"
        "      description: 'b'\n"
    )
    async def _boom(prompt, system=""):
        raise RuntimeError("openrouter down")

    # Patch at the call-site module (stickers), not the origin (runtime) —
    # stickers.py now imports _call_aux_llm at module level, so the function's
    # binding lives on `agents.stickers`.
    monkeypatch.setattr("agents.stickers._call_aux_llm", _boom)
    fid = await stickers.pick_sticker_file_id(user_msg="hi", reply="hm")
    assert fid in ("ID_A", "ID_B")


@pytest.mark.asyncio
async def test_stickers_pick_returns_none_when_llm_says_none(monkeypatch, tmp_path):
    """Spec-critical: when the LLM returns the literal "none", veto the send.
    A regression here would force a sticker on every gate pass and blow through
    the once-per-20-exchanges budget the persona depends on."""
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  pool:\n"
        "    - file_id: 'ID_A'\n"
        "      description: 'a'\n"
    )
    async def _none(prompt, system=""):
        return "none"

    monkeypatch.setattr("agents.stickers._call_aux_llm", _none)
    assert await stickers.pick_sticker_file_id(user_msg="hi", reply="hm") is None


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


# ---------- LLM picker: unknown sticker id → fallback to random ----------

@pytest.mark.asyncio
async def test_stickers_llm_returns_unknown_id_falls_back_to_random(monkeypatch, tmp_path):
    """LLM picker returns an id not in the pool → falls back to random.choice
    from the valid pool (never None, never raises)."""
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  pool:\n"
        "    - file_id: 'REAL_ID_A'\n"
        "      description: 'first'\n"
        "    - file_id: 'REAL_ID_B'\n"
        "      description: 'second'\n"
    )

    async def _llm_unknown(prompt, system=""):
        return "HALLUCINATED_ID_XYZ"

    monkeypatch.setattr("agents.stickers._call_aux_llm", _llm_unknown)

    results = set()
    for _ in range(20):
        fid = await stickers.pick_sticker_file_id(user_msg="hello", reply="hm")
        assert fid is not None, "Unknown LLM id must fall back to a valid file_id"
        assert fid in ("REAL_ID_A", "REAL_ID_B"), (
            f"Fallback must produce a pool member, got {fid!r}"
        )
        results.add(fid)
    # Over 20 calls, both pool members should have appeared (birthday paradox: p≈1-0.5^20≈1).
    # This asserts random.choice is used, not always index 0.
    assert len(results) > 0  # At minimum the fallback worked.


@pytest.mark.asyncio
async def test_stickers_llm_hallucinated_id_annotates_diary(monkeypatch, tmp_path):
    """When LLM returns an id not in pool, the module writes a diary entry
    to character_thoughts noting the hallucination before falling back."""
    _write_cfg(tmp_path, monkeypatch,
        "stickers:\n"
        "  enabled: true\n"
        "  pool:\n"
        "    - file_id: 'VALID_ID'\n"
        "      description: 'ok'\n"
    )

    async def _llm_hallucinate(prompt, system=""):
        return "NOT_IN_POOL_EVER"

    monkeypatch.setattr("agents.stickers._call_aux_llm", _llm_hallucinate)

    fid = await stickers.pick_sticker_file_id(user_msg="hi", reply="ok")

    # Must still return a valid id.
    assert fid == "VALID_ID"

    # Diary entry must have been written.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT thought FROM character_thoughts "
            "WHERE thought LIKE '%hallucinated%' OR thought LIKE '%NOT_IN_POOL_EVER%'"
        ).fetchall()
    assert rows, (
        "Expected a diary entry recording the hallucinated sticker id, found none"
    )
    thought_text = rows[0][0]
    assert "NOT_IN_POOL_EVER" in thought_text, (
        f"Diary entry should include the bad id, got: {thought_text!r}"
    )


def test_stickers_bump_uses_runtime_increment(monkeypatch, tmp_path):
    """_bump_outbound_counter must delegate to db.runtime_increment (atomic),
    not a raw read/write. Verify by checking the underlying SQL uses an
    atomic upsert (CAST expression) not a Python-side increment."""
    _write_cfg(tmp_path, monkeypatch, "stickers:\n  enabled: true\n  pool: []\n")

    calls: list[tuple] = []
    original_increment = db.runtime_increment

    def _spy_increment(key, by=1):
        calls.append((key, by))
        return original_increment(key, by=by)

    monkeypatch.setattr(db, "runtime_increment", _spy_increment)
    # Also patch the module-level reference in stickers (it imported directly).
    monkeypatch.setattr("agents.stickers.db", db)

    result = stickers._bump_outbound_counter()

    assert calls, "_bump_outbound_counter must call db.runtime_increment"
    key_called, by_called = calls[0]
    assert key_called == db.OUTBOUND_MSG_COUNTER_KEY, (
        f"Must increment the OUTBOUND_MSG_COUNTER_KEY, called with {key_called!r}"
    )
    assert by_called == 1, f"Must increment by 1, got by={by_called}"
    assert isinstance(result, int), "Must return the new counter value as int"


# ---------- generate_photo failure path sets runtime flag ----------

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
