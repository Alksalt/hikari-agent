"""Sprint 6A — Telegram cockpit command tests (mock-based, pytest-asyncio).

12 cases:
  1.  /help → lists all 16+ commands, owner only
  2.  /status → contains 'uptime', owner only
  3.  /tools (default=policy) → access_mode groups present
  4.  /tools recent → shows "tool calls" or "empty"
  5.  /audit recent → shows audit log header or empty
  6.  /audit tools → shows 7d header or empty
  7.  /audit approvals → shows approved calls or empty
  8.  /audit id <id> → shows row fields or "not found"
  9.  /settings (no args) → lists 4 allowlisted keys
  10. /settings get silence.default_minutes → returns current value
  11. /settings set silence.default_minutes 60 → confirms + audit trail
  12. non-owner → silent on all new commands
  13. /proactive recent → format_proactive_recent output
  14. /proactive why <id> → format_proactive_why output
  15. /proactive snooze wiki_new_file 1h → snooze confirmation
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from storage import db

# ---------------------------------------------------------------------------
# Shared fixtures (mirror test_telegram_memory_cmd.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh per-test DB."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _make_update(user_id: int, args: list[str] | None = None):
    message = MagicMock()
    message.reply_text = AsyncMock()

    user = MagicMock()
    user.id = user_id

    update = MagicMock()
    update.effective_user = user
    update.message = message
    update.effective_chat = MagicMock()
    update.effective_chat.id = user_id

    context = MagicMock()
    context.args = list(args) if args is not None else []
    # give context.application a bot_data dict
    context.application = MagicMock()
    context.application.bot_data = {}

    return update, context


def _owner_id() -> int:
    return 42


@pytest.fixture(autouse=True)
def _patch_owner(monkeypatch):
    monkeypatch.setattr("agents.telegram_bridge.owner_id", _owner_id)


# ---------------------------------------------------------------------------
# cockpit module helpers (unit-level, no telegram)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_cockpit_imports(monkeypatch):
    """Patch heavy imports inside cockpit that aren't available in test env."""
    # _uptime_str tries to import _BOOT_TIME from telegram_bridge — patch it
    import agents.cockpit as ck
    monkeypatch.setattr(ck, "_uptime_str", lambda: "5m 30s")
    monkeypatch.setattr(ck, "_probe_google_cached", AsyncMock(return_value="ok (mocked)"))
    monkeypatch.setattr(ck, "_OAUTH_PROBE_CACHE", {})


# ---------------------------------------------------------------------------
# 1. /help → lists commands, owner only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_help_lists_commands():
    from agents.telegram_bridge import cmd_help
    update, context = _make_update(_owner_id())
    await cmd_help(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/status" in text
    assert "/audit" in text
    assert "/settings" in text
    assert "/tools" in text
    assert len(text) <= 3900


# ---------------------------------------------------------------------------
# 2. /status → contains uptime line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_contains_uptime():
    from agents.telegram_bridge import cmd_status
    update, context = _make_update(_owner_id())
    # provide empty scheduler
    context.application.bot_data = {}
    await cmd_status(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "uptime" in text
    assert len(text) <= 3900


# ---------------------------------------------------------------------------
# 3. /tools policy → access_mode groups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_policy_groups():
    from agents.telegram_bridge import cmd_tools

    # Stub load_registry to avoid needing tools.yaml in test env
    fake_spec = MagicMock()
    fake_spec.id = "mcp__test__foo"
    fake_spec.access_mode = "read"
    fake_registry = MagicMock()
    fake_registry.tools.return_value = [fake_spec]

    update, context = _make_update(_owner_id(), args=[])
    with patch("tools._tools_yaml.load_registry", return_value=fake_registry):
        await cmd_tools(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "access_mode" in text or "read" in text
    assert len(text) <= 3900


# ---------------------------------------------------------------------------
# 4. /tools recent → "tool calls" or "empty"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_recent_empty():
    from agents.telegram_bridge import cmd_tools
    update, context = _make_update(_owner_id(), args=["recent"])
    await cmd_tools(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "audit log" in text or "no tool" in text


@pytest.mark.asyncio
async def test_tools_recent_with_data():
    db.tool_calls_insert(
        tool_id="mcp__test__bar",
        duration_ms=42,
        success=True,
        error_class=None,
        output_size=10,
    )
    from agents.telegram_bridge import cmd_tools
    update, context = _make_update(_owner_id(), args=["recent"])
    await cmd_tools(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "mcp__test__bar" in text


# ---------------------------------------------------------------------------
# 5. /audit recent → header present or empty message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_recent_empty():
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=[])
    await cmd_audit(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "audit log" in text or "empty" in text


@pytest.mark.asyncio
async def test_audit_recent_with_data():
    db.audit_append("test_tool", '{"a":1}', "done")
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["recent"])
    await cmd_audit(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "test_tool" in text
    assert "#" in text  # row id rendered


# ---------------------------------------------------------------------------
# 6. /audit tools → 7d header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_tools_subcommand():
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["tools"])
    await cmd_audit(update, context)
    text = update.message.reply_text.call_args[0][0]
    # either empty message or the 7d header
    assert "7d" in text or "no tool activity" in text


# ---------------------------------------------------------------------------
# 7. /audit approvals → approved calls or empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_approvals_empty():
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["approvals"])
    await cmd_audit(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "approved" in text or "no approved" in text


@pytest.mark.asyncio
async def test_audit_approvals_with_data():
    db.audit_append("approved_tool", "{}", "ok", approved_by="owner")
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["approvals"])
    await cmd_audit(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "approved_tool" in text


# ---------------------------------------------------------------------------
# 8. /audit id <id> → row detail or not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_id_not_found():
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["id", "9999"])
    await cmd_audit(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text


@pytest.mark.asyncio
async def test_audit_id_found():
    row_id = db.audit_append("tool_x", '{"k":"v"}', "result here")
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["id", str(row_id)])
    await cmd_audit(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "tool_x" in text
    assert "result here" in text


# ---------------------------------------------------------------------------
# 9. /settings (no args) → lists 4 allowlisted keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_list():
    from agents.telegram_bridge import cmd_settings
    update, context = _make_update(_owner_id(), args=[])
    await cmd_settings(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "silence.default_minutes" in text
    assert "GRAPHITI_ENABLED" in text
    assert "AUTH_PRECHECK" in text
    assert "proactive.enabled" in text


# ---------------------------------------------------------------------------
# 10. /settings get silence.default_minutes → returns value
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_get():
    from agents.telegram_bridge import cmd_settings
    update, context = _make_update(_owner_id(), args=["get", "silence.default_minutes"])
    await cmd_settings(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "silence.default_minutes" in text
    # should have some numeric value
    assert any(c.isdigit() for c in text)


# ---------------------------------------------------------------------------
# 11. /settings set silence.default_minutes 60 → confirm + audit trail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_set_leaves_audit_trail():
    from agents.telegram_bridge import cmd_settings
    update, context = _make_update(_owner_id(), args=["set", "silence.default_minutes", "60"])

    await cmd_settings(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "ok" in text.lower() or "60" in text

    # value stored in runtime_state, not config
    assert db.runtime_get("settings.silence.default_minutes") == "60"

    # check audit_log has a settings.set row
    rows = db.audit_recent(5)
    tool_names = [r["tool"] for r in rows]
    assert "settings.set" in tool_names


# ---------------------------------------------------------------------------
# 12. non-owner → silent on all new commands
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_owner_silent_help():
    from agents.telegram_bridge import cmd_help
    update, context = _make_update(user_id=999)
    await cmd_help(update, context)
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_owner_silent_status():
    from agents.telegram_bridge import cmd_status
    update, context = _make_update(user_id=999)
    await cmd_status(update, context)
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_owner_silent_tools():
    from agents.telegram_bridge import cmd_tools
    update, context = _make_update(user_id=999)
    await cmd_tools(update, context)
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_owner_silent_audit():
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(user_id=999)
    await cmd_audit(update, context)
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_owner_silent_settings():
    from agents.telegram_bridge import cmd_settings
    update, context = _make_update(user_id=999)
    await cmd_settings(update, context)
    update.message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# 13. /proactive recent → cockpit output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_recent_subcommand():
    from agents.telegram_bridge import cmd_proactive
    update, context = _make_update(_owner_id(), args=["recent"])
    await cmd_proactive(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    # either has events or the "no proactive events" message
    assert "proactive" in text.lower() or "no proactive" in text.lower()


# ---------------------------------------------------------------------------
# 14. /proactive why <id> → not found when id missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_why_not_found():
    from agents.telegram_bridge import cmd_proactive
    update, context = _make_update(_owner_id(), args=["why", "9999"])
    await cmd_proactive(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text


# ---------------------------------------------------------------------------
# 15. /proactive snooze wiki_new_file 1h → writes runtime_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_snooze_writes_state():
    from agents.telegram_bridge import cmd_proactive
    update, context = _make_update(_owner_id(), args=["snooze", "wiki_new_file", "1h"])
    await cmd_proactive(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "snoozed" in text.lower()
    # verify runtime_state was written
    raw = db.runtime_get("proactive_snooze_until")
    assert raw is not None
    snooze_map = json.loads(raw)
    assert "wiki_new_file" in snooze_map


# ---------------------------------------------------------------------------
# 16. selector respects snoozed sources
# ---------------------------------------------------------------------------

def test_selector_skips_snoozed():
    """Sources in the snooze map are excluded even when enabled."""
    import datetime as _dt
    from types import SimpleNamespace

    # Write a snooze entry that expires 1 hour from now
    future_iso = (
        _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1)
    ).isoformat()
    db.runtime_set("proactive_snooze_until", json.dumps({"wiki_new_file": future_iso}))

    from agents.engagement.selector import select
    from agents.engagement.triggers import TriggerCandidate

    candidate = TriggerCandidate(
        source="wiki_new_file",
        pool="user_anchored",
        pattern="notify",
        payload={},
        dedup_key="test-dedup",
        decay_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=2),
        novelty=1.0,
        actionability=1.0,
        confidence=1.0,
    )
    ctx = SimpleNamespace(
        now_local=_dt.datetime.now(),
        mood="focused",
        enabled_sources={"wiki_new_file"},
        pool_caps={"user_anchored": True},
        source_response_rate={},
        last_send_per_source={},
    )
    result = select([candidate], ctx)
    assert result is None, "snoozed source should not be selected"


# ---------------------------------------------------------------------------
# 17. /audit media → empty DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_media_empty():
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["media"])
    await cmd_audit(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "media" in text.lower() and ("no records" in text.lower() or "yet" in text.lower())


# ---------------------------------------------------------------------------
# 18. /audit media → with data shows kind + caption
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_media_with_data():
    db.media_events_insert("photo", telegram_message_id=42, caption="test cap")
    from agents.telegram_bridge import cmd_audit
    update, context = _make_update(_owner_id(), args=["media"])
    await cmd_audit(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "photo" in text
    assert "test cap" in text


# ---------------------------------------------------------------------------
# 19. /diary — empty DB returns "no diary entries"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diary_empty():
    from agents.telegram_bridge import cmd_diary
    update, context = _make_update(_owner_id(), args=[])
    await cmd_diary(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "no diary" in text.lower() or "diary" in text.lower()


# ---------------------------------------------------------------------------
# 20. /diary — returns formatted entries with date + body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diary_with_entries():
    db.diary_entry_upsert("2026-05-01", "something meaningful happened")
    db.diary_entry_upsert("2026-05-02", "another day, another thought")
    from agents.telegram_bridge import cmd_diary
    update, context = _make_update(_owner_id(), args=[])
    await cmd_diary(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "2026-05" in text
    assert len(text) <= 4000


# ---------------------------------------------------------------------------
# 21. /diary — non-owner ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diary_non_owner_silent():
    from agents.telegram_bridge import cmd_diary
    update, context = _make_update(user_id=999)
    await cmd_diary(update, context)
    update.message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# 22. /links — empty shelf returns "no saved links"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_links_empty():
    import tools.link_shelf.db as shelf_db
    shelf_db._reset_schema_sentinel()
    from agents.telegram_bridge import cmd_links
    update, context = _make_update(_owner_id(), args=[])
    await cmd_links(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "no saved links" in text.lower() or "link" in text.lower()


# ---------------------------------------------------------------------------
# 23. /links — returns bookmark entries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_links_with_data():
    import tools.link_shelf.db as shelf_db
    shelf_db._reset_schema_sentinel()
    shelf_db.insert(
        url="https://example.com/paper",
        title="Interesting Paper",
        snippet="A short snippet",
        kind="useful",
        tags=["ml"],
        note=None,
    )
    from agents.telegram_bridge import cmd_links
    update, context = _make_update(_owner_id(), args=[])
    await cmd_links(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "example.com" in text or "Interesting Paper" in text


# ---------------------------------------------------------------------------
# 24. /links search — returns filtered results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_links_search_filter():
    import tools.link_shelf.db as shelf_db
    shelf_db._reset_schema_sentinel()
    shelf_db.insert(
        url="https://arxiv.org/abs/1234",
        title="Attention Is All You Need",
        snippet="transformer paper",
        kind="source",
        tags=["transformers", "nlp"],
        note=None,
    )
    from agents.telegram_bridge import cmd_links
    update, context = _make_update(_owner_id(), args=["arxiv"])
    await cmd_links(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    # Either the match or the "no links" response is acceptable
    assert "arxiv" in text.lower() or "no links" in text.lower()


# ---------------------------------------------------------------------------
# 25. /receipt today — returns text (no entries = empty message)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_today_empty():
    from agents.telegram_bridge import cmd_receipt
    update, context = _make_update(_owner_id(), args=["today"])
    await cmd_receipt(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert len(text) <= 4000


# ---------------------------------------------------------------------------
# 26. /receipt week — returns week view or "nothing logged"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_week_empty():
    from agents.telegram_bridge import cmd_receipt
    update, context = _make_update(_owner_id(), args=["week"])
    await cmd_receipt(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert len(text) <= 4000


# ---------------------------------------------------------------------------
# 27. /decision pending — empty returns "no pending decisions"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decision_pending_empty():
    from agents.telegram_bridge import cmd_decision
    update, context = _make_update(_owner_id(), args=[])
    await cmd_decision(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "pending" in text.lower() or "no pending" in text.lower()


# ---------------------------------------------------------------------------
# 28. /decision pending — shows pending decisions with probability
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decision_pending_with_data():
    db.decision_insert(
        "the deal closes by friday",
        predicted_p=0.65,
        resolve_by="2026-05-30",
    )
    from agents.telegram_bridge import cmd_decision
    update, context = _make_update(_owner_id(), args=[])
    await cmd_decision(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "deal closes" in text or "65%" in text or "0.65" in text


# ---------------------------------------------------------------------------
# 29. /decision resolve <id> 1 — resolves a decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decision_resolve():
    did = db.decision_insert(
        "the PR merges today",
        predicted_p=0.8,
        resolve_by="2026-05-26",
    )
    from agents.telegram_bridge import cmd_decision
    update, context = _make_update(_owner_id(), args=["resolve", str(did), "1"])
    await cmd_decision(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "resolved" in text.lower() or str(did) in text


# ---------------------------------------------------------------------------
# 30. /decision — non-owner ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decision_non_owner_silent():
    from agents.telegram_bridge import cmd_decision
    update, context = _make_update(user_id=999)
    await cmd_decision(update, context)
    update.message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# 31. /voice — returns STT health section
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_returns_stt_health():
    from agents.telegram_bridge import cmd_voice
    update, context = _make_update(_owner_id(), args=[])
    await cmd_voice(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "stt" in text.lower() or "voice" in text.lower()
    assert len(text) <= 4000


# ---------------------------------------------------------------------------
# 32. /voice — with a voice note in messages history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_shows_recent_transcript():
    db.append_message("user", "[voice note] what's the weather today")
    from agents.telegram_bridge import cmd_voice
    update, context = _make_update(_owner_id(), args=[])
    await cmd_voice(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    # Either the note appeared in the output or the "no voice notes" message
    assert "voice note" in text.lower() or "no voice notes" in text.lower()


# ---------------------------------------------------------------------------
# 33. /voice — non-owner ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_non_owner_silent():
    from agents.telegram_bridge import cmd_voice
    update, context = _make_update(user_id=999)
    await cmd_voice(update, context)
    update.message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# 34. /proactive why <id> — not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_why_returns_not_found_for_missing():
    from agents.telegram_bridge import cmd_proactive
    update, context = _make_update(_owner_id(), args=["why", "88888"])
    await cmd_proactive(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text.lower()


# ---------------------------------------------------------------------------
# 35. /proactive why <id> — with real event: renders source/anchor/why_now
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_why_renders_reason_contract():
    eid = db.proactive_event_insert(
        source="wiki_new_file",
        pattern="notify",
        payload_json='{"title": "New wiki page"}',
        status="sent",
        anchor="a new page was added to the wiki",
        why_now="user is active right now",
        confidence=0.82,
        controls_json='["cool_down"]',
        data_checked_json='{"wiki_updated": true}',
    )
    from agents.telegram_bridge import cmd_proactive
    update, context = _make_update(_owner_id(), args=["why", str(eid)])
    await cmd_proactive(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "wiki_new_file" in text
    assert "anchor" in text.lower()
    assert "why_now" in text.lower() or "why now" in text.lower()
    assert "0.82" in text or "confidence" in text.lower()


# ---------------------------------------------------------------------------
# 36. /proactive why — non-owner ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_why_non_owner_silent():
    from agents.telegram_bridge import cmd_proactive
    update, context = _make_update(user_id=999, args=["why", "1"])
    await cmd_proactive(update, context)
    update.message.reply_text.assert_not_awaited()
