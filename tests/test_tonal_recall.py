"""tests/test_tonal_recall.py — unit tests for agents/tonal_recall.py.

Test matrix:
  1. Mocked aux LLM returns 'warm' → sessions.emotional_register updated to 'warm'
  2. LLM returns unexpected token → falls back to 'neutral'
  3. No messages for session → returns 'neutral', no DB write
  4. DB UPDATE failure → re-raises (Wave 1 fix)
  5. LLM call failure → returns 'neutral', does not raise
  6. All allowed register tokens accepted
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


def _seed_session_row():
    """Ensure session row id=1 exists so emotional_register can be updated."""
    from storage import db
    db.set_session_id("test-session-001")


def _get_emotional_register():
    from storage import db
    with db._conn() as conn:
        row = conn.execute(
            "SELECT emotional_register FROM session WHERE id = 1"
        ).fetchone()
    return row["emotional_register"] if row else None


def _insert_message(role: str, content: str):
    from storage import db
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, datetime('now'))",
            (role, content),
        )


def _insert_message_at(role: str, content: str, ts: str):
    from storage import db
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            (role, content, ts),
        )


def _iso(*, days_ago: float = 0, hours_ago: float = 0) -> str:
    """ISO-T UTC timestamp offset into the past, matching storage.db._now()'s
    format exactly (so string comparison against a since_iso cutoff behaves
    the same way it does in production)."""
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def _insert_msg(*, ts: str, content: str, role: str = "user") -> None:
    """Task 6: insert a message at an exact ISO-T timestamp (as opposed to
    _insert_message's SQLite datetime('now'), which doesn't let a test place
    a row at an arbitrary point relative to the 24h window cutoff)."""
    _insert_message_at(role, content, ts)


def _set_register(register: str) -> None:
    """Seed the session row (if absent) and force emotional_register to
    `register`, simulating a stale value left over from a prior session."""
    _seed_session_row()
    from storage import db
    with db._conn() as conn:
        conn.execute(
            "UPDATE session SET emotional_register = ? WHERE id = 1",
            (register,),
        )


def _get_register() -> str | None:
    return _get_emotional_register()


# ---------------------------------------------------------------------------
# 1. LLM returns 'warm' → emotional_register = 'warm'
# ---------------------------------------------------------------------------

async def test_returns_warm_and_persists(monkeypatch):
    from agents import tonal_recall

    _seed_session_row()
    _insert_message("user", "you're the best, i'm so happy today")
    _insert_message("assistant", "...noted.")
    # Task 6: min-message gate requires >= _MIN_MESSAGES in the 24h window
    # before classification runs — pad with recent filler.
    _insert_message("user", "still here")
    _insert_message("assistant", "mm")

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        return "warm"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == "warm"
    assert _get_emotional_register() == "warm"


# ---------------------------------------------------------------------------
# 2. LLM returns unexpected token → 'neutral' fallback
# ---------------------------------------------------------------------------

async def test_unexpected_token_falls_back_to_neutral(monkeypatch):
    from agents import tonal_recall

    _seed_session_row()
    _insert_message("user", "hello")
    # Task 6: min-message gate — pad above _MIN_MESSAGES so this actually
    # reaches the LLM call and exercises the unexpected-token fallback
    # (rather than short-circuiting on the thin-window gate).
    for i in range(3):
        _insert_message("user", f"filler-{i}")

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        return "confusing_label"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == "neutral"


# ---------------------------------------------------------------------------
# 3. No messages → returns 'neutral', no DB write attempted
# ---------------------------------------------------------------------------

async def test_no_messages_returns_neutral(monkeypatch):
    from agents import tonal_recall

    _seed_session_row()

    called = []

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        called.append(True)
        return "warm"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == "neutral"
    assert not called, "LLM should not be called when there are no messages"


# ---------------------------------------------------------------------------
# 4. DB UPDATE failure → re-raises (Wave 1 fix: stop swallowing failures)
# ---------------------------------------------------------------------------

async def test_db_update_failure_reraises(monkeypatch):
    from agents import tonal_recall

    _seed_session_row()
    _insert_message("user", "test message")
    # Task 6: min-message gate — pad above _MIN_MESSAGES so this reaches the
    # persist call (and its write failure) instead of short-circuiting on
    # the thin-window gate.
    for i in range(3):
        _insert_message("user", f"filler-{i}")

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        return "tense"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    # Patch _conn to raise on UPDATE
    import storage.db as _db
    original_conn = _db._conn

    class _FailConn:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("UPDATE"):
                raise RuntimeError("DB write failure")
            return original_conn().__enter__().execute(sql, params)

    with patch.object(_db, "_conn", return_value=_FailConn()):
        # _conn is a contextmanager — use a contextmanager mock
        from contextlib import contextmanager

        @contextmanager
        def _failing_conn():
            class _FC:
                def execute(self, sql, params=()):
                    if sql.strip().upper().startswith("UPDATE"):
                        raise RuntimeError("DB write failure")
                    # Allow SELECT through for _fetch_today_messages
                    ctx = original_conn()
                    c = ctx.__enter__()
                    return c.execute(sql, params)
            yield _FC()

        monkeypatch.setattr(_db, "_conn", _failing_conn)

        with pytest.raises((RuntimeError, Exception)):
            await tonal_recall.compute_session_register("test-session-001")


# ---------------------------------------------------------------------------
# 5. LLM call raises → returns 'neutral', does not re-raise
# ---------------------------------------------------------------------------

async def test_llm_failure_returns_neutral(monkeypatch):
    from agents import tonal_recall

    _seed_session_row()
    _insert_message("user", "something happened")
    # Task 6: min-message gate — pad above _MIN_MESSAGES so this reaches the
    # LLM call (and its failure) instead of short-circuiting on the
    # thin-window gate.
    for i in range(3):
        _insert_message("user", f"filler-{i}")

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        raise ConnectionError("OpenRouter down")

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == "neutral"  # safe fallback, no raise


# ---------------------------------------------------------------------------
# 6. All allowed register tokens are accepted and persisted
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7. No session row (id=1 absent) → warning logged, no raise, returns register
# ---------------------------------------------------------------------------

async def test_missing_session_row_logs_warning(monkeypatch, caplog):
    """When session row id=1 does not exist the UPDATE silently matches 0 rows.
    The function must log a warning rather than silently swallowing the miss,
    and must still return the classified register without raising.
    """
    import logging

    from agents import tonal_recall

    # Intentionally do NOT call _seed_session_row() — session table is empty.
    _insert_message("user", "something interesting today")
    # Task 6: min-message gate — pad above _MIN_MESSAGES so this reaches the
    # persist call (and its missing-row warning) instead of short-circuiting
    # on the thin-window gate (which would also warn, but for the wrong
    # reason and with the wrong resulting register).
    for i in range(3):
        _insert_message("user", f"filler-{i}")

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        return "warm"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    with caplog.at_level(logging.WARNING, logger="agents.tonal_recall"):
        result = await tonal_recall.compute_session_register("no-session-session")

    assert result == "warm"
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("missing" in w or "id=1" in w or "not persisted" in w for w in warning_texts), (
        f"expected a warning about missing session row, got: {warning_texts}"
    )

@pytest.mark.parametrize("register", ["warm", "neutral", "tense", "frosty", "significant"])
async def test_all_allowed_registers_persist(register, monkeypatch):
    from agents import tonal_recall

    _seed_session_row()
    _insert_message("user", "some content for the session")
    # Task 6: min-message gate — pad above _MIN_MESSAGES so this reaches the
    # LLM call instead of short-circuiting on the thin-window gate.
    for i in range(3):
        _insert_message("user", f"filler-{i}")

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        return register

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == register
    assert _get_emotional_register() == register


# ---------------------------------------------------------------------------
# 8. Evening-local session lands on UTC date D-1 — must still be picked up
#    (Wave fix: calendar-date keying dropped in favor of a recency window)
# ---------------------------------------------------------------------------

async def test_message_from_prior_utc_date_still_classified(monkeypatch):
    """A message stamped with yesterday's UTC date but still inside the last
    24h (e.g. an evening-local session in a timezone behind UTC) must still
    be included — a UTC-*calendar-date* filter would drop it (it lands on
    UTC date D-1), silently keeping the register at 'neutral' forever. Task 6
    replaced calendar-date keying with a rolling 24h window, so this now
    also doubles as a same-window-crosses-midnight regression: the message
    is 20h old (< _WINDOW_HOURS), just on the other side of UTC midnight."""
    from agents import tonal_recall

    _seed_session_row()
    yesterday_ts = _iso(hours_ago=20)
    _insert_msg(ts=yesterday_ts, content="this was a rough one, need to talk")
    # Task 6: min-message gate requires >= _MIN_MESSAGES in the window before
    # classification runs at all — pad with recent filler.
    for i in range(3):
        _insert_msg(ts=_iso(hours_ago=1), content=f"filler-{i}")

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        assert "rough one" in prompt
        return "significant"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == "significant"
    assert _get_emotional_register() == "significant"


# ---------------------------------------------------------------------------
# 9. Task 6 — 24h window excludes messages older than the window
# ---------------------------------------------------------------------------

async def test_window_excludes_old_messages(monkeypatch):
    """c72cf0d's global recent_messages(limit=40) spanned 9 days at real
    traffic volume and pinned the register to a stale 'tense' from week-old
    friction. A message 9 days old must not reach the classification prompt."""
    from agents import tonal_recall

    _seed_session_row()
    _insert_msg(ts=_iso(days_ago=9), content="OLD-TENSE-FIGHT")
    for i in range(5):
        _insert_msg(ts=_iso(hours_ago=2), content=f"fresh-{i}")

    captured: dict = {}

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        captured["prompt"] = prompt
        return "neutral"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    await tonal_recall.compute_session_register("test-session-001")
    assert "OLD-TENSE-FIGHT" not in captured["prompt"]


# ---------------------------------------------------------------------------
# 10. Task 6 — thin window (<4 messages in 24h) persists 'neutral', no LLM call
# ---------------------------------------------------------------------------

async def test_thin_window_persists_neutral(monkeypatch):
    """A stale register must not survive a thin window: fewer than
    _MIN_MESSAGES messages in the last 24h persists 'neutral' directly and
    skips the LLM call entirely, overwriting whatever register was left
    over from a prior (now-stale) session."""
    from agents import tonal_recall

    _set_register("tense")
    _insert_msg(ts=_iso(hours_ago=1), content="hi")

    called = []

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        called.append(1)
        return "warm"

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == "neutral"
    assert not called
    assert _get_register() == "neutral"
