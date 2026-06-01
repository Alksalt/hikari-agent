"""Tests for compound turn execution (Phase 6).

Verifies:
- should_extract heuristic gate (keyword + word-count threshold)
- extract_tasks JSON parsing + fallback on parse error
- run_compound_turn parallel dispatch and result concatenation
- dependency wave ordering (sequential after parallel)
- single-task shortcut path
"""
from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# should_extract tests
# ---------------------------------------------------------------------------

def test_should_extract_returns_false_for_short_message():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("check email") is False


def test_should_extract_returns_false_no_connectives():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("what is the weather in kyiv today please tell me") is False


def test_should_extract_returns_true_with_connective_and_length():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("look up the weather and also check my email please") is True


def test_should_extract_returns_true_also_keyword():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("remind me about the meeting and also set a timer for dinner") is True


def test_should_extract_returns_true_then_keyword():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("translate this phrase and then find the arxiv paper about it") is True


def test_should_extract_returns_false_below_word_threshold():
    from tools.dispatch.task_extractor import should_extract
    # 7 words — below MIN_WORDS=8
    assert should_extract("check weather and email today") is False


# ---------------------------------------------------------------------------
# stateful routing — a single extracted node and any extractor/validation
# fallback must run on run_user_turn (live session + injected memory), NEVER on
# the stateless run_internal_control. Regression for the dominant "feels dumb"
# misroute where Ukrainian chat tripped the connective gate and got a cold,
# context-free reply.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_node_routes_to_stateful_turn(monkeypatch):
    from agents import compound_turn
    from agents.work_packet import CompoundTaskNode

    async def fake_extract(text):
        return [CompoundTaskNode.from_raw_dict(
            {"task": text, "intent_type": "read", "utterance_span": [0, len(text)]},
            full_text=text,
        )]

    calls: dict = {}

    async def fake_run_user_turn(prompt):
        calls["stateful_prompt"] = prompt
        return "stateful in-character reply"

    async def fake_run_internal_control(*a, **k):
        calls["stateless"] = True
        return "cold reply"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", fake_extract)
    monkeypatch.setattr("agents.runtime.run_user_turn", fake_run_user_turn)
    monkeypatch.setattr("agents.runtime.run_internal_control", fake_run_internal_control)

    reply = await compound_turn.run_compound_turn_typed(
        "tell me one thing and also do nothing else at all here ok",
        user_turn_id="turn_test",
        internal_belief_context="REPLY-QUOTE-CTX",
    )
    assert reply == "stateful in-character reply"
    assert "stateless" not in calls, "single node must NOT use the stateless path"
    assert "REPLY-QUOTE-CTX" in calls["stateful_prompt"], "belief/reply prefix must be preserved"


@pytest.mark.asyncio
async def test_extractor_failure_falls_back_to_stateful(monkeypatch):
    from agents import compound_turn

    async def fake_extract(text):
        raise ValueError("extractor boom")

    calls: dict = {}

    async def fake_run_user_turn(prompt):
        calls["stateful_prompt"] = prompt
        return "stateful reply"

    async def fake_run_internal_control(*a, **k):
        calls["stateless"] = True
        return "cold"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", fake_extract)
    monkeypatch.setattr("agents.runtime.run_user_turn", fake_run_user_turn)
    monkeypatch.setattr("agents.runtime.run_internal_control", fake_run_internal_control)

    reply = await compound_turn.run_compound_turn_typed(
        "do this thing and also that other thing for me now please",
        user_turn_id="turn_test2",
    )
    assert reply == "stateful reply"
    assert "stateless" not in calls, "fallback must NOT use the stateless path"


# ---------------------------------------------------------------------------
# extract_tasks tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_tasks_parses_two_tasks(monkeypatch):
    from tools.dispatch import task_extractor

    async def _fake_aux_llm(prompt, *, system=""):
        return json.dumps([
            {"task": "look up the weather", "depends_on": []},
            {"task": "check email", "depends_on": []},
        ])

    monkeypatch.setattr("agents.runtime._call_aux_llm", _fake_aux_llm)
    tasks = await task_extractor.extract_tasks("look up the weather and also check email")
    assert len(tasks) == 2
    assert tasks[0]["task"] == "look up the weather"
    assert tasks[1]["task"] == "check email"
    assert tasks[0]["depends_on"] == []


@pytest.mark.asyncio
async def test_extract_tasks_fallback_on_bad_json(monkeypatch):
    from tools.dispatch import task_extractor

    async def _fake_aux_llm(prompt, *, system=""):
        return "not valid json at all"

    monkeypatch.setattr("agents.runtime._call_aux_llm", _fake_aux_llm)
    msg = "look up the weather and also check email inbox"
    tasks = await task_extractor.extract_tasks(msg)
    assert len(tasks) == 1
    assert tasks[0]["task"] == msg


@pytest.mark.asyncio
async def test_extract_tasks_strips_markdown_fences(monkeypatch):
    from tools.dispatch import task_extractor

    async def _fake_aux_llm(prompt, *, system=""):
        return '```json\n[{"task": "check weather", "depends_on": []}]\n```'

    monkeypatch.setattr("agents.runtime._call_aux_llm", _fake_aux_llm)
    tasks = await task_extractor.extract_tasks("check weather please now right")
    assert len(tasks) == 1
    assert tasks[0]["task"] == "check weather"


# ---------------------------------------------------------------------------
# run_compound_turn tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_compound_turn_single_task(monkeypatch):
    from agents.compound_turn import run_compound_turn

    calls = []

    async def _fake_ric(prompt, **_kwargs):
        calls.append(prompt)
        return f"result: {prompt}"

    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)
    result = await run_compound_turn([{"task": "check weather", "depends_on": []}])
    assert calls == ["check weather"]
    assert "check weather" in result


@pytest.mark.asyncio
async def test_run_compound_turn_parallel_independent(monkeypatch):
    """Two independent tasks should both run and results be combined."""
    from agents.compound_turn import run_compound_turn

    dispatched: list[str] = []

    async def _fake_ric(prompt, **_kwargs):
        dispatched.append(prompt)
        return f"[{prompt}]"

    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)
    tasks = [
        {"task": "weather", "depends_on": []},
        {"task": "email", "depends_on": []},
    ]
    result = await run_compound_turn(tasks)
    assert set(dispatched) == {"weather", "email"}
    assert "[weather]" in result
    assert "[email]" in result


@pytest.mark.asyncio
async def test_run_compound_turn_sequential_dependent(monkeypatch):
    """Task 1 depends on task 0; task 0 must complete first."""
    from agents.compound_turn import run_compound_turn

    order: list[str] = []

    async def _fake_ric(prompt, **_kwargs):
        order.append(prompt)
        return f"[{prompt}]"

    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)
    tasks = [
        {"task": "step-A", "depends_on": []},
        {"task": "step-B", "depends_on": [0]},
    ]
    result = await run_compound_turn(tasks)
    assert order.index("step-A") < order.index("step-B")
    assert "[step-A]" in result and "[step-B]" in result


@pytest.mark.asyncio
async def test_run_compound_turn_handles_subtask_exception(monkeypatch):
    """Partial failure: successful results returned, error text NOT surfaced to user."""
    from agents.compound_turn import run_compound_turn

    async def _fake_ric(prompt, **_kwargs):
        if prompt == "bad":
            raise RuntimeError("boom")
        return f"[{prompt}]"

    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)
    tasks = [
        {"task": "good", "depends_on": []},
        {"task": "bad", "depends_on": []},
    ]
    result = await run_compound_turn(tasks)
    assert "[good]" in result
    # Error strings must NOT be surfaced as user-facing reply.
    assert "failed" not in result
    assert "boom" not in result


@pytest.mark.asyncio
async def test_run_compound_turn_all_fail_raises(monkeypatch):
    """All tasks fail → exception propagates so outer handler sends 'brain hit a wall'."""
    from agents.compound_turn import run_compound_turn

    async def _always_fail(prompt, **_kwargs):
        raise RuntimeError("everything broke")

    monkeypatch.setattr("agents.runtime.run_internal_control", _always_fail)
    tasks = [
        {"task": "a", "depends_on": []},
        {"task": "b", "depends_on": []},
    ]
    with pytest.raises(RuntimeError, match="everything broke"):
        await run_compound_turn(tasks)


# ---------------------------------------------------------------------------
# run_compound_turn_typed (Sprint A Wave 3)
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    """Point storage.db at a fresh temp SQLite file for each test."""
    import os
    from importlib import reload
    db_path = tmp_path / "test_hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    from storage import db as _db
    reload(_db)
    yield _db
    # Cleanup
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_run_compound_turn_typed_reads_in_parallel(monkeypatch, _isolated_db):
    """Two reads → both dispatched, both done, packet status=done."""
    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode

    async def _fake_extract(message):
        return [
            CompoundTaskNode(
                intent_type="read", utterance_span=(0, 7),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="weather",
            ),
            CompoundTaskNode(
                intent_type="read", utterance_span=(8, 15),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="email",
            ),
        ]

    async def _fake_ric(prompt, **_kwargs):
        return f"[{prompt}]"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    result = await run_compound_turn_typed(
        "weather and email", user_turn_id="t1", step_timeout=5.0,
    )
    assert "[weather]" in result
    assert "[email]" in result


@pytest.mark.asyncio
async def test_run_compound_turn_typed_write_approval_conversion(monkeypatch, _isolated_db):
    """A write with risk=approve_required is converted to a CONFIRM-SEND prompt and marked waiting."""
    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode

    async def _fake_extract(message):
        return [
            CompoundTaskNode(
                intent_type="write", utterance_span=(0, 10),
                risk_class="approve_required", approval_policy="confirm_send",
                confidence=0.9, task="send email to alex",
                entities=["alex"],
            ),
        ]

    ric_calls: list[str] = []

    async def _fake_ric(prompt, **_kwargs):
        ric_calls.append(prompt)
        return "sent"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    result = await run_compound_turn_typed(
        "send email", user_turn_id="t2", step_timeout=5.0,
    )
    # Write should NOT have been dispatched; just queued for approval.
    assert ric_calls == []
    assert "CONFIRM-SEND" in result
    assert "waiting" in result.lower()


@pytest.mark.asyncio
async def test_run_compound_turn_typed_blocked_write_skipped(monkeypatch, _isolated_db):
    """A blocked write is skipped without dispatch."""
    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode

    async def _fake_extract(message):
        return [
            CompoundTaskNode(
                intent_type="write", utterance_span=(0, 10),
                risk_class="blocked", approval_policy="block",
                confidence=0.9, task="rm -rf /",
            ),
        ]

    ric_calls: list[str] = []

    async def _fake_ric(prompt, **_kwargs):
        ric_calls.append(prompt)
        return "ok"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    result = await run_compound_turn_typed(
        "wipe everything", user_turn_id="t3", step_timeout=5.0,
    )
    assert ric_calls == []
    assert "skipped" in result.lower()


@pytest.mark.asyncio
async def test_run_compound_turn_typed_falls_back_on_validation_error(monkeypatch, _isolated_db):
    """Validation error → single-LLM fallback returned to user."""
    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode

    async def _fake_extract(message):
        # Overlapping spans → validation fails
        return [
            CompoundTaskNode(
                intent_type="read", utterance_span=(0, 15),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="a",
            ),
            CompoundTaskNode(
                intent_type="read", utterance_span=(5, 20),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="b",
            ),
        ]

    fallback_called = {"hit": False}

    # Fallback is now STATEFUL (run_user_turn → session + memory), not the
    # stateless run_internal_control.
    async def _fake_run_user_turn(prompt):
        fallback_called["hit"] = True
        return f"FALLBACK[{prompt}]"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_user_turn", _fake_run_user_turn)

    result = await run_compound_turn_typed(
        "x" * 30, user_turn_id="t4", step_timeout=5.0,
    )
    assert fallback_called["hit"] is True
    assert "FALLBACK" in result


@pytest.mark.asyncio
async def test_run_compound_turn_typed_falls_back_on_extractor_error(monkeypatch, _isolated_db):
    """Extractor raises → single-LLM fallback."""
    from agents.compound_turn import run_compound_turn_typed

    async def _fake_extract(message):
        raise ValueError("bad json from llm")

    fallback_called = {"hit": False}

    # Fallback is now STATEFUL (run_user_turn → session + memory).
    async def _fake_run_user_turn(prompt):
        fallback_called["hit"] = True
        return "FALLBACK"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_user_turn", _fake_run_user_turn)

    result = await run_compound_turn_typed(
        "anything", user_turn_id="t5",
    )
    assert fallback_called["hit"] is True
    assert "FALLBACK" in result


@pytest.mark.asyncio
async def test_run_compound_turn_typed_read_step_timeout(monkeypatch, _isolated_db):
    """A read step that exceeds step_timeout is marked failed; receipt reports it."""
    import asyncio

    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode

    async def _fake_extract(message):
        return [
            CompoundTaskNode(
                intent_type="read", utterance_span=(0, 7),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="slow",
            ),
            CompoundTaskNode(
                intent_type="read", utterance_span=(8, 15),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="fast",
            ),
        ]

    async def _fake_ric(prompt, **_kwargs):
        if prompt == "slow":
            await asyncio.sleep(0.5)
            return "slow-done"
        return f"[{prompt}]"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    result = await run_compound_turn_typed(
        "x" * 20, user_turn_id="t6", step_timeout=0.05,
    )
    assert "[fast]" in result
    assert "failed" in result.lower() or "Timeout" in result


@pytest.mark.asyncio
async def test_run_compound_turn_typed_voice_lifts_flag(monkeypatch, _isolated_db):
    """is_voice=True lifts voice_uncertainty on all nodes."""
    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode

    captured_nodes: list[CompoundTaskNode] = []

    # Two nodes so this stays on the compound machinery (a single auto node now
    # short-circuits to the stateful path). is_voice must lift voice_uncertainty
    # on every node the compound planner processes.
    async def _fake_extract(message):
        nodes = [
            CompoundTaskNode(
                intent_type="read", utterance_span=(0, 7),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="weather",
                voice_uncertainty=False,  # extractor didn't flag it
            ),
            CompoundTaskNode(
                intent_type="read", utterance_span=(8, 12),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="news",
                voice_uncertainty=False,
            ),
        ]
        captured_nodes.extend(nodes)
        return nodes

    async def _fake_ric(prompt, **_kwargs):
        return f"[{prompt}]"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    await run_compound_turn_typed(
        "weather news", user_turn_id="tV", is_voice=True, step_timeout=5.0,
    )
    assert all(n.voice_uncertainty is True for n in captured_nodes)


# ---------------------------------------------------------------------------
# compound provenance — child step rows in DB before receipt composed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_compound_turn_typed_child_steps_persisted_before_receipt(
    monkeypatch, _isolated_db
):
    """Child tool_calls (work_packet_steps) are written to DB while the packet
    is still running, so any post-processing (like anti-fabrication checks)
    can query them against the parent context.

    After run_compound_turn_typed completes:
      - The work_packets row exists with status 'done'.
      - Both child work_packet_steps rows exist with status 'done'.
      - Each step's input_json contains the node's intent_type / task.
      - The packet's user_turn_id matches what was passed in.
    """
    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode
    from storage import db as _db

    async def _fake_extract(message):
        return [
            CompoundTaskNode(
                intent_type="read", utterance_span=(0, 8),
                risk_class="safe", approval_policy="auto",
                confidence=0.9, task="fetch calendar",
            ),
            CompoundTaskNode(
                intent_type="search", utterance_span=(9, 20),
                risk_class="safe", approval_policy="auto",
                confidence=0.85, task="find flights",
            ),
        ]

    async def _fake_ric(prompt, **_kwargs):
        return f"result:{prompt}"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    turn_id = "prov-test-t1"
    await run_compound_turn_typed(
        "check calendar and find flights", user_turn_id=turn_id, step_timeout=5.0,
    )

    # Verify the packet row exists.
    with _db._conn() as c:
        packet_row = c.execute(
            "SELECT * FROM work_packets WHERE user_turn_id = ?", (turn_id,)
        ).fetchone()
    assert packet_row is not None, "work_packets row must exist after typed run"
    assert packet_row["status"] == "done"

    packet_id = packet_row["id"]
    steps = _db.work_packet_steps(packet_id)
    assert len(steps) == 2, f"expected 2 child steps, got {len(steps)}"

    # Both steps should be done.
    assert all(s["status"] == "done" for s in steps), (
        f"expected all steps done, got {[s['status'] for s in steps]}"
    )

    # Each step's input_json must contain the node's intent data.
    import json
    for step in steps:
        node_data = json.loads(step["input_json"])
        assert node_data["intent_type"] in ("read", "search")
        assert node_data["task"] in ("fetch calendar", "find flights")


@pytest.mark.asyncio
async def test_run_compound_turn_typed_approve_required_step_is_waiting_in_db(
    monkeypatch, _isolated_db
):
    """After an approve_required write: the step row in DB has status='waiting'
    (not 'done' or 'pending') before the receipt is composed.
    This guarantees the parent packet view reflects the child's blocked state.
    """
    from agents.compound_turn import run_compound_turn_typed
    from agents.work_packet import CompoundTaskNode
    from storage import db as _db

    async def _fake_extract(message):
        return [
            CompoundTaskNode(
                intent_type="write", utterance_span=(0, 12),
                risk_class="approve_required", approval_policy="confirm_send",
                confidence=0.95, task="send invoice",
                entities=["invoice"],
            ),
        ]

    async def _fake_ric(prompt, **_kwargs):
        return "sent"

    monkeypatch.setattr("tools.dispatch.task_extractor.extract_typed_nodes", _fake_extract)
    monkeypatch.setattr("agents.runtime.run_internal_control", _fake_ric)

    turn_id = "prov-approve-t2"
    await run_compound_turn_typed(
        "send invoice to client", user_turn_id=turn_id, step_timeout=5.0,
    )

    with _db._conn() as c:
        packet_row = c.execute(
            "SELECT * FROM work_packets WHERE user_turn_id = ?", (turn_id,)
        ).fetchone()
    assert packet_row is not None
    # Packet itself is in 'waiting' state (has a waiting step).
    assert packet_row["status"] == "waiting"

    steps = _db.work_packet_steps(packet_row["id"])
    assert len(steps) == 1
    assert steps[0]["status"] == "waiting", (
        f"approve_required step must be 'waiting' in DB, got {steps[0]['status']!r}"
    )
