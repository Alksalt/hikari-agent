"""Phase-E integrity tests.

Covers two surviving fixes after Phase 3-C (photo generation removed):
  3. Classifier cost logging — _log_aux_cost is called after a successful
     vision API response
  4. Snooze dedup — _sync_gcal_reminder and _sync_apple_reminder call delete
     before re-creating when an existing event id is present
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixture: isolated DB (used by Fix 3)
# ---------------------------------------------------------------------------

@pytest.fixture()
def _db_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")

    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config as _cfg
    from storage import db as _db
    _cfg.reload()

    return _db


# ===========================================================================
# Fix 3 — classifier cost logging
# ===========================================================================

class TestClassifierCostLogging:
    @pytest.mark.asyncio
    async def test_cost_logged_after_successful_classify(self, _db_env, monkeypatch, tmp_path):
        """_log_aux_cost is called with the usage tokens from the API response."""

        # Write a fake image file.
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        # Stub the vision API to return a known usage block.
        fake_body = {
            "content": [{"type": "text", "text": "intent: other\nconfidence: 0.9\ndetails: test"}],
            "usage": {"input_tokens": 500, "output_tokens": 50},
        }

        logged_calls = []

        def _fake_log_aux_cost(model, prompt_chars, completion_chars, path):
            logged_calls.append({
                "model": model,
                "prompt_chars": prompt_chars,
                "completion_chars": completion_chars,
                "path": path,
            })

        # Patch _call_vision_api to return the fake body tuple.
        async def _fake_call_vision(*args, **kwargs):
            return "intent: other\nconfidence: 0.9\ndetails: test", fake_body

        import tools.photos.classify as classify_mod
        monkeypatch.setattr(classify_mod, "_call_vision_api", _fake_call_vision)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

        # Patch agents.runtime._log_aux_cost.
        import agents.runtime as runtime_mod
        monkeypatch.setattr(runtime_mod, "_log_aux_cost", _fake_log_aux_cost)

        result = await classify_mod.classify_photo_intent(img_path)

        assert result["intent"] == "other"
        assert len(logged_calls) == 1, f"Expected 1 cost log call, got: {logged_calls}"
        call = logged_calls[0]
        assert call["model"] == "claude-sonnet-4-6"
        assert call["path"] == "photo_classify"
        assert call["prompt_chars"] == 500 * 4
        assert call["completion_chars"] == 50 * 4

    @pytest.mark.asyncio
    async def test_cost_log_failure_does_not_raise(self, _db_env, monkeypatch, tmp_path):
        """Even if _log_aux_cost raises, classify_photo_intent must not raise
        (never-raises contract)."""
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        fake_body = {
            "content": [{"type": "text", "text": "intent: selfie\nconfidence: 0.8\ndetails: face"}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

        async def _fake_call_vision(*args, **kwargs):
            return "intent: selfie\nconfidence: 0.8\ndetails: face", fake_body

        import tools.photos.classify as classify_mod
        monkeypatch.setattr(classify_mod, "_call_vision_api", _fake_call_vision)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

        import agents.runtime as runtime_mod

        def _exploding_log(*a, **kw):
            raise RuntimeError("cost log exploded")

        monkeypatch.setattr(runtime_mod, "_log_aux_cost", _exploding_log)

        # Must not raise.
        result = await classify_mod.classify_photo_intent(img_path)
        assert result["intent"] == "selfie"


# ===========================================================================
# Fix 4 — snooze dedup: GCal and Apple update instead of duplicate
# ===========================================================================

class TestGCalSnoozeDedup:
    @pytest.fixture()
    def _isolated(self, tmp_path, monkeypatch):
        db_path = tmp_path / "hikari.db"
        monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
        monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
        import storage.db as _db_mod
        importlib.reload(_db_mod)
        from agents import config
        config.reload()
        from storage import db as _db
        yield _db

    @pytest.mark.asyncio
    async def test_snooze_deletes_then_creates(self, _isolated):
        """When gcal_event_id already exists, _sync_gcal_reminder must call
        delete_calendar_event before create_calendar_event."""
        db = _isolated

        # Insert a reminder row with an existing gcal_event_id.
        rid = db.reminder_insert(
            text="Stand-up",
            fire_at="2026-06-01T09:00:00Z",
            gcal_sync_pending=True,
        )
        db.reminder_update_gcal_event(rid, "stale-gcal-id-abc")

        call_log: list[tuple[str, str]] = []

        async def _fake_manager_call(server, tool_name, args):
            call_log.append((server, tool_name))
            if tool_name == "create_calendar_event":
                return {"id": "new-gcal-event-xyz"}
            return {}

        from tools.reminders import sync_gcal
        with patch.object(sync_gcal.MANAGER, "call", side_effect=_fake_manager_call):
            result = await sync_gcal._sync_gcal_reminder(
                reminder_id=rid,
                title="Stand-up",
                start_iso="2026-06-01T10:00:00Z",
            )

        assert result.gcal_event_id == "new-gcal-event-xyz"
        tool_names = [t for _, t in call_log]
        assert "delete_calendar_event" in tool_names, f"delete not called: {call_log}"
        assert "create_calendar_event" in tool_names, f"create not called: {call_log}"
        # delete must precede create
        assert tool_names.index("delete_calendar_event") < tool_names.index(
            "create_calendar_event"
        ), "delete must happen before create"

    @pytest.mark.asyncio
    async def test_no_existing_gcal_id_skips_delete(self, _isolated):
        """When there is no prior gcal_event_id, delete must not be called."""
        db = _isolated

        rid = db.reminder_insert(
            text="New reminder",
            fire_at="2026-06-01T09:00:00Z",
            gcal_sync_pending=True,
        )

        call_log: list[tuple[str, str]] = []

        async def _fake_manager_call(server, tool_name, args):
            call_log.append((server, tool_name))
            if tool_name == "create_calendar_event":
                return {"id": "brand-new-event"}
            return {}

        from tools.reminders import sync_gcal
        with patch.object(sync_gcal.MANAGER, "call", side_effect=_fake_manager_call):
            result = await sync_gcal._sync_gcal_reminder(
                reminder_id=rid,
                title="New reminder",
                start_iso="2026-06-01T09:00:00Z",
            )

        assert result.gcal_event_id == "brand-new-event"
        tool_names = [t for _, t in call_log]
        assert "delete_calendar_event" not in tool_names, (
            f"delete should not be called for new reminder: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_delete_failure_does_not_abort_create(self, _isolated):
        """If the delete call raises, _sync_gcal_reminder logs a warning but
        still proceeds to create the new event."""
        db = _isolated

        rid = db.reminder_insert(
            text="Meeting",
            fire_at="2026-06-02T14:00:00Z",
            gcal_sync_pending=True,
        )
        db.reminder_update_gcal_event(rid, "stale-id-to-delete")

        from agents.mcp_manager import McpCallError

        async def _fake_manager_call(server, tool_name, args):
            if tool_name == "delete_calendar_event":
                raise McpCallError(server, tool_name, "not found")
            if tool_name == "create_calendar_event":
                return {"id": "recovered-event-id"}
            return {}

        from tools.reminders import sync_gcal
        with patch.object(sync_gcal.MANAGER, "call", side_effect=_fake_manager_call):
            result = await sync_gcal._sync_gcal_reminder(
                reminder_id=rid,
                title="Meeting",
                start_iso="2026-06-02T14:00:00Z",
            )

        assert result.gcal_event_id == "recovered-event-id"


class TestAppleSnoozeDedup:
    @pytest.fixture()
    def _isolated(self, tmp_path, monkeypatch):
        db_path = tmp_path / "hikari.db"
        monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
        monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
        import storage.db as _db_mod
        importlib.reload(_db_mod)
        from agents import config
        config.reload()
        from storage import db as _db
        yield _db

    @pytest.mark.asyncio
    async def test_snooze_deletes_then_creates(self, _isolated):
        """When apple_event_id already exists, _sync_apple_reminder must call
        delete_reminder before create_reminder."""
        db = _isolated

        rid = db.reminder_insert(
            text="Take medication",
            fire_at="2026-06-01T08:00:00Z",
        )
        db.reminder_update_apple_event(rid, "stale-apple-id-xyz")

        call_log: list[tuple[str, str]] = []

        async def _fake_manager_call(server, tool_name, args):
            call_log.append((server, tool_name))
            if tool_name == "create_reminder":
                return {"id": "new-apple-reminder-abc"}
            return {}

        from tools.reminders import sync_apple
        with patch.object(sync_apple.MANAGER, "call", side_effect=_fake_manager_call):
            result = await sync_apple._sync_apple_reminder(
                reminder_id=rid,
                title="Take medication",
                due_iso="2026-06-01T09:00:00Z",
            )

        assert result.apple_event_id == "new-apple-reminder-abc"
        tool_names = [t for _, t in call_log]
        assert "delete_reminder" in tool_names, f"delete not called: {call_log}"
        assert "create_reminder" in tool_names, f"create not called: {call_log}"
        assert tool_names.index("delete_reminder") < tool_names.index(
            "create_reminder"
        ), "delete must happen before create"

    @pytest.mark.asyncio
    async def test_no_existing_apple_id_skips_delete(self, _isolated):
        db = _isolated

        rid = db.reminder_insert(
            text="New reminder",
            fire_at="2026-06-01T09:00:00Z",
        )

        call_log: list[tuple[str, str]] = []

        async def _fake_manager_call(server, tool_name, args):
            call_log.append((server, tool_name))
            if tool_name == "create_reminder":
                return {"id": "fresh-apple-id"}
            return {}

        from tools.reminders import sync_apple
        with patch.object(sync_apple.MANAGER, "call", side_effect=_fake_manager_call):
            result = await sync_apple._sync_apple_reminder(
                reminder_id=rid,
                title="New reminder",
                due_iso="2026-06-01T09:00:00Z",
            )

        assert result.apple_event_id == "fresh-apple-id"
        tool_names = [t for _, t in call_log]
        assert "delete_reminder" not in tool_names, (
            f"delete should not be called for new reminder: {call_log}"
        )
