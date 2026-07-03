"""Gap awareness — inject a # gap_since_last: line into inject_memory
when the user has been quiet ≥2h. Three bands: <2h invisible,
2h-24h soft, >24h strong (triggers the existing 'you went quiet'
voice line).

The gap is computed from runtime_state.last_user_message vs now."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import hooks


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def test_gap_under_2h_returns_empty():
    """<2h elapsed → no gap line (she's mid-conversation)."""
    now = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    last = now - timedelta(hours=1, minutes=30)
    out = hooks._format_gap_since_last(_iso(last), now=now)
    assert out == ""


def test_gap_soft_band_2h_to_24h():
    """2h-24h elapsed → soft '# gap_since_last: 4h' line."""
    now = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    last = now - timedelta(hours=4)
    out = hooks._format_gap_since_last(_iso(last), now=now)
    assert "# gap_since_last:" in out
    assert "4h" in out
    assert "long quiet" not in out  # soft band, no strong-signal text


def test_gap_long_band_over_24h():
    """>24h → strong line with the explicit voice-line directive."""
    now = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    last = now - timedelta(days=2, hours=3)
    out = hooks._format_gap_since_last(_iso(last), now=now)
    assert "# gap_since_last:" in out
    assert "2d" in out
    assert "you went quiet" in out  # references the voice line


def test_gap_unparseable_ts_returns_empty():
    """Garbage in runtime_state → no injection, no crash."""
    out = hooks._format_gap_since_last("not-a-timestamp")
    assert out == ""


def test_gap_missing_ts_returns_empty():
    """No runtime_state row → no injection."""
    out = hooks._format_gap_since_last(None)
    assert out == ""


@pytest.fixture
def _isolated_db(tmp_path: Path, monkeypatch):
    """Per-test fresh DB. Mirrors tests/test_facts_recall_decay.py:23-39."""
    from storage import db as _db
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    importlib.reload(_db)
    monkeypatch.setattr(_db, "_DB_PATH", db_path)
    _db._reset_schema_sentinel()
    yield _db


def test_inject_memory_emits_gap_block_when_long_quiet(_isolated_db):
    """End-to-end: stale last_user_message in runtime_state → inject_memory
    output contains the gap_since_last line."""
    import asyncio
    db = _isolated_db
    stale = datetime.now(UTC) - timedelta(days=3)
    db.runtime_set("last_user_message", _iso(stale))

    out = asyncio.run(hooks.inject_memory({"prompt": "test"}, None, None))
    text = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "# gap_since_last:" in text
    assert "3d" in text


def test_inject_memory_omits_gap_block_when_fresh(_isolated_db):
    """End-to-end: fresh last_user_message → no gap_since_last line."""
    import asyncio
    db = _isolated_db
    fresh = datetime.now(UTC) - timedelta(minutes=15)
    db.runtime_set("last_user_message", _iso(fresh))
    out = asyncio.run(hooks.inject_memory({"prompt": "test"}, None, None))
    text = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "# gap_since_last:" not in text


def test_respond_does_not_pre_write_last_user_message(_isolated_db):
    """FIX 2: respond() must NOT pre-write last_user_message — only the
    inject_memory hook stamps it (read-then-write). A pre-turn write made the
    hook read ~now, killing the gap signal on every interactive turn.

    With run_user_turn stubbed (no hook fires), the sentinel must survive.
    """
    import asyncio
    from unittest.mock import patch

    db = _isolated_db
    sentinel = _iso(datetime(2020, 1, 1, tzinfo=UTC))
    db.runtime_set("last_user_message", sentinel)

    async def _fake_run_user_turn(prompt: str) -> str:
        return ""

    from agents import runtime
    with patch.object(runtime, "run_user_turn", side_effect=_fake_run_user_turn):
        asyncio.run(runtime.respond("hello"))

    assert db.runtime_get("last_user_message") == sentinel, (
        "respond() must not touch last_user_message; the hook is the sole writer"
    )
