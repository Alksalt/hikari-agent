"""Phase-7 Stage B-1 tests: sticker scaffold (mirror of reactions tests).

Pool ships empty by default — exercise the empty-pool guard, the mood gate,
the cooldown gate, and the disabled flag.
"""

from __future__ import annotations

import importlib
from pathlib import Path

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
