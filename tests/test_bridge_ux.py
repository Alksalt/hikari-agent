"""Phase 8 — bridge UX fixes.

Covers:
  - TypingHeartbeat fires immediately on entry
  - TypingHeartbeat refreshes while the inner work runs
  - TypingHeartbeat cleans up its background task even when the inner block raises
  - _send_with_choreography skips the artificial delay when elapsed_real >= delay
  - Calendar health gate skips the job when unhealthy
"""

from __future__ import annotations

import asyncio
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


# ---------- TypingHeartbeat ----------

@pytest.mark.asyncio
async def test_typing_heartbeat_fires_immediately(monkeypatch):
    from agents import telegram_bridge

    sent: list = []
    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=lambda **kw: sent.append(kw)))
    monkeypatch.setattr(telegram_bridge, "_typing_refresh_sec", lambda: 10.0)

    async with telegram_bridge.TypingHeartbeat(bot, chat_id=99) as hb:
        # Within an immediate entry, the indicator must already have fired.
        assert len(sent) == 1
        assert sent[0]["chat_id"] == 99
        assert hb.elapsed >= 0


@pytest.mark.asyncio
async def test_typing_heartbeat_refreshes(monkeypatch):
    from agents import telegram_bridge

    sent: list = []
    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=lambda **kw: sent.append(kw)))
    monkeypatch.setattr(telegram_bridge, "_typing_refresh_sec", lambda: 0.05)

    async with telegram_bridge.TypingHeartbeat(bot, chat_id=99):
        await asyncio.sleep(0.18)  # 3+ refresh ticks
    # 1 immediate + at least 2 refreshes
    assert len(sent) >= 3


@pytest.mark.asyncio
async def test_typing_heartbeat_cleans_up_on_exception(monkeypatch):
    from agents import telegram_bridge

    bot = SimpleNamespace(send_chat_action=AsyncMock())
    monkeypatch.setattr(telegram_bridge, "_typing_refresh_sec", lambda: 0.05)

    with pytest.raises(RuntimeError):
        async with telegram_bridge.TypingHeartbeat(bot, chat_id=99) as hb:
            assert hb._task is not None
            raise RuntimeError("inner blew up")

    # Inner task should be done after exit.
    await asyncio.sleep(0)
    assert hb._task.done()


@pytest.mark.asyncio
async def test_typing_heartbeat_tolerates_bot_send_failure(monkeypatch):
    """If send_chat_action raises every time, the heartbeat must not bring
    down the surrounding handler."""
    from agents import telegram_bridge

    async def boom(**kw):
        raise RuntimeError("telegram down")
    bot = SimpleNamespace(send_chat_action=boom)
    monkeypatch.setattr(telegram_bridge, "_typing_refresh_sec", lambda: 0.05)

    async with telegram_bridge.TypingHeartbeat(bot, chat_id=99):
        await asyncio.sleep(0.08)
    # If we reached here, no exception propagated.


# ---------- _send_with_choreography elapsed-aware delay ----------

@pytest.mark.asyncio
async def test_choreography_skips_delay_when_elapsed_exceeds(monkeypatch):
    """If we've already burned the typing-delay budget in the agent path,
    don't sleep again before sending."""
    from agents import telegram_bridge

    monkeypatch.setattr(telegram_bridge, "compute_typing_delay", lambda txt, mood: 2.0)
    monkeypatch.setattr(telegram_bridge, "should_false_start", lambda txt: False)
    # Stub stickers + drift to no-ops.
    monkeypatch.setattr(telegram_bridge.stickers_mod, "_bump_outbound_counter", lambda: None)
    monkeypatch.setattr(telegram_bridge.stickers_mod, "maybe_send_sticker",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(telegram_bridge.drift_mod, "maybe_judge_and_log",
                        AsyncMock(return_value=None))

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    sent_msg = SimpleNamespace(message_id=42)
    message = SimpleNamespace(
        chat_id=99,
        reply_text=AsyncMock(return_value=sent_msg),
    )
    bot = SimpleNamespace(send_chat_action=AsyncMock())

    await telegram_bridge._send_with_choreography(
        bot, message, "short reply", elapsed_real=2.5,
    )
    # No real sleep should have occurred (elapsed beat the delay).
    assert all(s == 0 or s is None for s in sleeps) or sleeps == []


@pytest.mark.asyncio
async def test_choreography_sleeps_remaining_when_elapsed_short(monkeypatch):
    from agents import telegram_bridge

    monkeypatch.setattr(telegram_bridge, "compute_typing_delay", lambda txt, mood: 2.0)
    monkeypatch.setattr(telegram_bridge, "should_false_start", lambda txt: False)
    monkeypatch.setattr(telegram_bridge.stickers_mod, "_bump_outbound_counter", lambda: None)
    monkeypatch.setattr(telegram_bridge.stickers_mod, "maybe_send_sticker",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(telegram_bridge.drift_mod, "maybe_judge_and_log",
                        AsyncMock(return_value=None))

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    sent_msg = SimpleNamespace(message_id=42)
    message = SimpleNamespace(
        chat_id=99,
        reply_text=AsyncMock(return_value=sent_msg),
    )
    bot = SimpleNamespace(send_chat_action=AsyncMock())

    await telegram_bridge._send_with_choreography(
        bot, message, "short reply", elapsed_real=0.5,
    )
    # Should sleep for the remainder (~1.5s, give or take).
    assert any(0.5 < s <= 2.0 for s in sleeps), f"saw sleeps={sleeps!r}"


# ---------- calendar health gate ----------

def test_calendar_creds_healthy_explicit_disable(monkeypatch):
    from agents import scheduler as sched_mod

    db.runtime_set("calendar_heartbeat_healthy", "0")
    for _k in ("GOOGLE_WORKSPACE_CLIENT_ID", "GOOGLE_WORKSPACE_CLIENT_SECRET",
               "GOOGLE_WORKSPACE_REFRESH_TOKEN"):
        monkeypatch.delenv(_k, raising=False)
    assert sched_mod._calendar_creds_healthy() is False


def test_calendar_creds_healthy_explicit_enable(monkeypatch):
    from agents import scheduler as sched_mod

    db.runtime_set("calendar_heartbeat_healthy", "1")
    for _k in ("GOOGLE_WORKSPACE_CLIENT_ID", "GOOGLE_WORKSPACE_CLIENT_SECRET",
               "GOOGLE_WORKSPACE_REFRESH_TOKEN"):
        monkeypatch.delenv(_k, raising=False)
    assert sched_mod._calendar_creds_healthy() is True


def test_calendar_creds_healthy_falls_back_to_env(monkeypatch):
    """If no explicit flag, fall back to env-var presence."""
    from agents import scheduler as sched_mod

    db.runtime_set("calendar_heartbeat_healthy", None)
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "fake-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "fake-token")
    assert sched_mod._calendar_creds_healthy() is True

    for _k in ("GOOGLE_WORKSPACE_CLIENT_ID", "GOOGLE_WORKSPACE_CLIENT_SECRET",
               "GOOGLE_WORKSPACE_REFRESH_TOKEN"):
        monkeypatch.delenv(_k, raising=False)
    assert sched_mod._calendar_creds_healthy() is False


def test_build_scheduler_skips_calendar_job_when_unhealthy(monkeypatch):
    from agents import scheduler as sched_mod

    db.runtime_set("calendar_heartbeat_healthy", "0")
    for _k in ("GOOGLE_WORKSPACE_CLIENT_ID", "GOOGLE_WORKSPACE_CLIENT_SECRET",
               "GOOGLE_WORKSPACE_REFRESH_TOKEN"):
        monkeypatch.delenv(_k, raising=False)

    async def send_text(s):
        return None

    sched = sched_mod.build_scheduler(send_text)
    ids = {j.id for j in sched.get_jobs()}
    assert "calendar_heartbeat" not in ids
    # Other jobs still wired.
    assert "heartbeat" in ids
    assert "daily_reflection" in ids
    assert "memory_prune" in ids


def test_build_scheduler_includes_calendar_when_healthy(monkeypatch):
    from agents import scheduler as sched_mod

    db.runtime_set("calendar_heartbeat_healthy", "1")
    async def send_text(s):
        return None
    sched = sched_mod.build_scheduler(send_text)
    ids = {j.id for j in sched.get_jobs()}
    assert "calendar_heartbeat" in ids


# ---------- plain-text reaches respond() ----------

@pytest.mark.asyncio
async def test_plain_text_reaches_respond(monkeypatch):
    """Post-nonverbal-deletion invariant: every plain text user-turn that
    isn't politeness-refused or approval-resolved must reach respond().
    Pins that no probability gate creeps back in."""
    import datetime

    from telegram import Chat, Message, Update, User

    from agents import telegram_bridge

    # Build a minimal Update carrying a plain text message from the owner.
    owner = 12345
    user = User(id=owner, first_name="test", is_bot=False)
    chat = Chat(id=owner, type="private")
    message = Message(
        message_id=1,
        date=datetime.datetime.now(),
        chat=chat,
        from_user=user,
        text="what's the weather like",
    )
    update = Update(update_id=1, message=message)

    # Bot stub — TypingHeartbeat calls send_chat_action; _send_with_choreography
    # calls reply_text (patched below so it doesn't run).
    bot = SimpleNamespace(send_chat_action=AsyncMock())
    ctx = SimpleNamespace(bot=bot)

    respond_mock = AsyncMock(return_value="cloudy, probably")

    # Patch respond so we can assert it was awaited without running the SDK.
    monkeypatch.setattr(telegram_bridge, "respond", respond_mock)
    # Short-circuit the choreography (reply_text, stickers, drift) — not under test.
    monkeypatch.setattr(telegram_bridge, "_send_with_choreography", AsyncMock())
    # Approval resolver must return False (no pending approval).
    monkeypatch.setattr(
        telegram_bridge.approval_tools, "resolve_pending_approval",
        AsyncMock(return_value=False),
    )
    # Reactions fire-and-forget — stub out.
    monkeypatch.setattr(
        telegram_bridge.reactions_mod, "maybe_react", AsyncMock(return_value=None),
    )
    # Affect scan is sync — stub to no-op.
    monkeypatch.setattr(telegram_bridge.affect_mod, "scan_inbound", lambda _: None)

    await telegram_bridge.handle_message(update, ctx)

    respond_mock.assert_awaited_once()
    # The argument passed to respond must be the original message text
    # (or belief-frame-augmented, but must contain it).
    called_with = respond_mock.call_args[0][0]
    assert "weather" in called_with
