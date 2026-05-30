"""Phase 7 — Compound-turn child tool-call aggregation tests.

Verifies that child tool names collected by run_internal_control inside a
compound turn are merged into the parent's LAST_TURN_TOOL_NAMES ContextVar
BEFORE post_filter's fabrication backstop runs, so real inbox/calendar
receipts produced in child turns survive filter_outgoing unchanged.

Four cases:
  1. child tool call reaches parent LAST_TURN_TOOL_NAMES after typed run.
  2. inbox-shaped text from a real child fetch is NOT replaced by filter_outgoing.
  3. inbox-shaped text with NO child tool call IS still replaced (no regression).
  4. parallel reads with distinct tool names both land in the sink (race safety).
"""
from __future__ import annotations

import os
import pytest


# ---------------------------------------------------------------------------
# Shared DB fixture (mirrors tests/test_compound_turn.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    """Point storage.db at a fresh temp SQLite file for each test."""
    from importlib import reload
    db_path = tmp_path / "test_hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    from storage import db as _db
    reload(_db)
    yield _db
    try:
        os.unlink(db_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Test 1 — child tool call reaches parent after run_compound_turn_typed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_child_tool_call_reaches_parent_last_turn_tool_names(
    monkeypatch, _isolated_db
):
    """After run_compound_turn_typed, LAST_TURN_TOOL_NAMES contains the tool
    name that a child run_internal_control reported via the sink."""
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.compound_turn import run_compound_turn_typed

    CHILD_TOOL = "mcp__google_workspace__query_gmail_emails"

    async def _fake_ric(prompt, *, tool_names_sink=None, **kwargs):
        # Simulate child SDK turn: report a tool call via the sink.
        if tool_names_sink is not None:
            tool_names_sink.add(CHILD_TOOL)
        return "5 unread — all newsletters"

    async def _fake_extract(message):
        from agents.work_packet import CompoundTaskNode
        # Two nodes so the turn stays on the compound machinery — a single auto
        # node now short-circuits to the stateful run_user_turn path. The child
        # sink aggregation under test only runs for genuine multi-task packets.
        return [
            CompoundTaskNode(intent_type="read", utterance_span=(0, 5),
                             risk_class="safe", approval_policy="auto",
                             confidence=0.9, task="fetch inbox"),
            CompoundTaskNode(intent_type="read", utterance_span=(6, 11),
                             risk_class="safe", approval_policy="auto",
                             confidence=0.9, task="check labels"),
        ]

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    # Reset contextvar so we're not inheriting state from another test.
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        await run_compound_turn_typed(
            "fetch inbox and labels", user_turn_id="agg-t1", step_timeout=5.0,
        )
        tool_names = LAST_TURN_TOOL_NAMES.get()
        assert CHILD_TOOL in tool_names, (
            f"Expected {CHILD_TOOL!r} in LAST_TURN_TOOL_NAMES, got {tool_names!r}"
        )
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


# ---------------------------------------------------------------------------
# Test 2 — real inbox fetch survives filter_outgoing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_inbox_fetch_survives_filter_outgoing(monkeypatch, _isolated_db):
    """After run_compound_turn_typed where child reports a gmail tool call,
    filter_outgoing must NOT replace the inbox-shaped receipt text."""
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.compound_turn import run_compound_turn_typed
    from agents.post_filter import _FABRICATION_REPLACEMENT

    CHILD_TOOL = "mcp__google_workspace__query_gmail_emails"
    REPLY_TEXT = "5 unread, all from Google"

    async def _fake_ric(prompt, *, tool_names_sink=None, **kwargs):
        if tool_names_sink is not None:
            tool_names_sink.add(CHILD_TOOL)
        return REPLY_TEXT

    async def _fake_extract(message):
        from agents.work_packet import CompoundTaskNode
        # Two nodes so the turn stays on the compound machinery — a single auto
        # node now short-circuits to the stateful run_user_turn path (which in
        # production populates LAST_TURN_TOOL_NAMES itself). The compound child
        # sink aggregation under test only runs for genuine multi-task packets.
        return [
            CompoundTaskNode(intent_type="read", utterance_span=(0, 5),
                             risk_class="safe", approval_policy="auto",
                             confidence=0.9, task="check gmail"),
            CompoundTaskNode(intent_type="read", utterance_span=(6, 11),
                             risk_class="safe", approval_policy="auto",
                             confidence=0.9, task="check labels"),
        ]

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        await run_compound_turn_typed(
            "check gmail and labels", user_turn_id="agg-t2", step_timeout=5.0,
        )

        # Now call filter_outgoing in the SAME context — backstop should pass.
        # We need to import filter_outgoing; its async side-effects (aux LLM)
        # are patched out via the dummy monkeypatch on _call_aux_llm if needed.
        # For the fabrication backstop specifically (sync), no LLM needed.
        from agents.post_filter import _strip_fabricated_external_data
        _text, fired, reason = _strip_fabricated_external_data(REPLY_TEXT)
        assert fired is False, (
            f"Fabrication backstop fired on a real fetch — got reason={reason!r}. "
            f"LAST_TURN_TOOL_NAMES={LAST_TURN_TOOL_NAMES.get()!r}"
        )
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


# ---------------------------------------------------------------------------
# Test 3 — fabrication WITHOUT a child tool call is still stripped
# ---------------------------------------------------------------------------

def test_fabrication_without_child_tool_call_is_stripped():
    """If the child did NOT touch the sink, the backstop still fires on
    inbox-shaped text. Guards against over-suppression."""
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.post_filter import _strip_fabricated_external_data, _FABRICATION_REPLACEMENT

    # Empty tool names — no fetch happened.
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        text, fired, reason = _strip_fabricated_external_data(
            "5 unread, all from Google"
        )
        assert fired is True, (
            "Fabrication backstop must fire when no inbox fetch tool was called"
        )
        assert text == _FABRICATION_REPLACEMENT
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


# ---------------------------------------------------------------------------
# Test 4 — parallel reads, both tool names land in sink
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parallel_reads_both_tool_names_in_sink(monkeypatch, _isolated_db):
    """Two parallel read nodes, each fake adds a distinct tool name to the sink.
    Both must be present in LAST_TURN_TOOL_NAMES after run_compound_turn_typed."""
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.compound_turn import run_compound_turn_typed

    TOOL_A = "mcp__google_workspace__query_gmail_emails"
    TOOL_B = "mcp__google_workspace__calendar_get_events"

    user_text = "fetch inbox and calendar"  # 24 chars

    async def _fake_extract(message):
        from agents.work_packet import CompoundTaskNode
        return [
            CompoundTaskNode(
                intent_type="read", utterance_span=(0, 11),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="fetch inbox",
            ),
            CompoundTaskNode(
                intent_type="read", utterance_span=(16, 24),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="fetch calendar",
            ),
        ]

    async def _fake_ric(prompt, *, tool_names_sink=None, **kwargs):
        if tool_names_sink is not None:
            if "inbox" in prompt:
                tool_names_sink.add(TOOL_A)
            elif "calendar" in prompt:
                tool_names_sink.add(TOOL_B)
        return f"result: {prompt}"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        await run_compound_turn_typed(
            user_text, user_turn_id="agg-t4", step_timeout=5.0,
        )
        tool_names = LAST_TURN_TOOL_NAMES.get()
        assert TOOL_A in tool_names, f"{TOOL_A!r} missing from sink: {tool_names!r}"
        assert TOOL_B in tool_names, f"{TOOL_B!r} missing from sink: {tool_names!r}"
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


# ---------------------------------------------------------------------------
# Test 5 — legacy run_compound_turn sink works for multi-task path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_run_compound_turn_sink_multi_task(monkeypatch):
    """Legacy run_compound_turn (multi-task) also aggregates child tool names."""
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.compound_turn import run_compound_turn

    CHILD_TOOL = "mcp__google_workspace__query_gmail_emails"

    async def _fake_ric(prompt, *, tool_names_sink=None, **kwargs):
        if tool_names_sink is not None:
            tool_names_sink.add(CHILD_TOOL)
        return f"[{prompt}]"

    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        await run_compound_turn([
            {"task": "inbox", "depends_on": []},
            {"task": "calendar", "depends_on": []},
        ])
        tool_names = LAST_TURN_TOOL_NAMES.get()
        assert CHILD_TOOL in tool_names, (
            f"Expected {CHILD_TOOL!r} in LAST_TURN_TOOL_NAMES, got {tool_names!r}"
        )
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)
