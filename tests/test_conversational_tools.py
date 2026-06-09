"""Phase 5a: conversational tool surface tests.

One file covering the 7 new in-process tools:
  1. reminder_list   (updated to include_done / local-tz output)
  2. link_search     (existing tool — happy path + empty result)
  3. receipt_read    (new unified period tool)
  4. diary_read      (new)
  5. set_silence     (new)
  6. set_proactive_source (new)
  7. checkin_control (new)

Each tool: >= 2 tests (happy path + edge).
set_silence / set_proactive_source also assert STATE PARITY:
  the tool writes exactly the runtime_state keys the command handlers read.
"""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh hikari.db for every test; reset link_shelf schema sentinel too."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()

    # link_shelf owns its own per-process schema sentinel
    from tools.link_shelf import db as shelf_db
    shelf_db._reset_schema_sentinel()

    # registry cache must be cleared so the new tool modules are picked up
    from tools._registry import clear_cache
    clear_cache()

    yield db_path


@pytest.fixture(autouse=True)
def _isolated_receipt_db(tmp_path: Path, monkeypatch):
    """Fresh day_receipt db for every test."""
    db_file = tmp_path / "test_receipt.db"
    monkeypatch.setenv("DAY_RECEIPT_DB", str(db_file))
    from tools.day_receipt import _db as receipt_db
    receipt_db._reset_schema_sentinel()
    yield db_file
    receipt_db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# 1. reminder_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reminder_list_happy_path():
    from tools.reminders import reminder_create, reminder_list

    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await reminder_create.handler({"when_iso": fire, "text": "brush teeth"})
    await reminder_create.handler({"when_iso": fire, "text": "take vitamins"})

    out = await reminder_list.handler({})
    body = out["content"][0]["text"]
    assert "brush teeth" in body
    assert "take vitamins" in body
    assert out["data"]["reminders"]


@pytest.mark.asyncio
async def test_reminder_list_empty():
    from tools.reminders import reminder_list

    out = await reminder_list.handler({})
    body = out["content"][0]["text"]
    assert "no" in body.lower()
    assert out["data"]["reminders"] == []


@pytest.mark.asyncio
async def test_reminder_list_include_done_shows_cancelled():
    from tools.reminders import reminder_cancel, reminder_create, reminder_list

    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await reminder_create.handler({"when_iso": fire, "text": "cancelled one"})
    rid = result["data"]["id"]
    await reminder_cancel.handler({"reminder_id": rid})

    # Default (include_done=False) should not show cancelled
    out_active = await reminder_list.handler({})
    assert out_active["data"]["reminders"] == []

    # include_done=True should include it
    out_all = await reminder_list.handler({"include_done": True})
    ids = [r["id"] for r in out_all["data"]["reminders"]]
    assert rid in ids


# ---------------------------------------------------------------------------
# 2. link_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_search_happy_path(monkeypatch):
    from tools.link_shelf import handlers, link_save, link_search

    async def _fake_fetch(url: str):
        return (handlers._url_to_title(url), None)
    monkeypatch.setattr(handlers, "_fetch_metadata", _fake_fetch)

    await link_save.handler({
        "url": "https://example.com/transformer-paper",
        "kind": "useful",
        "tags": ["ml", "attention"],
        "note": "the original paper",
    })

    out = await link_search.handler({"query": "transformer", "limit": 5})
    body = out["content"][0]["text"]
    assert "transformer" in body.lower() or "example.com" in body


@pytest.mark.asyncio
async def test_link_search_empty_result():
    from tools.link_shelf import link_search

    out = await link_search.handler({"query": "xyzzy_no_match"})
    body = out["content"][0]["text"]
    # empty result should say something sensible
    assert body  # not a crash


@pytest.mark.asyncio
async def test_link_search_no_query_returns_recent(monkeypatch):
    """Calling link_list with no query should list recent links."""
    from tools.link_shelf import handlers, link_list, link_save

    async def _fake_fetch(url: str):
        return (handlers._url_to_title(url), None)
    monkeypatch.setattr(handlers, "_fetch_metadata", _fake_fetch)

    await link_save.handler({
        "url": "https://arxiv.org/abs/2406.12345",
        "kind": "source",
    })

    out = await link_list.handler({"limit": 10})
    body = out["content"][0]["text"]
    assert "arxiv.org" in body


# ---------------------------------------------------------------------------
# 3. receipt_read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_read_today_happy_path():
    from tools.day_receipt import receipt_add, receipt_read

    await receipt_add.handler({"category": "made", "text": "shipped v2"})
    await receipt_add.handler({"category": "learned", "text": "read the paper"})

    out = await receipt_read.handler({"period": "today"})
    assert out["data"]["entries"]
    assert out["data"]["period"] == "today"
    cats = {e["category"] for e in out["data"]["entries"]}
    assert "made" in cats


@pytest.mark.asyncio
async def test_receipt_read_empty_today():
    from tools.day_receipt import receipt_read

    out = await receipt_read.handler({"period": "today"})
    body = out["content"][0]["text"]
    assert "nothing" in body.lower() or out["data"]["entries"] == []


@pytest.mark.asyncio
async def test_receipt_read_week():
    from tools.day_receipt import receipt_add, receipt_read

    await receipt_add.handler({"category": "moved", "text": "ran 5k"})

    out = await receipt_read.handler({"period": "week"})
    assert out["data"]["period"] == "week"
    assert isinstance(out["data"]["entries"], list)


@pytest.mark.asyncio
async def test_receipt_read_specific_date_empty():
    from tools.day_receipt import receipt_read

    out = await receipt_read.handler({"period": "2020-01-01"})
    body = out["content"][0]["text"]
    assert "nothing" in body.lower() or out["data"]["entries"] == []


@pytest.mark.asyncio
async def test_receipt_read_bad_period():
    from tools.day_receipt import receipt_read

    out = await receipt_read.handler({"period": "yesterday"})
    body = out["content"][0]["text"]
    assert "refused" in body.lower()


# ---------------------------------------------------------------------------
# 4. diary_read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diary_read_happy_path():
    from storage import db
    from tools.diary import diary_read

    db.diary_entry_upsert("2026-06-01", "a good day. shipped something.")
    db.diary_entry_upsert("2026-06-02", "focus mode. no distractions.")

    out = await diary_read.handler({"days": 7})
    body = out["content"][0]["text"]
    assert out["data"]["entries"]
    assert out["data"]["total"] >= 2
    assert "2026-06" in body


@pytest.mark.asyncio
async def test_diary_read_empty():
    from tools.diary import diary_read

    out = await diary_read.handler({})
    body = out["content"][0]["text"]
    assert "no diary" in body.lower()
    assert out["data"]["entries"] == []


@pytest.mark.asyncio
async def test_diary_read_pagination():
    from storage import db
    from tools.diary import diary_read

    for i in range(1, 12):
        db.diary_entry_upsert(f"2026-05-{i:02d}", f"entry {i}")

    out_p0 = await diary_read.handler({"page": 0})
    out_p1 = await diary_read.handler({"page": 1})

    # Both pages should have entries (11 entries, page_size=5)
    assert out_p0["data"]["entries"]
    assert out_p1["data"]["entries"]
    # Pages should be different
    dates_p0 = {e["entry_date"] for e in out_p0["data"]["entries"]}
    dates_p1 = {e["entry_date"] for e in out_p1["data"]["entries"]}
    assert not dates_p0.intersection(dates_p1)


# ---------------------------------------------------------------------------
# 5. set_silence — parity with /silence and /unsilence handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_silence_happy_path():
    from storage import db
    from tools.controls import set_silence

    out = await set_silence.handler({"minutes": 60})
    body = out["content"][0]["text"]
    assert "quiet" in body.lower() or "60" in body

    # PARITY: the same key the bridge's silence gate reads
    raw = db.runtime_get("silence_until")
    assert raw is not None
    until_dt = datetime.fromisoformat(raw)
    # Should be ~60 min from now
    delta = (until_dt - datetime.now(UTC)).total_seconds()
    assert 55 * 60 < delta < 65 * 60


@pytest.mark.asyncio
async def test_set_silence_off():
    from storage import db
    from tools.controls import set_silence

    # Set a silence first
    await set_silence.handler({"minutes": 30})
    assert db.runtime_get("silence_until") is not None

    # Clear it — parity with /unsilence which writes None
    out = await set_silence.handler({"off": True})
    body = out["content"][0]["text"]
    assert "on" in body.lower() or "back" in body.lower() or "clear" in body.lower()

    # PARITY: bridge's /unsilence writes runtime_set("silence_until", None)
    assert db.runtime_get("silence_until") is None


@pytest.mark.asyncio
async def test_set_silence_invalid_minutes():
    from tools.controls import set_silence

    out = await set_silence.handler({"minutes": 0})
    body = out["content"][0]["text"]
    assert "refused" in body.lower()


# ---------------------------------------------------------------------------
# 6. set_proactive_source — parity with /proactive on|off|snooze
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_proactive_source_off():
    from storage import db
    from tools.controls import set_proactive_source

    out = await set_proactive_source.handler({
        "source": "weather_mood_shift",
        "action": "off",
    })
    body = out["content"][0]["text"]
    assert "off" in body.lower() or "weather_mood_shift" in body

    # PARITY: bridge writes proactive_enabled_sources_override as JSON list
    raw = db.runtime_get("proactive_enabled_sources_override")
    assert raw is not None
    enabled = json.loads(raw)
    assert "weather_mood_shift" not in enabled


@pytest.mark.asyncio
async def test_set_proactive_source_on_then_off():
    from storage import db
    from tools.controls import set_proactive_source

    await set_proactive_source.handler({
        "source": "weather_mood_shift", "action": "on",
    })
    raw_on = db.runtime_get("proactive_enabled_sources_override")
    assert "weather_mood_shift" in json.loads(raw_on)

    await set_proactive_source.handler({
        "source": "weather_mood_shift", "action": "off",
    })
    raw_off = db.runtime_get("proactive_enabled_sources_override")
    assert "weather_mood_shift" not in json.loads(raw_off)


@pytest.mark.asyncio
async def test_set_proactive_source_snooze():
    from storage import db
    from tools.controls import set_proactive_source

    out = await set_proactive_source.handler({
        "source": "calendar_event_prep",
        "action": "snooze",
        "snooze_hours": 2.0,
    })
    body = out["content"][0]["text"]
    assert "snoozed" in body.lower() or "calendar_event_prep" in body

    # PARITY: bridge writes proactive_snooze_until as JSON dict {source: iso}
    raw = db.runtime_get("proactive_snooze_until")
    assert raw is not None
    snooze_map = json.loads(raw)
    assert "calendar_event_prep" in snooze_map
    until_dt = datetime.fromisoformat(snooze_map["calendar_event_prep"])
    delta = (until_dt - datetime.now(UTC)).total_seconds()
    assert 1.9 * 3600 < delta < 2.1 * 3600


@pytest.mark.asyncio
async def test_set_proactive_source_bad_source():
    from tools.controls import set_proactive_source

    out = await set_proactive_source.handler({
        "source": "definitely_not_a_real_producer",
        "action": "off",
    })
    body = out["content"][0]["text"]
    assert "refused" in body.lower()
    assert "unknown source" in body.lower() or "valid sources" in body.lower()


@pytest.mark.asyncio
async def test_set_proactive_source_status():
    from tools.controls import set_proactive_source

    out = await set_proactive_source.handler({"action": "status"})
    body = out["content"][0]["text"]
    # format_proactive_status returns info about enabled/disabled sources
    assert body  # should not be empty


@pytest.mark.asyncio
async def test_set_proactive_source_snooze_missing_hours():
    from tools.controls import set_proactive_source

    out = await set_proactive_source.handler({
        "source": "calendar_event_prep",
        "action": "snooze",
        # snooze_hours intentionally omitted
    })
    body = out["content"][0]["text"]
    assert "refused" in body.lower()


# ---------------------------------------------------------------------------
# 7. checkin_control
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkin_control_skip_tomorrow():
    from tools.controls import checkin_control

    out = await checkin_control.handler({"action": "skip_tomorrow"})
    body = out["content"][0]["text"]
    assert "skip" in body.lower() or out["data"]["action"] == "skip_tomorrow"
    assert out["data"]["skipped_date"]

    # Verify via daily_checkin that tomorrow is now in skip_dates
    from agents.daily_checkin import _load_schedule
    schedule = _load_schedule()
    assert out["data"]["skipped_date"] in [str(d) for d in schedule.get("skip_dates", [])]


@pytest.mark.asyncio
async def test_checkin_control_run_now_sets_force_flag():
    from storage import db
    from tools.controls import checkin_control
    from tools.controls.checkin import _FORCE_KEY

    # Pre-populate last_fired_date to simulate "already fired today"
    db.runtime_set("daily_checkin_last_fired_date", "2026-06-09")

    out = await checkin_control.handler({"action": "run_now"})
    body = out["content"][0]["text"]
    assert "queue" in body.lower() or "minute" in body.lower()
    assert out["data"]["queued"] is True

    # The dedup guard should be cleared
    assert db.runtime_get("daily_checkin_last_fired_date") is None
    # The force flag should be set
    assert db.runtime_get(_FORCE_KEY) == "1"


@pytest.mark.asyncio
async def test_checkin_control_force_flag_peeked_by_should_fire_now():
    """should_fire_now returns True when force flag is set but does NOT clear it
    (peek semantics — clearing happens only on successful send)."""
    from zoneinfo import ZoneInfo

    from agents.daily_checkin import should_fire_now
    from storage import db
    from tools.controls import checkin_control
    from tools.controls.checkin import _FORCE_KEY

    await checkin_control.handler({"action": "run_now"})
    assert db.runtime_get(_FORCE_KEY) == "1"

    tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    result = should_fire_now(now)
    assert result is True
    # Flag must still be set — peek semantics, NOT consumed here
    assert db.runtime_get(_FORCE_KEY) == "1"


@pytest.mark.asyncio
async def test_checkin_control_run_now_while_disabled_refuses():
    """run_now while daily_checkin is disabled returns a clear message and
    does NOT set the force flag (prevent a zombie flag firing on re-enable)."""
    # Patch config to report disabled
    from agents import config as _cfg
    from storage import db
    from tools.controls import checkin_control
    from tools.controls.checkin import _FORCE_KEY
    original = _cfg.get
    def _patched_get(key, default=None):
        if key == "daily_checkin.enabled":
            return False
        return original(key, default)
    import unittest.mock as _mock
    with _mock.patch.object(_cfg, "get", side_effect=_patched_get):
        out = await checkin_control.handler({"action": "run_now"})

    body = out["content"][0]["text"]
    assert "disabled" in body.lower()
    assert db.runtime_get(_FORCE_KEY) is None
    assert out["data"]["queued"] is False


@pytest.mark.asyncio
async def test_force_flag_survives_cadence_abort(monkeypatch):
    """Force flag stays set when cadence governor vetoes the run, so the next
    tick retries automatically."""
    from storage import db
    from tools.controls import checkin_control
    from tools.controls.checkin import _FORCE_KEY

    await checkin_control.handler({"action": "run_now"})
    assert db.runtime_get(_FORCE_KEY) == "1"

    # Veto every send via the cadence module
    import agents.cadence as _cadence
    monkeypatch.setattr(_cadence, "can_send", lambda *a, **kw: (False, "test veto"))

    from agents.daily_checkin import maybe_run_daily_checkin
    async def _no_send(text):
        return True
    result = await maybe_run_daily_checkin(_no_send)
    assert result is False
    # Flag must still be set — abort path leaves it for retry
    assert db.runtime_get(_FORCE_KEY) == "1"


@pytest.mark.asyncio
async def test_force_flag_cleared_on_successful_send(monkeypatch):
    """Force flag is cleared only after a successful send."""
    from storage import db
    from tools.controls import checkin_control
    from tools.controls.checkin import _FORCE_KEY

    await checkin_control.handler({"action": "run_now"})
    assert db.runtime_get(_FORCE_KEY) == "1"

    # Allow cadence
    import agents.cadence as _cadence
    monkeypatch.setattr(_cadence, "can_send", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(_cadence, "record_ceremony_sent", lambda *a, **kw: None)

    # Return a composed message without hitting the LLM
    import agents.daily_checkin as _dc
    async def _fake_compose():
        return "morning. check emails?"
    monkeypatch.setattr(_dc, "compose_checkin_question", _fake_compose)

    # Gate passes immediately — patch on the proactive_gate module (local import)
    import agents.proactive_gate as _pg
    from agents.proactive_gate import ReservationResult
    async def _fake_reserve_and_send(**kw):
        return ReservationResult("sent", None, None, 1, "morning. check emails?")
    monkeypatch.setattr(_pg, "reserve_and_send", _fake_reserve_and_send)

    async def _send(text):
        return True
    result = await _dc.maybe_run_daily_checkin(_send)
    assert result is True
    # Flag must now be cleared
    assert db.runtime_get(_FORCE_KEY) is None


@pytest.mark.asyncio
async def test_skip_tomorrow_uses_home_tz(monkeypatch):
    """skip_tomorrow writes a date computed in HOME_TZ, matching the timezone
    _is_skipped_today uses — so they always agree."""
    from datetime import datetime as _dt

    from tools.controls import checkin_control

    # Set HOME_TZ to Auckland (UTC+12/+13) so local tomorrow may differ from UTC
    monkeypatch.setenv("HOME_TZ", "Pacific/Auckland")
    # Force reload of the resolver's env read
    import importlib

    import agents.daily_checkin as _dc
    importlib.reload(_dc)

    out = await checkin_control.handler({"action": "skip_tomorrow"})
    skipped = out["data"]["skipped_date"]

    # Compute what we expect using the same resolver
    from datetime import timedelta
    expected = (
        _dt.now(_dc._resolve_local_tz()).date() + timedelta(days=1)
    ).isoformat()
    assert skipped == expected

    # Restore
    importlib.reload(_dc)


@pytest.mark.asyncio
async def test_proactive_source_off_last_source_warning():
    """Turning off the last enabled source appends the all-off warning."""
    # Enable only one source explicitly, then turn it off
    from agents.engagement.producers import ALL_PRODUCER_IDS
    from storage import db
    from tools.controls import set_proactive_source
    one_source = sorted(ALL_PRODUCER_IDS)[0]

    # Set the override to contain only one source
    import json as _json
    db.runtime_set("proactive_enabled_sources_override", _json.dumps([one_source]))

    out = await set_proactive_source.handler({
        "source": one_source,
        "action": "off",
    })
    body = out["content"][0]["text"]
    assert "last enabled source" in body.lower()
    assert "all proactive messages are now off" in body.lower()
    assert "action='on'" in body


@pytest.mark.asyncio
async def test_checkin_control_bad_action():
    from tools.controls import checkin_control

    out = await checkin_control.handler({"action": "explode"})
    body = out["content"][0]["text"]
    assert "refused" in body.lower()
