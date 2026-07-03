"""capability_offers table + helpers (Task 7) and offer engine (Task 8)."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import capability_offers
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def test_offer_roundtrip_and_outcomes():
    rid = db.capability_offer_insert(offer_id="day_receipt", telegram_message_id=111)
    assert rid > 0
    assert db.capability_offer_recent_outcomes("day_receipt") == ["shown"]
    db.capability_offer_mark_tapped(rid)
    assert db.capability_offer_recent_outcomes("day_receipt") == ["tapped"]


def test_stale_shown_rows_marked_ignored():
    db.capability_offer_insert(offer_id="a", telegram_message_id=None)
    db.capability_offer_insert(offer_id="b", telegram_message_id=None)
    n = db.capability_offer_mark_stale_ignored()
    assert n == 2
    assert db.capability_offer_recent_outcomes("a") == ["ignored"]


def test_today_count_and_last_shown():
    assert db.capability_offers_today_count() == 0
    db.capability_offer_insert(offer_id="a", telegram_message_id=None)
    assert db.capability_offers_today_count() == 1
    assert db.capability_offer_last_shown("a") is not None
    assert db.capability_offer_last_shown("never") is None


def test_tool_calls_aggregations():
    db.tool_calls_insert(tool_id="mcp__x__one", duration_ms=5, success=True,
                         error_class=None, output_size=10)
    used = db.tool_calls_used_since("2000-01-01T00:00:00+00:00")
    assert "mcp__x__one" in used
    last = db.tool_calls_last_used(["mcp__x__one", "mcp__x__never"])
    assert "mcp__x__one" in last and "mcp__x__never" not in last


def _seed_tool_call(tool_id: str, ago_days: float = 0.0) -> None:
    ts = (datetime.now(UTC) - timedelta(days=ago_days)).isoformat()
    with db._conn() as c:
        c.execute(
            "INSERT INTO tool_calls (tool_id, started_at, duration_ms, success, "
            "error_class, output_size) VALUES (?, ?, 1, 1, NULL, 1)",
            (tool_id, ts),
        )


def test_offer_catalog_domains_exist_in_tool_catalog():
    """A typo'd domain never matches and the offer silently never fires."""
    from tools.catalog import get_catalog
    live = {e.domain for e in get_catalog().entries}
    for entry in capability_offers._catalog():
        assert set(entry["domains"]) & live, f"offer {entry['id']!r} has no live domain"


def test_select_offer_matches_turn_domain_and_skips_used():
    # Turn used a gmail tool → gmail-adjacent offers are candidates.
    _seed_tool_call("mcp__hikari_utility__query_inbox")
    # jobhunt_radar's tool ran yesterday → already discovered → excluded.
    _seed_tool_call("mcp__hikari_utility__jobhunt_radar", ago_days=1)
    picked = capability_offers.select_offer(turn_elapsed_sec=10.0)
    assert picked is not None
    assert picked["id"] != "jobhunt_radar"
    assert "gmail" in picked["domains"] or "calendar" in picked["domains"]


def test_select_offer_respects_drop_after_ignored():
    _seed_tool_call("mcp__hikari_utility__query_inbox")
    for entry in capability_offers._catalog():
        for _ in range(2):
            db.capability_offer_insert(offer_id=entry["id"], telegram_message_id=None)
    db.capability_offer_mark_stale_ignored()  # all rows → 'ignored'
    assert capability_offers.select_offer(turn_elapsed_sec=10.0) is None


@pytest.mark.asyncio
async def test_maybe_offer_daily_cap():
    _seed_tool_call("mcp__hikari_utility__query_inbox")
    db.capability_offer_insert(offer_id="translate", telegram_message_id=None)  # today
    out = await capability_offers.maybe_offer(
        chat_id=1, turn_elapsed_sec=10.0, telegram_message_id=42
    )
    assert out is None  # max_per_day=1 already consumed


@pytest.mark.asyncio
async def test_maybe_offer_attaches_button_and_records(monkeypatch):
    _seed_tool_call("mcp__hikari_utility__query_inbox")
    attach = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "agents.telegram_bridge.attach_keyboard_to_sent_message", attach
    )
    out = await capability_offers.maybe_offer(
        chat_id=1, turn_elapsed_sec=10.0, telegram_message_id=42
    )
    assert out is not None
    attach.assert_awaited_once()
    kb = attach.await_args.args[1]
    assert kb.inline_keyboard[0][0].callback_data.startswith("offer:go:")
    assert db.capability_offer_recent_outcomes(out) == ["shown"]


def _seed_offer_row(offer_id: str, shown_ago_days: float, outcome: str) -> None:
    """Insert a capability_offers row with a backdated shown_at (the db helper
    always stamps now(), so history seeding goes through _conn directly)."""
    ts = (datetime.now(UTC) - timedelta(days=shown_ago_days)).isoformat()
    with db._conn() as c:
        c.execute(
            "INSERT INTO capability_offers "
            "(offer_id, shown_at, telegram_message_id, outcome) "
            "VALUES (?, ?, NULL, ?)",
            (offer_id, ts, outcome),
        )


def _isolate_translate_candidate() -> None:
    """Make 'translate' the only viable candidate: this turn used a gmail tool
    (query_inbox), and the other gmail-domain offers' tools ran recently so
    they count as already discovered (jobhunt_radar, reminders)."""
    _seed_tool_call("mcp__hikari_utility__query_inbox")
    _seed_tool_call("mcp__hikari_utility__jobhunt_radar", ago_days=2)
    _seed_tool_call("mcp__hikari_utility__reminder_create", ago_days=2)


def test_select_offer_min_gap_excludes_recently_shown():
    # translate shown 3 days ago (< min_days_between_same_offer=7) → excluded.
    # outcome 'tapped' so drop_after_ignored can't be the reason.
    _isolate_translate_candidate()
    _seed_offer_row("translate", shown_ago_days=3, outcome="tapped")
    assert capability_offers.select_offer(turn_elapsed_sec=10.0) is None


def test_select_offer_min_gap_allows_after_gap():
    # Same setup but shown 8 days ago (> 7-day gap) → eligible again.
    _isolate_translate_candidate()
    _seed_offer_row("translate", shown_ago_days=8, outcome="tapped")
    picked = capability_offers.select_offer(turn_elapsed_sec=10.0)
    assert picked is not None
    assert picked["id"] == "translate"


def test_select_offer_tap_resets_ignore_streak():
    # Outcomes newest-first: [ignored, tapped, ignored]. drop_after_ignored=2
    # requires the 2 MOST RECENT to be ignored — a tap in between resets the
    # streak, so the offer stays eligible. Rows backdated past the min-gap
    # window so the gap check doesn't interfere.
    _isolate_translate_candidate()
    _seed_offer_row("translate", shown_ago_days=10, outcome="ignored")
    _seed_offer_row("translate", shown_ago_days=9, outcome="tapped")
    _seed_offer_row("translate", shown_ago_days=8, outcome="ignored")
    picked = capability_offers.select_offer(turn_elapsed_sec=10.0)
    assert picked is not None
    assert picked["id"] == "translate"


def test_select_offer_unused_window_boundary_still_undiscovered():
    # translate's tool last ran 31 days ago — outside unused_window_days=30,
    # so the capability still counts as undiscovered → eligible.
    _isolate_translate_candidate()
    _seed_tool_call("mcp__hikari_utility__translate", ago_days=31)
    picked = capability_offers.select_offer(turn_elapsed_sec=10.0)
    assert picked is not None
    assert picked["id"] == "translate"


def test_select_offer_recent_tool_use_marks_discovered():
    # translate's tool ran 2 days ago — inside the 30-day window → already
    # discovered → excluded. No other candidates remain → None.
    _isolate_translate_candidate()
    _seed_tool_call("mcp__hikari_utility__translate", ago_days=2)
    assert capability_offers.select_offer(turn_elapsed_sec=10.0) is None


@pytest.mark.asyncio
async def test_cb_offer_marks_tapped_and_runs_phrase(monkeypatch):
    from agents import telegram_bridge

    rid = db.capability_offer_insert(offer_id="translate", telegram_message_id=7)
    respond = AsyncMock(return_value="done.")
    send = AsyncMock()
    monkeypatch.setattr(telegram_bridge, "respond", respond)
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", send)
    await telegram_bridge._cb_offer(AsyncMock(), 1, rid, "translate")
    assert db.capability_offer_recent_outcomes("translate") == ["tapped"]
    respond.assert_awaited_once()
    send.assert_awaited_once()
