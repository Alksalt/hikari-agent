"""Tests for tools/photos/generate.py — mood gate, daily cap, counter ordering.

Covers:
  - Mood not 'weirdly good' + unprompted=True → refuses with in-voice line
  - Daily cap reached → refuses with in-voice line
  - Counter only bumps AFTER outbox insert success (not before)
  - Outbox insert failure → no counter bump, file cleaned up
  - Successful generation → counter bumps once
"""
from __future__ import annotations

import importlib
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture: isolated DB + photo outbox
# ---------------------------------------------------------------------------

@pytest.fixture()
def _photo_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    outbox = tmp_path / "photo_outbox"
    outbox.mkdir()

    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("HIKARI_PHOTO_OUTBOX", str(outbox))

    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db as _db
    monkeypatch.setattr(_db, "_DB_PATH", db_path)

    # Reload the photos module so OUTBOX picks up the new env.
    import tools.photos._shared as _shared_mod
    import tools.photos.generate as _gen_mod
    importlib.reload(_shared_mod)
    importlib.reload(_gen_mod)

    return _db, outbox, _gen_mod


# ---------------------------------------------------------------------------
# Helper: call the underlying async function (bypasses SDK @tool wrapper)
# ---------------------------------------------------------------------------

def _get_handler(gen_mod):
    handler = getattr(gen_mod.generate_photo, "handler", gen_mod.generate_photo)
    if not callable(handler):
        # Some SDK versions expose it differently
        handler = gen_mod.generate_photo
    return handler


# ---------------------------------------------------------------------------
# Tests: mood gate
# ---------------------------------------------------------------------------

class TestMoodGate:
    @pytest.mark.asyncio
    async def test_unprompted_focused_mood_refuses(self, _photo_env, monkeypatch):
        """unprompted=True + mood != 'weirdly good' → refused."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "focused")

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "focused", "unprompted": True})

        text = result["content"][0]["text"]
        assert text.startswith("refused:"), f"Expected refused, got: {text!r}"
        assert "weirdly good" in text or "unprompted" in text

    @pytest.mark.asyncio
    async def test_unprompted_tired_mood_refuses(self, _photo_env):
        """unprompted=True + tired → refused."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "tired")

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "tired", "unprompted": True})

        text = result["content"][0]["text"]
        assert text.startswith("refused:")

    @pytest.mark.asyncio
    async def test_unprompted_irritable_mood_refuses(self, _photo_env):
        """irritable always refuses (even user-requested per the general irritable gate)."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "irritable")

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "irritable", "unprompted": False})

        text = result["content"][0]["text"]
        assert text.startswith("refused:")

    @pytest.mark.asyncio
    async def test_user_requested_focused_does_not_refuse_on_mood(
        self, _photo_env, monkeypatch
    ):
        """User-requested (unprompted=False) focused mood passes the mood gate
        (would only fail on cap or flux)."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "focused")

        # Stub flux to return fake bytes so generation succeeds.
        async def _fake_flux(prompt, model):
            return b"PNG_FAKE_BYTES"

        monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux)

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "focused", "unprompted": False})

        text = result["content"][0]["text"]
        assert not text.startswith("refused:"), (
            f"User-requested photo with non-irritable mood should not be refused: {text!r}"
        )


# ---------------------------------------------------------------------------
# Tests: daily cap
# ---------------------------------------------------------------------------

class TestDailyCap:
    @pytest.mark.asyncio
    async def test_cap_reached_refuses(self, _photo_env, monkeypatch):
        """When daily cap (2) is already reached, refuse with in-voice line."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "weirdly good")

        # Simulate cap already reached.
        today = time.strftime("%Y-%m-%d")
        db.runtime_set("photos_sent_date", today)
        db.runtime_set("photos_sent_today", gen_mod.DAILY_CAP)  # = 2

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "weirdly good", "unprompted": False})

        text = result["content"][0]["text"]
        assert text.startswith("refused:"), f"Expected refused at cap, got: {text!r}"
        assert "cap" in text.lower() or "daily" in text.lower()

    @pytest.mark.asyncio
    async def test_one_below_cap_does_not_refuse(self, _photo_env, monkeypatch):
        """One photo already sent today → still allowed (cap is 2)."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "weirdly good")

        today = time.strftime("%Y-%m-%d")
        db.runtime_set("photos_sent_date", today)
        db.runtime_set("photos_sent_today", gen_mod.DAILY_CAP - 1)  # 1 sent

        async def _fake_flux(prompt, model):
            return b"PNG_FAKE_BYTES"

        monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux)

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "weirdly good", "unprompted": False})

        text = result["content"][0]["text"]
        assert not text.startswith("refused:"), (
            f"Should not refuse when count < cap, got: {text!r}"
        )


# ---------------------------------------------------------------------------
# Tests: counter ordering (bump AFTER outbox insert success)
# ---------------------------------------------------------------------------

class TestCounterOrdering:
    @pytest.mark.asyncio
    async def test_counter_not_bumped_on_outbox_insert_failure(
        self, _photo_env, monkeypatch
    ):
        """If media_outbox_insert raises, _record_photo_sent must NOT be called —
        the counter must stay at 0."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "weirdly good")

        async def _fake_flux(prompt, model):
            return b"PNG_FAKE_BYTES"

        monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux)

        # Make outbox insert fail.
        def _outbox_fail(*args, **kwargs):
            raise RuntimeError("db full")

        import storage.db as _db_mod
        monkeypatch.setattr(_db_mod, "media_outbox_insert", _outbox_fail)

        # Also patch the module-level reference inside generate.py if it cached it.
        monkeypatch.setattr(gen_mod.db, "media_outbox_insert", _outbox_fail)

        counter_before = db.runtime_get_int("photos_sent_today", 0)

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "weirdly good", "unprompted": False})

        counter_after = db.runtime_get_int("photos_sent_today", 0)
        text = result["content"][0]["text"]

        # Counter must not have changed.
        assert counter_after == counter_before, (
            f"Counter should not bump on insert failure — was {counter_before}, now {counter_after}"
        )
        # Result should signal failure.
        assert "image_gen_down" in text or text.startswith("refused:")

    @pytest.mark.asyncio
    async def test_counter_bumps_exactly_once_on_success(
        self, _photo_env, monkeypatch
    ):
        """Successful generation + insert → counter bumps exactly once."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "weirdly good")

        async def _fake_flux(prompt, model):
            return b"PNG_FAKE_BYTES"

        monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux)

        today = time.strftime("%Y-%m-%d")
        db.runtime_set("photos_sent_date", today)
        db.runtime_set("photos_sent_today", 0)

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "weirdly good", "unprompted": False})

        text = result["content"][0]["text"]
        assert not text.startswith("refused:"), f"Unexpected refusal: {text!r}"
        assert "image_gen_down" not in text, f"Unexpected flux failure: {text!r}"

        counter = db.runtime_get_int("photos_sent_today", 0)
        assert counter == 1, f"Counter should be 1 after one successful photo, got {counter}"

    @pytest.mark.asyncio
    async def test_counter_not_bumped_on_flux_failure(self, _photo_env, monkeypatch):
        """flux returns None (image gen down) → counter must NOT increase."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "weirdly good")

        async def _fake_flux_none(prompt, model):
            return None

        monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux_none)

        today = time.strftime("%Y-%m-%d")
        db.runtime_set("photos_sent_date", today)
        db.runtime_set("photos_sent_today", 0)

        handler = _get_handler(gen_mod)
        result = await handler({"mood": "weirdly good", "unprompted": False})

        text = result["content"][0]["text"]
        assert "image_gen_down" in text

        counter = db.runtime_get_int("photos_sent_today", 0)
        assert counter == 0, f"Counter must not bump on flux failure, got {counter}"

    @pytest.mark.asyncio
    async def test_orphan_file_removed_on_outbox_failure(
        self, _photo_env, monkeypatch
    ):
        """If outbox insert fails after writing the PNG, the orphan file is
        cleaned up — no stale files left in the outbox directory."""
        db, outbox, gen_mod = _photo_env
        db.upsert_core_block("mood_today", "weirdly good")

        async def _fake_flux(prompt, model):
            return b"PNG_FAKE_BYTES"

        monkeypatch.setattr(gen_mod, "_call_flux", _fake_flux)

        def _outbox_fail(*args, **kwargs):
            raise RuntimeError("db full")

        import storage.db as _db_mod
        monkeypatch.setattr(_db_mod, "media_outbox_insert", _outbox_fail)
        monkeypatch.setattr(gen_mod.db, "media_outbox_insert", _outbox_fail)

        handler = _get_handler(gen_mod)
        await handler({"mood": "weirdly good", "unprompted": False})

        leftover_pngs = list(outbox.glob("*.png"))
        assert not leftover_pngs, (
            f"Orphan PNG files should be cleaned up after outbox failure: {leftover_pngs}"
        )
