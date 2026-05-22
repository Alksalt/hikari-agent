"""End-to-end: capture → resolve → Brier score."""
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


def test_capture_resolve_brier_e2e():
    from storage import db
    from tools.decision_log.capture import decision_log_capture
    from tools.decision_log.resolve import decision_log_resolve

    asyncio.run(decision_log_capture.handler(
        {"statement": "ships friday", "predicted_p": 0.8,
         "resolve_by": "2026-05-01"}
    ))

    with db._conn() as c:
        row = c.execute(
            "SELECT id FROM decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    did = row["id"]

    asyncio.run(decision_log_resolve.handler({"decision_id": did, "outcome": 1}))

    score = db.decision_brier_score(window_days=365)
    assert score["n"] == 1
