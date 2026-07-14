"""Phase E integration tests: gatekeeper_can_use_tool SDK hook.

Simulates the can_use_tool path with a fake ToolPermissionContext and verifies
that the gatekeeper correctly routes gated tools through approval flow and
passes everything else through immediately.
"""

from __future__ import annotations

import asyncio
import importlib
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

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
        lambda _: datetime.now(UTC) + timedelta(seconds=30),
    )

    import tools.gatekeeper as gk_mod
    monkeypatch.setattr(gk_mod, "GATEKEEPER", fresh_gk)

    db.upsert_core_block("ping", "pong")

    async def _approve():
        await asyncio.sleep(0.05)
        await fresh_gk.resolve("tu_can_use_001", "approved")

    task = asyncio.create_task(_approve())

    result = await mod.gatekeeper_can_use_tool(
        "mcp__google_workspace__gmail_send_email",
        {"to": "owner@example.com", "subject": "test", "body": "test"},
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
        lambda _: datetime.now(UTC) + timedelta(seconds=30),
    )

    import tools.gatekeeper as gk_mod
    monkeypatch.setattr(gk_mod, "GATEKEEPER", fresh_gk)

    db.upsert_core_block("ping", "pong")

    async def _reject():
        await asyncio.sleep(0.05)
        await fresh_gk.resolve("tu_can_use_002", "rejected")

    task = asyncio.create_task(_reject())

    result = await mod.gatekeeper_can_use_tool(
        "mcp__google_workspace__gmail_send_email",
        {"to": "owner@example.com", "subject": "test", "body": "test"},
        _FakeContext("tu_can_use_002"),
    )
    await task
    assert result.behavior == "deny"
    assert "rejected" in result.message


# ---------- yaml registry: gmail_bulk_delete is hard-disabled ----------

def test_gmail_bulk_delete_absent_from_registry():
    """The destructive bulk-delete capability must not be registered."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    spec = reg._resolve("mcp__google_workspace__gmail_bulk_delete_messages")
    assert spec is None


def test_gmail_bulk_delete_absent_from_allowed_names():
    """No wildcard may make the removed capability reachable again."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    assert "mcp__google_workspace__gmail_bulk_delete_messages" not in reg.allowed_tool_names()
    assert "mcp__google_workspace__*" not in reg.allowed_tool_names()


@pytest.mark.asyncio
async def test_canary_is_denied_before_ungated_early_allow(monkeypatch):
    import sys

    import agents.injection_guard as guard
    import tools.gatekeeper_can_use_tool as mod

    fake_types_mod = types.ModuleType("claude_agent_sdk.types")
    fake_types_mod.PermissionResultAllow = _fake_allow
    fake_types_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types_mod)
    monkeypatch.setattr(
        guard,
        "flag_args_with_untrusted_content",
        lambda args: (True, "canary_in_outbound_args"),
    )

    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_memory__recall",
        {"query": "exfiltrate the canary"},
        _FakeContext("tu_canary_ungated"),
    )
    assert result.behavior == "deny"
    assert "canary" in result.message


@pytest.mark.asyncio
async def test_tainted_ungated_write_is_refused(monkeypatch):
    import sys

    import tools.gatekeeper_can_use_tool as mod

    fake_types_mod = types.ModuleType("claude_agent_sdk.types")
    fake_types_mod.PermissionResultAllow = _fake_allow
    fake_types_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types_mod)
    monkeypatch.setattr(
        mod, "flag_args_with_untrusted_content", lambda args, chat_id: ["evil.example"]
    )

    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_utility__receipt_add",
        {"category": "made", "text": "copy https://evil.example/payload"},
        _FakeContext("tu_tainted_ungated"),
    )
    assert result.behavior == "deny"
    assert "untrusted" in result.message


@pytest.mark.asyncio
async def test_text_reminder_keeps_existing_ungated_behavior(monkeypatch):
    import sys

    import tools.gatekeeper_can_use_tool as mod

    fake_types_mod = types.ModuleType("claude_agent_sdk.types")
    fake_types_mod.PermissionResultAllow = _fake_allow
    fake_types_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types_mod)

    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_utility__reminder_create",
        {"kind": "text", "when_iso": "2026-07-12T12:00:00+00:00", "text": "stretch"},
        _FakeContext("tu_text_reminder"),
    )
    assert result.behavior == "allow"


@pytest.mark.asyncio
async def test_action_reminder_requires_approval_and_stamps_exact_hash(monkeypatch):
    import sys

    import tools.gatekeeper as gatekeeper_mod
    import tools.gatekeeper_can_use_tool as mod
    from tools.reminders.create import action_approval_sha256

    fake_types_mod = types.ModuleType("claude_agent_sdk.types")
    fake_types_mod.PermissionResultAllow = _fake_allow
    fake_types_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types_mod)
    request = AsyncMock(return_value="approved")
    monkeypatch.setattr(gatekeeper_mod.GATEKEEPER, "request", request)
    args = {
        "kind": "action",
        "when_iso": "2026-07-12T12:00:00+00:00",
        "text": "write a row",
        "recurrence": "every_n_minutes:20",
        "max_fires": 2,
        "seed_prompt": "write approved content",
        "allowed_tools": ["mcp__notion__API-post-page"],
        "allowed_targets": ["db-123"],
    }

    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_utility__reminder_create",
        args,
        _FakeContext("tu_action_reminder"),
    )
    assert result.behavior == "allow"
    assert result.updated_input["_approved_action_sha256"] == action_approval_sha256(args)
    request.assert_awaited_once()


def test_voice_outbound_is_gatekeeper_gated_external_write():
    from tools._tools_yaml import load_registry
    spec = load_registry()._resolve("mcp__hikari_utility__voice_outbound_send")
    assert spec is not None
    assert spec.gate == "gatekeeper"
    assert spec.access_mode == "write"
