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


# ---------------------------------------------------------------------------
# 1. LLM returns 'warm' → emotional_register = 'warm'
# ---------------------------------------------------------------------------

async def test_returns_warm_and_persists(monkeypatch):
    from agents import tonal_recall

    _seed_session_row()
    _insert_message("user", "you're the best, i'm so happy today")
    _insert_message("assistant", "...noted.")

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

    async def _fake_aux(prompt, *, system=None, max_tokens=16):
        return register

    monkeypatch.setattr("agents.tonal_recall.run_aux_composition", _fake_aux)

    result = await tonal_recall.compute_session_register("test-session-001")
    assert result == register
    assert _get_emotional_register() == register
