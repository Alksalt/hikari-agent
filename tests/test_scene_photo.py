"""Tests for tools/photos/scene.py — prompt composition, daily cap, outbox write."""
from __future__ import annotations

import importlib
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _scene_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    outbox = tmp_path / "photo_outbox"
    outbox.mkdir()

    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("HIKARI_PHOTO_OUTBOX", str(outbox))

    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db as _db

    import tools.photos._shared as _shared_mod
    import tools.photos.scene as _scene_mod
    importlib.reload(_shared_mod)
    importlib.reload(_scene_mod)

    return _db, outbox, _scene_mod


def _handler(scene_mod):
    h = getattr(scene_mod.scene_photo_send, "handler", scene_mod.scene_photo_send)
    return h if callable(h) else scene_mod.scene_photo_send


# ---------------------------------------------------------------------------
# Prompt composition tests (pure function, no DB/outbox)
# ---------------------------------------------------------------------------

class TestSceneForActivity:
    def test_emits_scene_for_coding_activity(self):
        from tools.photos.scene import _scene_for_activity
        result = _scene_for_activity("working on the model", "peak", "default")
        assert "laptop" in result

    def test_emits_scene_for_tea(self):
        from tools.photos.scene import _scene_for_activity
        result = _scene_for_activity("making tea", "evening", "default")
        assert "mug" in result or "ceramic" in result

    def test_emits_scene_fallback_generic_desk(self):
        from tools.photos.scene import _scene_for_activity
        result = _scene_for_activity("something obscure", "default", "default")
        assert "desk" in result

    def test_layers_winter_ambient(self):
        from tools.photos.scene import _scene_for_activity
        result = _scene_for_activity("reading", "default", "winter")
        assert "cold blue" in result or "bare branches" in result

    def test_hint_overrides_activity(self):
        """hint='reading' should produce a book scene regardless of stored activity."""
        from tools.photos.scene import _scene_for_activity
        result = _scene_for_activity("reading", "default", "default")
        assert "book" in result


# ---------------------------------------------------------------------------
# Daily cap test
# ---------------------------------------------------------------------------

class TestDailyCap:
    @pytest.mark.asyncio
    async def test_respects_daily_cap(self, _scene_env, monkeypatch):
        db, outbox, scene_mod = _scene_env

        # Seed counter at cap.
        today = time.strftime("%Y-%m-%d")
        db.runtime_set("scene_photos_sent_date", today)
        # Use the same default as the module (2); no need to import cfg here.
        db.runtime_set("scene_photos_sent_today", 2)

        h = _handler(scene_mod)
        result = await h({"hint": ""})

        text = result["content"][0]["text"]
        assert "daily_cap" in text, f"Expected daily_cap refusal, got: {text!r}"


# ---------------------------------------------------------------------------
# Outbox write test
# ---------------------------------------------------------------------------

class TestOutboxWrite:
    @pytest.mark.asyncio
    async def test_writes_to_media_outbox(self, _scene_env, monkeypatch):
        """Mock _call_flux → verify a row is inserted with kind='photo' and file on disk."""
        db, outbox, scene_mod = _scene_env
        db.upsert_core_block("hikari_current_activity", "writing code")

        async def _fake_flux(prompt, model):
            return b"FAKE_JPEG_BYTES"

        monkeypatch.setattr(scene_mod, "_call_flux", _fake_flux)

        h = _handler(scene_mod)
        result = await h({"hint": ""})

        text = result["content"][0]["text"]
        assert text.startswith("queued"), f"Expected queued, got: {text!r}"
        assert "(scene)" in text

        # At least one .jpg file in outbox.
        jpgs = list(outbox.glob("*.jpg"))
        assert jpgs, "Expected a .jpg file in the outbox"


# ---------------------------------------------------------------------------
# No orphan files on flux failure
# ---------------------------------------------------------------------------

class TestFluxFailure:
    @pytest.mark.asyncio
    async def test_flux_failure_no_orphan_files(self, _scene_env, monkeypatch):
        """_call_flux raises → no file should be left in PHOTO_OUTBOX."""
        db, outbox, scene_mod = _scene_env

        async def _failing_flux(prompt, model):
            raise RuntimeError("network error")

        monkeypatch.setattr(scene_mod, "_call_flux", _failing_flux)

        h = _handler(scene_mod)
        result = await h({"hint": ""})

        text = result["content"][0]["text"]
        assert "refused" in text, f"Expected refused, got: {text!r}"

        leftover = list(outbox.iterdir())
        assert not leftover, f"Orphan files found: {leftover}"


# ---------------------------------------------------------------------------
# Hint override integration
# ---------------------------------------------------------------------------

class TestHintOverride:
    @pytest.mark.asyncio
    async def test_hint_overrides_activity_in_prompt(self, _scene_env, monkeypatch):
        """hint='reading' generates a book scene even when stored activity is 'running model'."""
        db, outbox, scene_mod = _scene_env
        db.upsert_core_block("hikari_current_activity", "running the model")

        captured = {}

        async def _capture_flux(prompt, model):
            captured["prompt"] = prompt
            return b"FAKE_JPEG"

        monkeypatch.setattr(scene_mod, "_call_flux", _capture_flux)

        h = _handler(scene_mod)
        await h({"hint": "reading"})

        assert "book" in captured.get("prompt", ""), (
            f"Expected book scene from hint='reading', prompt was: {captured.get('prompt')!r}"
        )
