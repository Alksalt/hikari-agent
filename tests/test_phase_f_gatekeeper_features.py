"""Phase F: Gatekeeper new feature tests.

Covers:
- always_approve per-session per-tool allowlist (hit, miss, TTL expiry)
- per-tool timeout override via gate_timeout_sec in tools.yaml
- per-tool timeout fallback to default when gate_timeout_sec is absent

(The /approvals slash-command coverage moved to test_telegram_cockpit_cmds.py
when slash-commands were removed.)
"""

from __future__ import annotations

import importlib
from datetime import UTC
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
    yield


# ---------------------------------------------------------------------------
# Feature 1: always_approve per-session per-tool
# ---------------------------------------------------------------------------

def test_always_approve_hits_within_ttl():
    """always_approve whitelists (chat_id, tool_name); _check_always_approve
    returns True within the TTL."""
    from tools.approvals import _ALWAYS_APPROVE, _check_always_approve, always_approve

    _ALWAYS_APPROVE.clear()
    always_approve(chat_id=12345, tool_name="mcp__test__tool", ttl_seconds=3600)
    assert _check_always_approve(12345, "mcp__test__tool") is True
    # Different chat_id or tool_name — no hit.
    assert _check_always_approve(99999, "mcp__test__tool") is False
    assert _check_always_approve(12345, "mcp__test__other_tool") is False
    _ALWAYS_APPROVE.clear()


def test_always_approve_expires_after_ttl():
    """When the TTL has elapsed, _check_always_approve returns False and
    evicts the stale entry."""
    import time

    from tools.approvals import _ALWAYS_APPROVE, _check_always_approve

    _ALWAYS_APPROVE.clear()
    # Plant an entry that expired 1 second ago — no real sleep needed.
    _ALWAYS_APPROVE[(12345, "mcp__test__expiry")] = time.time() - 1.0
    assert _check_always_approve(12345, "mcp__test__expiry") is False
    # Entry was evicted.
    assert (12345, "mcp__test__expiry") not in _ALWAYS_APPROVE
    _ALWAYS_APPROVE.clear()


@pytest.mark.asyncio
async def test_gatekeeper_request_skips_prompt_when_always_approve(monkeypatch, tmp_path):
    """When always_approve is active, Gatekeeper.request returns 'approved'
    without calling send_text (no Telegram prompt)."""
    from datetime import datetime, timedelta

    from tools.approvals import _ALWAYS_APPROVE, always_approve
    from tools.gatekeeper import GATEKEEPER

    _ALWAYS_APPROVE.clear()
    always_approve(chat_id=12345, tool_name="mcp__test__gated", ttl_seconds=60)

    send_calls: list = []

    async def fake_send(chat_id, text):
        send_calls.append((chat_id, text))

    GATEKEEPER.set_send_text(fake_send)

    outcome = await GATEKEEPER.request(
        tool_use_id="tu_always_approve_test",
        tool_name="mcp__test__gated",
        chat_id=12345,
        args={},
        summary="test tool",
        deadline=datetime.now(UTC) + timedelta(seconds=10),
    )

    assert outcome == "approved"
    assert len(send_calls) == 0, "should not have prompted user when always_approve is active"
    _ALWAYS_APPROVE.clear()


# ---------------------------------------------------------------------------
# Feature 3: per-tool timeout override via _deadline_for
# ---------------------------------------------------------------------------

def test_per_tool_timeout_override_applies(tmp_path, monkeypatch):
    """When gate_timeout_sec is set in tools.yaml, _deadline_for uses it.

    Tests the ToolSpec.gate_timeout_sec field and the deadline computation
    logic in gatekeeper_can_use_tool._deadline_for by monkeypatching the
    registry inside that module's local import scope.
    """
    from datetime import datetime

    # Create a minimal tools.yaml fixture with gate_timeout_sec=120.
    yaml_text = """
mcp_servers: {}
tools:
  - id: "mcp__test__timed_tool"
    bucket: 1
    server: null
    gate: gatekeeper
    gate_timeout_sec: 120
    untrusted_output: false
    wrap_patterns: []
subagents: {}
"""
    yaml_path = tmp_path / "tools.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    from tools._tools_yaml import load_registry
    reg = load_registry(yaml_path)

    # Verify the spec parsed correctly.
    spec = reg._resolve("mcp__test__timed_tool")
    assert spec is not None
    assert spec.gate_timeout_sec == 120

    # Patch load_registry inside _tools_yaml so the cached call inside
    # _deadline_for picks up our fixture registry.
    monkeypatch.setattr("tools._tools_yaml.load_registry", lambda path=None: reg)

    from tools.gatekeeper_can_use_tool import _deadline_for

    before = datetime.now(UTC)
    deadline = _deadline_for("mcp__test__timed_tool")
    delta = (deadline - before).total_seconds()

    # Should be close to 120s (allow ±5s for test overhead).
    assert 115 <= delta <= 125, f"Expected ~120s deadline, got delta={delta:.1f}s"


def test_per_tool_timeout_falls_back_to_default(tmp_path, monkeypatch):
    """When gate_timeout_sec is absent, _deadline_for falls back to config default."""
    from datetime import datetime

    yaml_text = """
mcp_servers: {}
tools:
  - id: "mcp__test__no_timeout_tool"
    bucket: 1
    server: null
    gate: gatekeeper
    untrusted_output: false
    wrap_patterns: []
subagents: {}
"""
    yaml_path = tmp_path / "tools.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    from tools._tools_yaml import load_registry
    reg = load_registry(yaml_path)

    spec = reg._resolve("mcp__test__no_timeout_tool")
    assert spec is not None
    assert spec.gate_timeout_sec is None

    from tools.gatekeeper_can_use_tool import _deadline_for

    before = datetime.now(UTC)
    deadline = _deadline_for("mcp__test__no_timeout_tool")
    delta = (deadline - before).total_seconds()

    # Default is 300s (from config or fallback). Allow ±5s.
    assert 295 <= delta <= 305, f"Expected ~300s fallback deadline, got delta={delta:.1f}s"
