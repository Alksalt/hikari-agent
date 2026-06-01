"""Fix 6: Google auth scope/account startup preflight + fail-open enforce.

The scope/account probes surface OAuth gaps at boot; the per-call enforce
precheck must fail-OPEN when the google scope probe is indeterminate (empty)
so a transient probe failure can't deny every Google tool.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents import config
from storage import db

# The full grant (auth.scripts BASE_SCOPES) — covers every google tool via supersets.
_BASE = {
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
}
_BULK_DELETE = "mcp__google_workspace__gmail_bulk_delete_messages"  # needs full-mail


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


def _fake_provider(scopes=None, account=""):
    p = type("FakeProvider", (), {})()
    p.current_scopes = AsyncMock(return_value=set(scopes or set()))
    p.current_account = AsyncMock(return_value=account)
    return p


# ---------- scope coverage probe ----------

@pytest.mark.asyncio
async def test_probe_scopes_ok_when_covered():
    from agents.google_health import probe_google_scopes
    with patch("auth.providers.get_provider", return_value=_fake_provider(_BASE)):
        status, missing = await probe_google_scopes()
    assert status == "ok"
    assert missing == []


@pytest.mark.asyncio
async def test_probe_scopes_under_scoped_flags_missing():
    from agents.google_health import probe_google_scopes
    granted = _BASE - {"https://mail.google.com/"}  # the real incident
    with patch("auth.providers.get_provider", return_value=_fake_provider(granted)):
        status, missing = await probe_google_scopes()
    assert status == "under_scoped"
    assert "https://mail.google.com/" in missing


@pytest.mark.asyncio
async def test_probe_scopes_unknown_on_empty():
    """Empty granted set = failed probe; must NOT report under_scoped (no false alarm)."""
    from agents.google_health import probe_google_scopes
    with patch("auth.providers.get_provider", return_value=_fake_provider(set())):
        status, _ = await probe_google_scopes()
    assert status == "unknown"


# ---------- account binding probe ----------

@pytest.mark.asyncio
async def test_probe_account_ok_mismatch_unknown(monkeypatch):
    from agents.google_health import probe_google_account
    monkeypatch.setenv("GOOGLE_WORKSPACE_USER_EMAIL", "olealt25@gmail.com")

    with patch("auth.providers.get_provider",
               return_value=_fake_provider(account="olealt25@gmail.com")):
        assert (await probe_google_account())[0] == "ok"

    with patch("auth.providers.get_provider",
               return_value=_fake_provider(account="wrong@gmail.com")):
        status, detail = await probe_google_account()
        assert status == "mismatch"
        assert "wrong@gmail.com" in detail

    with patch("auth.providers.get_provider", return_value=_fake_provider(account="")):
        assert (await probe_google_account())[0] == "unknown"


# ---------- per-call enforce: fail-open on indeterminate ----------

@pytest.mark.asyncio
async def test_precheck_enforce_fails_open_on_empty_google(monkeypatch):
    """The load-bearing safety: empty google scope probe must ALLOW, not deny
    every gated Google tool."""
    monkeypatch.setenv("AUTH_PRECHECK_OVERRIDE", "enforce")
    from agents.hooks import _precheck_scopes
    with patch("auth.providers.get_provider", return_value=_fake_provider(set())):
        out = await _precheck_scopes(_BULK_DELETE, {})
    assert out is None


@pytest.mark.asyncio
async def test_precheck_enforce_denies_when_genuinely_missing(monkeypatch):
    monkeypatch.setenv("AUTH_PRECHECK_OVERRIDE", "enforce")
    from agents.hooks import _precheck_scopes
    granted = {"https://www.googleapis.com/auth/gmail.modify"}  # non-empty, lacks full-mail
    with patch("auth.providers.get_provider", return_value=_fake_provider(granted)):
        out = await _precheck_scopes(_BULK_DELETE, {})
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_precheck_enforce_allows_when_covered(monkeypatch):
    monkeypatch.setenv("AUTH_PRECHECK_OVERRIDE", "enforce")
    from agents.hooks import _precheck_scopes
    with patch("auth.providers.get_provider",
               return_value=_fake_provider({"https://mail.google.com/"})):
        out = await _precheck_scopes(_BULK_DELETE, {})
    assert out is None
