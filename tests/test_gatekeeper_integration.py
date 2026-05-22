"""Phase E integration tests: gatekeeper_can_use_tool SDK hook.

Simulates the can_use_tool path with a fake ToolPermissionContext and verifies
that the gatekeeper correctly routes gated tools through approval flow and
passes everything else through immediately.
"""

from __future__ import annotations

import asyncio
import importlib
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    from agents import config
    config.reload()
    yield


class _FakeContext:
    """Minimal stand-in for ToolPermissionContext."""
    def __init__(self, tool_use_id: str):
        self.tool_use_id = tool_use_id


def _fake_allow(**kwargs):
    """Return a namespace that looks like PermissionResultAllow."""
    return types.SimpleNamespace(behavior="allow", **kwargs)


def _fake_deny(**kwargs):
    """Return a namespace that looks like PermissionResultDeny."""
    return types.SimpleNamespace(behavior="deny", **kwargs)


@pytest.fixture()
def patched_can_use_tool(monkeypatch):
    """Patch out SDK imports so tests work without a real SDK install."""
    import tools.gatekeeper_can_use_tool as mod

    # Patch the SDK result types with our fakes.
    fake_sdk_types = types.ModuleType("claude_agent_sdk.types")
    fake_sdk_types.PermissionResultAllow = _fake_allow
    fake_sdk_types.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(importlib.import_module.__self__.__class__.__module__
                        and {}, "claude_agent_sdk.types", fake_sdk_types)
    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_sdk_types)

    # Reload the module so the patched import is picked up.
    importlib.reload(mod)
    return mod


# ---------- non-gated tool passes through ----------

@pytest.mark.asyncio
async def test_non_gated_tool_returns_allow(monkeypatch):
    """Tools with gate != 'gatekeeper' must get PermissionResultAllow immediately."""
    import sys
    import types as _types

    # Patch SDK types.
    fake_types_mod = _types.ModuleType("claude_agent_sdk.types")
    fake_types_mod.PermissionResultAllow = _fake_allow
    fake_types_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types_mod)

    import tools.gatekeeper_can_use_tool as mod
    importlib.reload(mod)

    db.upsert_core_block("ping", "pong")

    # mcp__hikari_memory__recall has gate: null in tools.yaml.
    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_memory__recall",
        {"query": "anything"},
        _FakeContext("tu_non_gated_001"),
    )
    assert result.behavior == "allow"


# ---------- gated tool → approve → Allow ----------

@pytest.mark.asyncio
async def test_gated_tool_approve_returns_allow(monkeypatch):
    """gatekeeper_can_use_tool for a gated tool returns Allow after approval."""
    import sys
    import types as _types

    fake_types_mod = _types.ModuleType("claude_agent_sdk.types")
    fake_types_mod.PermissionResultAllow = _fake_allow
    fake_types_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types_mod)

    # Use a fresh Gatekeeper to avoid shared state.
    from tools.gatekeeper import Gatekeeper
    fresh_gk = Gatekeeper()
    fresh_gk.set_send_text(lambda chat_id, text: asyncio.sleep(0))

    import tools.gatekeeper_can_use_tool as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_gate_for", lambda _: "gatekeeper")
    monkeypatch.setattr(mod, "_resolve_chat_id", lambda: 12345)
    monkeypatch.setattr(
        mod, "_deadline_for",
        lambda _: datetime.now(timezone.utc) + timedelta(seconds=30),
    )

    import tools.gatekeeper as gk_mod
    monkeypatch.setattr(gk_mod, "GATEKEEPER", fresh_gk)

    db.upsert_core_block("ping", "pong")

    async def _approve():
        await asyncio.sleep(0.05)
        await fresh_gk.resolve("tu_can_use_001", "approved")

    task = asyncio.create_task(_approve())

    result = await mod.gatekeeper_can_use_tool(
        "mcp__google_workspace__gmail_bulk_delete_messages",
        {"query": "label:trash"},
        _FakeContext("tu_can_use_001"),
    )
    await task
    assert result.behavior == "allow"


# ---------- gated tool → reject → Deny ----------

@pytest.mark.asyncio
async def test_gated_tool_reject_returns_deny(monkeypatch):
    """gatekeeper_can_use_tool for a gated tool returns Deny after rejection."""
    import sys
    import types as _types

    fake_types_mod = _types.ModuleType("claude_agent_sdk.types")
    fake_types_mod.PermissionResultAllow = _fake_allow
    fake_types_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types_mod)

    from tools.gatekeeper import Gatekeeper
    fresh_gk = Gatekeeper()
    fresh_gk.set_send_text(lambda chat_id, text: asyncio.sleep(0))

    import tools.gatekeeper_can_use_tool as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_gate_for", lambda _: "gatekeeper")
    monkeypatch.setattr(mod, "_resolve_chat_id", lambda: 12345)
    monkeypatch.setattr(
        mod, "_deadline_for",
        lambda _: datetime.now(timezone.utc) + timedelta(seconds=30),
    )

    import tools.gatekeeper as gk_mod
    monkeypatch.setattr(gk_mod, "GATEKEEPER", fresh_gk)

    db.upsert_core_block("ping", "pong")

    async def _reject():
        await asyncio.sleep(0.05)
        await fresh_gk.resolve("tu_can_use_002", "rejected")

    task = asyncio.create_task(_reject())

    result = await mod.gatekeeper_can_use_tool(
        "mcp__google_workspace__gmail_bulk_delete_messages",
        {"query": "label:trash"},
        _FakeContext("tu_can_use_002"),
    )
    await task
    assert result.behavior == "deny"
    assert "rejected" in result.message


# ---------- yaml registry: gmail_bulk_delete now has gate=gatekeeper ----------

def test_gmail_bulk_delete_gate_is_gatekeeper():
    """After Phase E, gmail_bulk_delete_messages must have gate: gatekeeper in the registry."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    spec = reg._resolve("mcp__google_workspace__gmail_bulk_delete_messages")
    assert spec is not None
    assert spec.gate == "gatekeeper"


def test_gmail_bulk_delete_not_in_defer_patterns():
    """gmail_bulk_delete_messages must NOT appear in defer_gated_patterns()."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    patterns = reg.defer_gated_patterns()
    import re
    tool = "mcp__google_workspace__gmail_bulk_delete_messages"
    for pat in patterns:
        assert not re.fullmatch(pat, tool), (
            f"gmail_bulk_delete_messages unexpectedly matched defer pattern {pat!r}"
        )
