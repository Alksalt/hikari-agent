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
async def test_resume_after_defer_identity_fallback_when_no_mapping(monkeypatch, tmp_path):
    """When no defer_confirmed_tools entry exists for the gated tool, the resume
    falls through to identity: it calls run_internal_control with the original
    tool name in the synthesized prompt, sets the IN_APPROVAL_RESUME_TOOL
    contextvar so the PreToolUse defer hook bypasses for this tool only, and
    does NOT extend extra_allowed_tools (the tool is already in the base
    allowlist via wildcard).

    Was previously codified as 'aborts cleanly' — that was the bug surfaced by
    the live gmail_bulk_delete_messages CONFIRM-SEND test: 16 destructive tools
    were never executable on approval because each lacked a sibling mapping.
    """
    # Tweak config so defer_confirmed_tools is empty.
    cfg_text = (
        "approvals:\n"
        "  defer_confirmed_tools: {}\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import hooks
    from tools import approvals as approval_tools

    sent: list[tuple[int, str]] = []

    async def fake_safe_send(chat_id, text):
        sent.append((chat_id, text))
    monkeypatch.setattr(approval_tools, "_safe_send", fake_safe_send)

    captured: dict = {}

    async def fake_run_internal_control(prompt, *, max_turns, max_budget_usd,
                                        extra_allowed_tools=None):
        captured["prompt"] = prompt
        captured["extra_allowed_tools"] = extra_allowed_tools
        # The contextvar must be set to the gated tool name at the moment the
        # SDK call runs — that's how the PreToolUse defer hook knows to skip.
        captured["contextvar"] = hooks.IN_APPROVAL_RESUME_TOOL.get()
        return "deleted."

    import agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "run_internal_control",
                        fake_run_internal_control)

    aid = db.approval_create_deferred(
        chat_id=12345,
        tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        tier=2, summary="bulk delete 11 messages",
        args={"message_ids": ["a", "b", "c"]},
        deferred_tool_use_id="tu_gmail_bulk",
        deferred_tool_input={"message_ids": ["a", "b", "c"]},
    )
    pending = db.approval_pending_for(12345)
    consumed = await approval_tools._resume_after_defer(aid, pending)
    assert consumed is True
    # The SDK call was made (no abort).
    assert "prompt" in captured
    # No "_confirmed" sibling — the prompt instructs the model to call the
    # original tool name directly.
    assert "gmail_bulk_delete_messages" in captured["prompt"]
    # extra_allowed_tools is None: the tool is already in the base allowlist
    # via the mcp__google_workspace__* wildcard; passing it would also
    # incorrectly trigger the dispatch-confirmed server attachment heuristic
    # in _build_options.
    assert captured["extra_allowed_tools"] is None
    # Contextvar was set to the exact tool name being resumed.
    assert captured["contextvar"] == (
        "mcp__google_workspace__gmail_bulk_delete_messages"
    )
    # And it's been reset back to None by the time the resume returns.
    assert hooks.IN_APPROVAL_RESUME_TOOL.get() is None
    # No abort message was sent.
    assert not any("no confirmed-tool mapping" in t for _, t in sent)


@pytest.mark.asyncio
async def test_defer_hook_bypasses_when_in_approval_resume(monkeypatch):
    """When IN_APPROVAL_RESUME_TOOL is set to the tool being called, the
    PreToolUse defer hook short-circuits — that's how the resume turn can
    actually execute the gated tool without looping the defer."""
    from agents import hooks

    token = hooks.IN_APPROVAL_RESUME_TOOL.set(
        "mcp__google_workspace__gmail_bulk_delete_messages",
    )
    try:
        out = await hooks.defer_gated_tools(
            {"tool_name": "mcp__google_workspace__gmail_bulk_delete_messages",
             "tool_use_id": "tu_resume",
             "tool_input": {"message_ids": ["a"]}},
            None, None,
        )
        # Empty dict = "don't defer, let the SDK run the tool."
        assert out == {}
        # No approval row created during the bypass.
        assert db.approval_pending_for(12345) is None
    finally:
        hooks.IN_APPROVAL_RESUME_TOOL.reset(token)


@pytest.mark.asyncio
async def test_defer_hook_still_defers_other_tools_during_approval_resume(monkeypatch):
    """Bypass is scoped to the EXACT tool name in IN_APPROVAL_RESUME_TOOL. If
    the resume turn's model decides to call a *different* gated tool, that
    tool still defers normally — the user shouldn't lose approval coverage
    on unrelated destructive operations just because one approval is mid-flight.
    """
    from agents import hooks
    from tools import approvals as approval_tools

    async def fake_send(chat_id, tier, summary):
        pass
    monkeypatch.setattr(approval_tools, "send_defer_prompt", fake_send)

    token = hooks.IN_APPROVAL_RESUME_TOOL.set(
        "mcp__google_workspace__gmail_bulk_delete_messages",
    )
    try:
        # Different gated tool — must defer.
        out = await hooks.defer_gated_tools(
            {"tool_name": "mcp__google_workspace__drive_delete_file",
             "tool_use_id": "tu_other",
             "tool_input": {"file_id": "xyz"}},
            None, None,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "defer"
    finally:
        hooks.IN_APPROVAL_RESUME_TOOL.reset(token)


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
    # Phase 8: only CONFIRM-SEND triggers; a stray "y" should NOT consume
    # (returns False so message routes to agent), but for deferred rows it now
    # also implicit-cancels the pending row (Task 4 behavior).
    handled_y = await approval_tools.resolve_pending_approval(12345, "y")
    assert handled_y is False
    assert routed["resume"] == 0
    # The deferred row was implicit-cancelled by "y" — create a fresh row
    # before exercising the CONFIRM-SEND routing path.
    db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__hikari_dispatch__dispatch_claude_session",
        tier=2, summary="...",
        args={}, deferred_tool_use_id="tu_routed_2", deferred_tool_input={},
    )
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


# ---------- Task 1: _queue_cancel_tool_use helper ----------

def test_queue_cancel_tool_use_appends_and_dedups(tmp_path, monkeypatch):
    """`_queue_cancel_tool_use` writes the id into runtime_state under
    `cancelled_tool_use_ids` (JSON list), is dedup-safe on repeat, and is a
    no-op on falsy input."""
    import json
    from tools import approvals as approval_tools
    from storage import db

    # Pristine state
    db.runtime_set("cancelled_tool_use_ids", None)

    approval_tools._queue_cancel_tool_use("toolu_a")
    approval_tools._queue_cancel_tool_use("toolu_b")
    approval_tools._queue_cancel_tool_use("toolu_a")  # dedup
    approval_tools._queue_cancel_tool_use("")          # no-op
    approval_tools._queue_cancel_tool_use(None)        # no-op

    raw = db.runtime_get("cancelled_tool_use_ids")
    assert raw is not None
    assert json.loads(raw) == ["toolu_a", "toolu_b"]


# ---------- Task 2: defer hook honors cancel queue ----------

async def test_defer_hook_returns_deny_when_id_in_cancel_queue(monkeypatch, tmp_path):
    """`defer_gated_tools` returns permissionDecision='deny' for a tool_use_id
    already enqueued in `cancelled_tool_use_ids`. The id is popped (one-shot);
    a second call with the same id would defer again."""
    import json
    from agents import hooks
    from storage import db
    from tools import approvals as approval_tools

    # OWNER_TELEGRAM_ID is required by the legacy defer path; the deny branch
    # short-circuits before that lookup, but set it to be safe.
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    db.runtime_set(approval_tools.CANCEL_QUEUE_KEY, json.dumps(["toolu_stuck"]))

    out = await hooks.defer_gated_tools(
        {
            "tool_name": "mcp__google_workspace__gmail_bulk_delete_messages",
            "tool_input": {"message_ids": ["x"]},
            "tool_use_id": "toolu_stuck",
        },
        None, None,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    # Queue is now empty
    raw = db.runtime_get(approval_tools.CANCEL_QUEUE_KEY)
    assert json.loads(raw or "[]") == []

    # A fresh defer-attempt with the same id (after pop) defers normally again
    out2 = await hooks.defer_gated_tools(
        {
            "tool_name": "mcp__google_workspace__gmail_bulk_delete_messages",
            "tool_input": {"message_ids": ["x"]},
            "tool_use_id": "toolu_stuck",
        },
        None, None,
    )
    assert out2["hookSpecificOutput"]["permissionDecision"] == "defer"


# ---------- Task 3: timeout watcher enqueues ----------

async def test_timeout_watcher_enqueues_deferred_tool_use_id(monkeypatch):
    """When `_timeout_watcher` fires on a deferred approval, the
    `deferred_tool_use_id` is appended to `cancelled_tool_use_ids` so the next
    user turn unsticks the SDK session."""
    import json
    import asyncio
    from storage import db
    from tools import approvals as approval_tools

    # Reset queue + create a deferred row
    db.runtime_set(approval_tools.CANCEL_QUEUE_KEY, None)
    aid = db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        tier=2, summary="x", args={},
        deferred_tool_use_id="toolu_timed_out",
        deferred_tool_input={},
    )

    # Stub the Telegram send and shorten the timeout
    async def _noop_send(*a, **kw):
        return
    monkeypatch.setattr(approval_tools, "_safe_send", _noop_send)
    monkeypatch.setattr(approval_tools, "_timeout_sec", lambda: 0)

    await approval_tools._timeout_watcher(aid, 12345)

    # Approval row is terminal, and the cancel queue has the id
    pending = db.approval_pending_for(12345)
    assert pending is None
    raw = db.runtime_get(approval_tools.CANCEL_QUEUE_KEY)
    assert json.loads(raw or "[]") == ["toolu_timed_out"]


# ---------- Task 4: reject phrase enqueues + implicit-cancel ----------

async def test_resolve_pending_approval_rejected_enqueues(monkeypatch):
    """User typing a reject phrase ('cancel'/'stop'/'abort') enqueues the
    deferred tool_use_id so the next user turn unsticks the SDK session."""
    import json
    from storage import db
    from tools import approvals as approval_tools

    db.runtime_set(approval_tools.CANCEL_QUEUE_KEY, None)
    db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        tier=2, summary="x", args={},
        deferred_tool_use_id="toolu_rejected",
        deferred_tool_input={},
    )

    async def _noop_send(*a, **kw):
        return
    monkeypatch.setattr(approval_tools, "_safe_send", _noop_send)

    consumed = await approval_tools.resolve_pending_approval(12345, "cancel")
    assert consumed is True
    raw = db.runtime_get(approval_tools.CANCEL_QUEUE_KEY)
    assert json.loads(raw or "[]") == ["toolu_rejected"]


async def test_resolve_pending_approval_implicit_cancel_on_unrelated_message(monkeypatch):
    """A non-CONFIRM/non-reject message while a deferred approval is pending
    implicitly cancels: enqueues the tool_use_id, marks the row rejected,
    sends a short ack, and returns False so the message still routes to the
    agent normally."""
    import json
    from storage import db
    from tools import approvals as approval_tools

    db.runtime_set(approval_tools.CANCEL_QUEUE_KEY, None)
    db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        tier=2, summary="x", args={},
        deferred_tool_use_id="toolu_abandoned",
        deferred_tool_input={},
    )

    sent: list[tuple[int, str]] = []
    async def _capture(chat_id, text):
        sent.append((chat_id, text))
    monkeypatch.setattr(approval_tools, "_safe_send", _capture)

    consumed = await approval_tools.resolve_pending_approval(
        12345, "what do you think about vyshyvanka?",
    )
    # Not consumed — the message still routes to the agent
    assert consumed is False
    # Row is terminal
    assert db.approval_pending_for(12345) is None
    # Queue holds the id
    raw = db.runtime_get(approval_tools.CANCEL_QUEUE_KEY)
    assert json.loads(raw or "[]") == ["toolu_abandoned"]
    # User got exactly one short ack
    assert len(sent) == 1
    assert "dropping" in sent[0][1].lower() or "moving on" in sent[0][1].lower()


# ---------- Task 5: resume-after-defer enqueues on success + failure ----------

async def test_resume_after_defer_enqueues_on_success(monkeypatch, tmp_path):
    """On successful resume (the stateless side channel ran the tool), the
    deferred_tool_use_id is enqueued so the live session's dangling tool_use
    gets denied on its next attempt."""
    import json
    from storage import db
    from tools import approvals as approval_tools

    db.runtime_set(approval_tools.CANCEL_QUEUE_KEY, None)
    aid = db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        tier=2, summary="x", args={},
        deferred_tool_use_id="toolu_resume_ok",
        deferred_tool_input={"message_ids": ["a"]},
    )
    pending = db.approval_pending_for(12345)

    async def _fake_run_internal_control(*a, **kw):
        return "ok done"
    monkeypatch.setattr(
        "agents.runtime.run_internal_control", _fake_run_internal_control,
    )

    # Stub out the bridge choreography to avoid needing a live Bot
    async def _stub_choreo(bot, chat_id, text):
        return
    monkeypatch.setattr(
        "agents.telegram_bridge._send_text_with_choreography", _stub_choreo,
    )

    class _DummyBot:
        pass
    monkeypatch.setattr(approval_tools, "_bot", lambda: _DummyBot())

    consumed = await approval_tools._resume_after_defer(aid, pending)
    assert consumed is True
    raw = db.runtime_get(approval_tools.CANCEL_QUEUE_KEY)
    assert json.loads(raw or "[]") == ["toolu_resume_ok"]


async def test_resume_after_defer_enqueues_on_failure(monkeypatch, tmp_path):
    """On failed resume (the side-channel SDK call raised), the
    deferred_tool_use_id is STILL enqueued — we don't want a failed execute
    to leave the live session stuck either."""
    import json
    from storage import db
    from tools import approvals as approval_tools

    db.runtime_set(approval_tools.CANCEL_QUEUE_KEY, None)
    aid = db.approval_create_deferred(
        chat_id=12345, tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        tier=2, summary="x", args={},
        deferred_tool_use_id="toolu_resume_fail",
        deferred_tool_input={"message_ids": ["a"]},
    )
    pending = db.approval_pending_for(12345)

    async def _boom(*a, **kw):
        raise RuntimeError("simulated SDK failure")
    monkeypatch.setattr(
        "agents.runtime.run_internal_control", _boom,
    )
    async def _noop_send(*a, **kw):
        return
    monkeypatch.setattr(approval_tools, "_safe_send", _noop_send)

    await approval_tools._resume_after_defer(aid, pending)
    raw = db.runtime_get(approval_tools.CANCEL_QUEUE_KEY)
    assert json.loads(raw or "[]") == ["toolu_resume_fail"]


# ---------------------------------------------------------------------------
# Task 6: probe_gmail_bulk_delete_scope_ok
# ---------------------------------------------------------------------------


async def test_probe_gmail_bulk_delete_scope_ok_caches_true(monkeypatch):
    """When the access_token's scope list includes the full Gmail scope,
    `probe_gmail_bulk_delete_scope_ok` returns True and caches the result."""
    from storage import db
    from tools import approvals as approval_tools

    # Clear cache
    db.runtime_set("gmail_bulk_delete_scope_ok", None)
    db.runtime_set("gmail_bulk_delete_scope_checked_at", None)

    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "rtok")

    class _StubResp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    class _StubAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            assert "oauth2.googleapis.com/token" in url
            return _StubResp({"access_token": "atok", "expires_in": 3600})

        async def get(self, url, params=None, **kw):
            assert "tokeninfo" in url
            return _StubResp({"scope": (
                "https://mail.google.com/ "
                "https://www.googleapis.com/auth/calendar"
            )})

    monkeypatch.setattr(
        "tools.approvals.httpx.AsyncClient", _StubAsyncClient,
    )

    ok = await approval_tools.probe_gmail_bulk_delete_scope_ok()
    assert ok is True
    assert db.runtime_get("gmail_bulk_delete_scope_ok") == "true"


async def test_probe_gmail_bulk_delete_scope_ok_false_when_scope_missing(monkeypatch):
    """When the access_token's scope list is gmail.modify only,
    `probe_gmail_bulk_delete_scope_ok` returns False (modify doesn't include
    batchDelete)."""
    from storage import db
    from tools import approvals as approval_tools

    db.runtime_set("gmail_bulk_delete_scope_ok", None)
    db.runtime_set("gmail_bulk_delete_scope_checked_at", None)

    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "rtok")

    class _StubResp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    class _StubAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            return _StubResp({"access_token": "atok", "expires_in": 3600})

        async def get(self, url, params=None, **kw):
            return _StubResp({"scope": (
                "https://www.googleapis.com/auth/gmail.modify "
                "https://www.googleapis.com/auth/calendar"
            )})

    monkeypatch.setattr(
        "tools.approvals.httpx.AsyncClient", _StubAsyncClient,
    )

    ok = await approval_tools.probe_gmail_bulk_delete_scope_ok()
    assert ok is False
    assert db.runtime_get("gmail_bulk_delete_scope_ok") == "false"


async def test_probe_gmail_bulk_delete_scope_ok_returns_cached_value(monkeypatch):
    """Within the 24h TTL, the probe returns the cached value without hitting
    the network."""
    from datetime import UTC, datetime

    from storage import db
    from tools import approvals as approval_tools

    db.runtime_set("gmail_bulk_delete_scope_ok", "true")
    db.runtime_set(
        "gmail_bulk_delete_scope_checked_at",
        datetime.now(UTC).isoformat(),
    )

    # No network stub — if it tried to hit the wire the test would hang/fail
    ok = await approval_tools.probe_gmail_bulk_delete_scope_ok()
    assert ok is True


# ---------------------------------------------------------------------------
# Task 7: compose_email_message scope gate
# ---------------------------------------------------------------------------


async def test_compose_email_message_skips_delete_proposal_when_scope_missing(
    monkeypatch,
):
    """When `probe_gmail_bulk_delete_scope_ok()` returns False, the morning
    digest omits the 'nuke them?' delete line even if deletable_count > 0."""
    from agents import daily_checkin

    async def _no_scope():
        return False

    monkeypatch.setattr(
        "agents.daily_checkin.probe_gmail_bulk_delete_scope_ok",
        _no_scope,
        raising=False,
    )

    # Stub voice composer to capture the prompt the LLM would see
    captured_prompts: list[str] = []

    async def _capture(prompt):
        captured_prompts.append(prompt)
        return "ok"

    monkeypatch.setattr(daily_checkin, "_compose", _capture)

    data = {
        "unread_personal": [],
        "calendar_invites": [],
        "deletable": {"count": 42, "top_senders": ["spam.example"], "sample_ids": ["x"]},
    }
    await daily_checkin.compose_email_message(data)
    assert len(captured_prompts) == 1
    p = captured_prompts[0]
    assert "deletable" not in p.lower()
    assert "nuke" not in p.lower()


async def test_compose_email_message_keeps_delete_proposal_when_scope_ok(monkeypatch):
    """When the probe returns True, the existing delete-proposal flow is
    untouched."""
    from agents import daily_checkin

    async def _ok_scope():
        return True

    monkeypatch.setattr(
        "agents.daily_checkin.probe_gmail_bulk_delete_scope_ok",
        _ok_scope,
        raising=False,
    )
    captured: list[str] = []

    async def _capture(prompt):
        captured.append(prompt)
        return "ok"

    monkeypatch.setattr(daily_checkin, "_compose", _capture)

    data = {
        "unread_personal": [],
        "calendar_invites": [],
        "deletable": {"count": 42, "top_senders": ["spam.example"], "sample_ids": ["x"]},
    }
    await daily_checkin.compose_email_message(data)
    p = captured[0]
    assert "deletable" in p.lower()
    assert "nuke" in p.lower()
