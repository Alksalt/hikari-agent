"""Sprint 4 Phase 4C-3b — registry access_mode + wildcard write deny."""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


# ---------------------------------------------------------------------------
# access_mode field round-trips through the registry
# ---------------------------------------------------------------------------

@pytest.fixture()
def registry():
    from tools._tools_yaml import DEFAULT_YAML_PATH, _load_yaml
    return _load_yaml(DEFAULT_YAML_PATH)


def test_google_workspace_has_no_wildcard(registry):
    # Unknown upstream tools must be absent from discovery, not merely denied
    # after the model has already seen them.
    assert registry._resolve("mcp__google_workspace__*") is None


def test_access_mode_write_on_apple_events(registry):
    spec = registry._resolve("mcp__apple_events__*")
    assert spec is not None
    assert spec.access_mode == "write"


def test_access_mode_write_on_playwright(registry):
    # Sprint A: wildcard flipped to write so any future persistent-state
    # playwright tool fails closed via the gatekeeper wildcard-write deny path.
    spec = registry._resolve("mcp__playwright__*")
    assert spec is not None
    assert spec.access_mode == "write"


def test_access_mode_write_on_notion_wildcard(registry):
    # Phase 0.2 reviewer-fix: wildcard flipped to write → fail-closed.
    spec = registry._resolve("mcp__notion__*")
    assert spec is not None
    assert spec.access_mode == "write"


def test_access_mode_write_on_github_wildcard(registry):
    # Phase 0.2 reviewer-fix: wildcard flipped to write → fail-closed.
    spec = registry._resolve("mcp__github__*")
    assert spec is not None
    assert spec.access_mode == "write"


# ---------------------------------------------------------------------------
# _resolve_with_kind
# ---------------------------------------------------------------------------

def test_resolve_with_kind_explicit(registry):
    spec, kind = registry._resolve_with_kind("mcp__google_workspace__gmail_send_email")
    assert spec is not None
    assert kind == "explicit"


def test_resolve_with_kind_unknown_google_workspace_tool(registry):
    spec, kind = registry._resolve_with_kind("mcp__google_workspace__some_future_tool")
    assert spec is None
    assert kind is None


def test_resolve_with_kind_unknown(registry):
    spec, kind = registry._resolve_with_kind("mcp__totally_unknown__tool")
    assert spec is None
    assert kind is None


# ---------------------------------------------------------------------------
# gatekeeper_can_use_tool — wildcard write/destructive → deny
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_wildcard_denied():
    """A tool that resolves only via a write wildcard must be denied.

    Uses a github tool that has no explicit registry entry so it falls through
    to the mcp__github__* wildcard (access_mode=write) — the gatekeeper
    wildcard-write deny must fire.
    """
    from tools.gatekeeper_can_use_tool import gatekeeper_can_use_tool
    ns = SimpleNamespace(tool_use_id="t1")
    result = await gatekeeper_can_use_tool("mcp__github__some_unknown_write_op", {}, ns)
    msg = getattr(result, "message", "") or ""
    behavior = getattr(result, "behavior", "")
    assert "write" in msg or behavior == "deny", (
        f"expected deny for write wildcard, got behavior={behavior!r} msg={msg!r}"
    )


@pytest.mark.asyncio
async def test_read_wildcard_allowed():
    """Read-mode wildcards stay allowed (no gate, no write/destructive)."""
    from tools.gatekeeper_can_use_tool import gatekeeper_can_use_tool
    ns = SimpleNamespace(tool_use_id="t1")
    result = await gatekeeper_can_use_tool("mcp__google_workspace__query_gmail_emails", {}, ns)
    behavior = getattr(result, "behavior", "")
    # PermissionResultAllow has no 'behavior' attribute in the SDK — absence of deny is fine
    assert behavior in ("allow", ""), f"read wildcard should allow, got {result!r}"


@pytest.mark.asyncio
async def test_apple_events_write_wildcard_denied():
    """apple_events wildcard (write) must be denied for unknown tools."""
    from tools.gatekeeper_can_use_tool import gatekeeper_can_use_tool
    ns = SimpleNamespace(tool_use_id="t1")
    result = await gatekeeper_can_use_tool("mcp__apple_events__do_something_new", {}, ns)
    msg = getattr(result, "message", "") or ""
    behavior = getattr(result, "behavior", "")
    assert "write" in msg or behavior == "deny", (
        f"expected deny for write wildcard, got behavior={behavior!r} msg={msg!r}"
    )
