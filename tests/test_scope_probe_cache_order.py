"""probe_gmail_bulk_delete_scope_ok — env-before-cache ordering regression."""
from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import config


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


def test_scope_probe_returns_false_when_env_unset_even_if_cache_true(monkeypatch):
    """Cache says True, but env vars are missing → must return False without touching cache."""
    from storage import db as db_mod
    from tools import approvals

    fresh_checked_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(db_mod, "runtime_get", lambda key: {
        approvals.SCOPE_PROBE_OK_KEY: "true",
        approvals.SCOPE_PROBE_CHECKED_AT_KEY: fresh_checked_at,
    }.get(key))

    monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", raising=False)

    result = asyncio.run(approvals.probe_gmail_bulk_delete_scope_ok())
    assert result is False


def test_scope_probe_returns_cached_when_env_set_and_cache_fresh(monkeypatch):
    """Env vars present + cache fresh → return cached True without network call."""
    from storage import db as db_mod
    from tools import approvals

    fresh_checked_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(db_mod, "runtime_get", lambda key: {
        approvals.SCOPE_PROBE_OK_KEY: "true",
        approvals.SCOPE_PROBE_CHECKED_AT_KEY: fresh_checked_at,
    }.get(key))

    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "fake_id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "fake_secret")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "fake_token")

    result = asyncio.run(approvals.probe_gmail_bulk_delete_scope_ok())
    assert result is True
