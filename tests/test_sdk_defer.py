"""Phase 6/8 SDK-defer migration tests: PreToolUse defer hook, deferred-row
persistence + recovery, resume-via-fresh-_run_query, concurrency lock.

Phase 8 reshaped the approval matrix: ``wiki_append`` is no longer gated, and
``dispatch_claude_session`` is conditionally gated based on its ``allowed_tools``
arg. These tests use the dispatch arg-gate as the canonical defer scenario.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------- db schema migration ----------

def test_approvals_table_has_defer_columns():
    """Idempotent migration adds deferred_* columns."""
    # First call ensures schema + migrations.
    db.upsert_core_block("ping", "ping")  # any write triggers _conn -> _ensure_schema
    with db._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(approvals)").fetchall()}
    for needed in ("deferred_tool_use_id", "deferred_tool_name",
                   "deferred_tool_input_json"):
        assert needed in cols, f"missing column {needed}"


def test_approval_create_deferred_round_trip():
    aid = db.approval_create_deferred(
        chat_id=12345,
        tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2,
        summary="dispatch: edit repo",
        args={"repo_path": "/Users/alt/work_dir/x", "task": "fix bug",
              "allowed_tools": "Read,Edit,Bash"},
        deferred_tool_use_id="tu_test_001",
        deferred_tool_input={"repo_path": "/Users/alt/work_dir/x",
                             "task": "fix bug",
                             "allowed_tools": "Read,Edit,Bash"},
    )
    assert aid > 0
    row = db.approval_pending_for(12345)
    assert row is not None
    assert row["deferred_tool_use_id"] == "tu_test_001"
    assert row["deferred_tool_name"] == "mcp__hikari_dispatch__dispatch_claude_session"
    assert json.loads(row["deferred_tool_input_json"])["allowed_tools"] == "Read,Edit,Bash"


def test_approvals_pending_deferred_returns_only_defer_rows():
    """approvals_pending_deferred filters out legacy (no deferred_tool_use_id) rows."""
    # A legacy row (no deferred fields).
    db.approval_create(12345, "legacy_tool", 2, "...", {"x": 1})
    # A defer row.
    db.approval_create_deferred(
        chat_id=12345, tool_name="t", tier=2, summary="s",
        args={}, deferred_tool_use_id="tu_a", deferred_tool_input={},
    )
    deferred = db.approvals_pending_deferred()
    assert len(deferred) == 1
    assert deferred[0]["deferred_tool_use_id"] == "tu_a"


# ---------- PreToolUse defer hook ----------

@pytest.mark.asyncio
async def test_defer_hook_returns_defer_for_gated_tool(monkeypatch):
    """defer_gated_tools returns the SDK defer decision for tools in the gated list."""
    from agents import hooks

    # Stub the OOB Telegram prompt so we don't actually need a bot.
    sent: list[tuple[int, int, str]] = []
    from tools import approvals as approval_tools

    async def fake_send(chat_id, tier, summary):
        sent.append((chat_id, tier, summary))
    monkeypatch.setattr(approval_tools, "send_defer_prompt", fake_send)

    out = await hooks.defer_gated_tools(
        {"tool_name": "mcp__hikari_dispatch__dispatch_claude_session",
         "tool_use_id": "tu_abc",
         "tool_input": {"repo_path": "/Users/alt/work_dir/x",
                        "task": "fix bug",
                        "allowed_tools": "Read,Edit,Bash"}},
        None, None,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "defer"
    # Persisted a row.
    pending = db.approval_pending_for(12345)
    assert pending is not None
    assert pending["deferred_tool_use_id"] == "tu_abc"
    # Sent a prompt.
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_defer_hook_skips_ungated_tool():
    """Non-gated tools pass through with an empty hook output (no defer)."""
    from agents import hooks
    out = await hooks.defer_gated_tools(
        {"tool_name": "mcp__hikari_memory__recall",
         "tool_use_id": "tu_def",
         "tool_input": {"query": "anything"}},
        None, None,
    )
    assert out == {}
    assert db.approval_pending_for(12345) is None


# ---------- _resume_after_defer ----------

@pytest.mark.asyncio
async def test_resume_after_defer_invokes_run_query_with_extra_tool(monkeypatch):
    """After 'y', _resume_after_defer kicks off run_internal_control passing
    the post-approval sibling tool in extra_allowed_tools.
    Stream C: _run_query was split into _invoke_sdk + 3 entrypoints;
    approval resume now uses run_internal_control."""
    from tools import approvals as approval_tools

    # Stub the post-approval reply send.
    sent: list[tuple[int, str]] = []

    async def fake_safe_send(chat_id, text):
        sent.append((chat_id, text))
    monkeypatch.setattr(approval_tools, "_safe_send", fake_safe_send)

    # Stub run_internal_control to record what it was called with.
    captured: dict = {}

    async def fake_run_internal_control(prompt, *, max_turns, max_budget_usd,
                                        extra_allowed_tools=None):
        captured["prompt"] = prompt
        captured["extra_allowed_tools"] = extra_allowed_tools
        captured["max_turns"] = max_turns
        return "done."

    import agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "run_internal_control", fake_run_internal_control)

    aid = db.approval_create_deferred(
        chat_id=12345,
        tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2,
        summary="dispatch: edit cabbage repo",
        args={"repo_path": "/Users/alt/work_dir/cabbage",
              "task": "ripen the cabbage",
              "allowed_tools": "Read,Edit,Bash"},
        deferred_tool_use_id="tu_xyz",
        deferred_tool_input={"repo_path": "/Users/alt/work_dir/cabbage",
                             "task": "ripen the cabbage",
                             "allowed_tools": "Read,Edit,Bash"},
    )
    pending = db.approval_pending_for(12345)
    consumed = await approval_tools._resume_after_defer(aid, pending)
    assert consumed is True
    # Resume called the LLM with the sibling tool in allowlist.
    assert captured["extra_allowed_tools"] == [
        "mcp__hikari_dispatch_confirmed__dispatch_claude_session_confirmed"
    ]
    # The synthetic prompt references the args.
    assert "ripen the cabbage" in captured["prompt"]
    assert "dispatch_claude_session_confirmed" in captured["prompt"]
    # Reply was sent.
    assert any("done" in t for _, t in sent)


@pytest.mark.asyncio
async def test_resume_after_defer_aborts_without_confirmed_mapping(monkeypatch, tmp_path):
    """If config has no defer_confirmed_tools entry for the gated tool, the
    resume aborts cleanly (no _run_query call, status=rejected)."""
    # Tweak config so defer_confirmed_tools is empty.
    cfg_text = (
        "approvals:\n"
        "  defer_confirmed_tools: {}\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from tools import approvals as approval_tools
    sent: list[tuple[int, str]] = []

    async def fake_safe_send(chat_id, text):
        sent.append((chat_id, text))
    monkeypatch.setattr(approval_tools, "_safe_send", fake_safe_send)

    called = {"n": 0}

    async def fake_run_internal_control(*a, **k):
        called["n"] += 1
        return "should not be called"

    import agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "run_internal_control", fake_run_internal_control)

    aid = db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2, summary="...",
        args={}, deferred_tool_use_id="tu_oops", deferred_tool_input={},
    )
    pending = db.approval_pending_for(12345)
    await approval_tools._resume_after_defer(aid, pending)
    assert called["n"] == 0  # run_internal_control never called
    assert any("no confirmed-tool mapping" in t for _, t in sent)


# ---------- routing through resolve_pending_approval ----------

@pytest.mark.asyncio
async def test_resolve_routes_defer_row_to_resume(monkeypatch):
    """resolve_pending_approval picks the defer path when the pending row
    carries a deferred_tool_use_id."""
    from tools import approvals as approval_tools

    # Stub send + _run_query.
    async def fake_safe_send(chat_id, text):
        return None
    monkeypatch.setattr(approval_tools, "_safe_send", fake_safe_send)

    routed = {"resume": 0, "legacy": 0}

    async def fake_resume(aid, pending):
        routed["resume"] += 1
        return True

    async def fake_run_approval(aid, pending):
        routed["legacy"] += 1
        return True

    monkeypatch.setattr(approval_tools, "_resume_after_defer", fake_resume)
    monkeypatch.setattr(approval_tools, "_run_approval", fake_run_approval)

    db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2, summary="...",
        args={}, deferred_tool_use_id="tu_routed", deferred_tool_input={},
    )
    # Phase 8: only CONFIRM-SEND triggers; a stray "y" should NOT consume.
    handled_y = await approval_tools.resolve_pending_approval(12345, "y")
    assert handled_y is False
    assert routed["resume"] == 0
    # The explicit phrase routes through to resume.
    handled = await approval_tools.resolve_pending_approval(12345, "CONFIRM-SEND")
    assert handled is True
    assert routed["resume"] == 1
    assert routed["legacy"] == 0


# ---------- concurrency lock ----------

@pytest.mark.asyncio
async def test_run_query_lock_serializes_calls(monkeypatch):
    """_RUN_LOCK ensures two concurrent _run_query calls don't fork the SDK
    session. We can't easily inspect SDK state here, so we observe that the
    locked-region body runs serially: enter A → exit A → enter B."""
    import agents.runtime as runtime_mod

    timeline: list[str] = []
    enter_count = {"n": 0}

    async def fake_inner():
        # Stand in for everything inside the lock — sleep briefly so we'd
        # interleave if the lock weren't there.
        enter_count["n"] += 1
        timeline.append(f"enter-{enter_count['n']}")
        await asyncio.sleep(0.05)
        timeline.append(f"exit-{enter_count['n']}")

    async def patched_run_query(prompt, *, max_turns=15, max_budget_usd=0.50,
                                log_to_memory=True, extra_allowed_tools=None):
        async with runtime_mod._RUN_LOCK:
            await fake_inner()

    # Two concurrent calls.
    await asyncio.gather(
        patched_run_query("a"),
        patched_run_query("b"),
    )
    # Strict serialization: enter-1 → exit-1 → enter-2 → exit-2
    assert timeline == ["enter-1", "exit-1", "enter-2", "exit-2"]


# ---------- format-string robustness ----------

@pytest.mark.asyncio
async def test_resume_handles_braces_in_tool_input(monkeypatch):
    """JSON content with literal braces must not crash the format() call."""
    from tools import approvals as approval_tools

    async def fake_safe_send(chat_id, text):
        return None
    monkeypatch.setattr(approval_tools, "_safe_send", fake_safe_send)

    captured: dict = {}

    async def fake_run_internal_control(prompt, *, max_turns, max_budget_usd,
                                        extra_allowed_tools=None):
        captured["prompt"] = prompt
        return "ok"

    import agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "run_internal_control", fake_run_internal_control)

    aid = db.approval_create_deferred(
        chat_id=12345,
        tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2,
        summary="...",
        # The content literally contains JSON braces that would break str.format
        # if not escaped.
        args={"repo_path": "/Users/alt/work_dir/x",
              "task": '{"key": "value", "list": [1,2]}',
              "allowed_tools": "Read,Edit"},
        deferred_tool_use_id="tu_braces",
        deferred_tool_input={"repo_path": "/Users/alt/work_dir/x",
                             "task": '{"key": "value", "list": [1,2]}',
                             "allowed_tools": "Read,Edit"},
    )
    pending = db.approval_pending_for(12345)
    consumed = await approval_tools._resume_after_defer(aid, pending)
    assert consumed is True
    # Prompt got built (didn't raise) and contains the original content. The
    # inner quotes are JSON-escaped (\") because the content is nested JSON.
    assert "key" in captured["prompt"]
    assert "value" in captured["prompt"]
    assert "list" in captured["prompt"]


# ---------- ordering: approve marker fires AFTER successful execution ----------

@pytest.mark.asyncio
async def test_resume_failure_keeps_row_as_rejected_not_approved(monkeypatch):
    """If _run_query raises, the approval row must NOT be left as 'approved'
    in the audit log — it must be 'rejected' so restart-recovery doesn't loop."""
    from tools import approvals as approval_tools

    async def fake_safe_send(chat_id, text):
        return None
    monkeypatch.setattr(approval_tools, "_safe_send", fake_safe_send)

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated SDK failure")

    import agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "run_internal_control", boom)

    aid = db.approval_create_deferred(
        chat_id=12345,
        tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2, summary="...",
        args={"repo_path": "/Users/alt/work_dir/x", "task": "fail",
              "allowed_tools": "Read,Edit"},
        deferred_tool_use_id="tu_fail",
        deferred_tool_input={"repo_path": "/Users/alt/work_dir/x",
                             "task": "fail",
                             "allowed_tools": "Read,Edit"},
    )
    pending = db.approval_pending_for(12345)
    await approval_tools._resume_after_defer(aid, pending)

    # Row should NOT be 'approved' — it should be 'rejected' (or any
    # non-pending terminal state) so the restart-recovery skips it.
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE id = ?", (aid,),
        ).fetchone()
    assert row["status"] == "rejected"


# ---------- hook: defer fires even when OOB prompt send fails ----------

@pytest.mark.asyncio
async def test_defer_hook_still_defers_when_oob_send_fails(monkeypatch):
    """Best-effort guarantee: prompt-send failure does NOT prevent the SDK halt."""
    from agents import hooks
    from tools import approvals as approval_tools

    async def fake_send(chat_id, tier, summary):
        raise RuntimeError("telegram unreachable")
    monkeypatch.setattr(approval_tools, "send_defer_prompt", fake_send)

    out = await hooks.defer_gated_tools(
        {"tool_name": "mcp__hikari_dispatch__dispatch_claude_session",
         "tool_use_id": "tu_no_telegram",
         "tool_input": {"repo_path": "/Users/alt/work_dir/x", "task": "t",
                        "allowed_tools": "Read,Edit"}},
        None, None,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "defer"
    # Row persisted despite send failure (we'd rather lose the prompt than the halt).
    pending = db.approval_pending_for(12345)
    assert pending is not None
    assert pending["deferred_tool_use_id"] == "tu_no_telegram"


# ---------- restart recovery ----------

@pytest.mark.asyncio
async def test_recover_deferred_approvals_resurfaces_prompt(monkeypatch):
    """On startup, any pending deferred approval gets re-sent with a
    restart-suffix so the user knows it's a resurrection."""
    from agents import background_listener
    from tools import approvals as approval_tools

    sent: list[tuple[int, int, str]] = []

    async def fake_send_defer(chat_id, tier, summary):
        sent.append((chat_id, tier, summary))
    monkeypatch.setattr(approval_tools, "send_defer_prompt", fake_send_defer)

    # Plant a stale defer row.
    db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2, summary="dispatch: edit pending",
        args={"x": 1}, deferred_tool_use_id="tu_stale",
        deferred_tool_input={"x": 1},
    )

    # Bot stub — only needs to exist; recover doesn't use it directly.
    class _Bot:
        pass
    await background_listener.recover_deferred_approvals(_Bot())

    assert len(sent) == 1
    chat_id, tier, summary = sent[0]
    assert chat_id == 12345
    assert tier == 2
    assert "still waiting" in summary.lower()
