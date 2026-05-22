"""decision_log_capture: predicted_p range validation."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_capture_refuses_negative_p():
    from storage import db
    from tools.decision_log.capture import decision_log_capture
    result = asyncio.run(decision_log_capture.handler(
        {"statement": "x", "predicted_p": -0.5, "resolve_by": "2026-12-01"}
    ))
    assert "[0,1]" in str(result) or "0 or 1" in str(result) or "must be" in str(result)
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    assert n == 0


def test_capture_refuses_above_one():
    from storage import db
    from tools.decision_log.capture import decision_log_capture
    result = asyncio.run(decision_log_capture.handler(
        {"statement": "x", "predicted_p": 3.0, "resolve_by": "2026-12-01"}
    ))
    assert "[0,1]" in str(result) or "must be" in str(result)
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    assert n == 0


def test_capture_accepts_zero_and_one():
    from storage import db
    from tools.decision_log.capture import decision_log_capture
    asyncio.run(decision_log_capture.handler(
        {"statement": "zero", "predicted_p": 0.0, "resolve_by": "2026-12-01"}
    ))
    asyncio.run(decision_log_capture.handler(
        {"statement": "one", "predicted_p": 1.0, "resolve_by": "2026-12-01"}
    ))
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    assert n == 2
