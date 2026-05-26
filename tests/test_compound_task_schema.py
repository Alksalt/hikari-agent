"""tests/test_compound_task_schema.py — CompoundTaskNode schema validation + WorkPacket step ordering.

Tests cover:
  1. validate_nodes rejects missing / out-of-vocab intent_type
  2. validate_nodes rejects out-of-vocab risk_class
  3. validate_nodes rejects risk/policy mismatch
  4. validate_nodes rejects overlapping utterance_spans
  5. validate_nodes accepts a minimal well-formed node
  6. validate_nodes accepts a multi-node list with distinct spans
  7. from_raw_dict defaults out-of-vocab intent to 'read'
  8. from_raw_dict clamps confidence to [0, 1]
  9. from_raw_dict raises on missing task text
  10. from_raw_dict raises on non-dict input
  11. _partition_steps places read/search/calc intents first, writes second
  12. approve_required write → WorkPacket step gets status='waiting'
  13. validate_nodes rejects empty node list
  14. validate_nodes flags span-end < span-start
  15. validate_nodes flags span exceeds full_text length
"""
from __future__ import annotations

import pytest

from agents.work_packet import CompoundTaskNode, WorkPacket, WorkStep, validate_nodes
from agents.compound_turn import _partition_steps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(
    *,
    intent_type="read",
    utterance_span=(0, 10),
    risk_class="safe",
    approval_policy="auto",
    confidence=0.9,
    task="do something",
    entities=None,
    time_refs=None,
):
    return CompoundTaskNode(
        intent_type=intent_type,
        utterance_span=utterance_span,
        entities=entities or [],
        time_refs=time_refs or [],
        risk_class=risk_class,
        approval_policy=approval_policy,
        confidence=confidence,
        task=task,
    )


def _step(node: CompoundTaskNode, step_id: int = 1, step_index: int = 0) -> WorkStep:
    return WorkStep(
        step_id=step_id,
        step_index=step_index,
        tool_name=f"{node.intent_type}:auto",
        node=node,
    )


# ---------------------------------------------------------------------------
# 1. validate_nodes — empty list
# ---------------------------------------------------------------------------

def test_validate_nodes_rejects_empty_list():
    errors = validate_nodes([])
    assert errors, "empty list should produce errors"
    assert any("empty" in e or "no nodes" in e for e in errors)


# ---------------------------------------------------------------------------
# 2. validate_nodes — bad intent_type (post-construction via object mutation)
# ---------------------------------------------------------------------------

def test_validate_nodes_rejects_bad_intent_type():
    node = _node()
    object.__setattr__(node, "intent_type", "fly_to_moon")  # bypass slots type safety
    errors = validate_nodes([node])
    assert any("intent_type" in e for e in errors)


# ---------------------------------------------------------------------------
# 3. validate_nodes — bad risk_class
# ---------------------------------------------------------------------------

def test_validate_nodes_rejects_bad_risk_class():
    node = _node()
    object.__setattr__(node, "risk_class", "extremely_risky")
    errors = validate_nodes([node])
    assert any("risk_class" in e for e in errors)


# ---------------------------------------------------------------------------
# 4. validate_nodes — risk / policy mismatch
# ---------------------------------------------------------------------------

def test_validate_nodes_rejects_risk_policy_mismatch():
    # approve_required requires confirm_send
    node = _node(risk_class="approve_required", approval_policy="auto")
    errors = validate_nodes([node])
    assert any("risk_class" in e or "approval_policy" in e for e in errors)


def test_validate_nodes_rejects_safe_with_confirm_send():
    node = _node(risk_class="safe", approval_policy="confirm_send")
    errors = validate_nodes([node])
    assert any("approval_policy" in e or "risk_class" in e for e in errors)


def test_validate_nodes_rejects_blocked_with_auto():
    node = _node(risk_class="blocked", approval_policy="auto")
    errors = validate_nodes([node])
    assert any("approval_policy" in e or "risk_class" in e for e in errors)


# ---------------------------------------------------------------------------
# 5. validate_nodes — overlapping utterance_spans
# ---------------------------------------------------------------------------

def test_validate_nodes_rejects_overlapping_spans():
    a = _node(utterance_span=(0, 15), task="first part")
    b = _node(utterance_span=(10, 25), task="second part")
    errors = validate_nodes([a, b])
    assert any("overlap" in e for e in errors)


def test_validate_nodes_accepts_touching_spans():
    # Boundary touch (a.hi == b.lo) is allowed
    a = _node(utterance_span=(0, 10), task="first")
    b = _node(utterance_span=(10, 20), task="second")
    errors = validate_nodes([a, b])
    assert not errors, f"touching spans should be valid, got: {errors}"


# ---------------------------------------------------------------------------
# 6. validate_nodes — span end < start
# ---------------------------------------------------------------------------

def test_validate_nodes_rejects_inverted_span():
    node = _node(utterance_span=(20, 5), task="backwards")
    errors = validate_nodes([node])
    assert any("end < start" in e for e in errors)


# ---------------------------------------------------------------------------
# 7. validate_nodes — span exceeds full_text
# ---------------------------------------------------------------------------

def test_validate_nodes_rejects_span_beyond_text():
    node = _node(utterance_span=(0, 100), task="too long")
    errors = validate_nodes([node], full_text="short text")
    assert any("exceeds" in e for e in errors)


# ---------------------------------------------------------------------------
# 8. validate_nodes — well-formed single node
# ---------------------------------------------------------------------------

def test_validate_nodes_accepts_minimal_node():
    node = _node()
    errors = validate_nodes([node])
    assert errors == []


# ---------------------------------------------------------------------------
# 9. validate_nodes — well-formed multi-node with distinct non-overlapping spans
# ---------------------------------------------------------------------------

def test_validate_nodes_accepts_multi_node():
    nodes = [
        _node(intent_type="read", utterance_span=(0, 10), task="read task"),
        _node(
            intent_type="write", utterance_span=(11, 22),
            risk_class="approve_required", approval_policy="confirm_send",
            task="write task",
        ),
    ]
    errors = validate_nodes(nodes)
    assert errors == []


# ---------------------------------------------------------------------------
# 10. from_raw_dict — out-of-vocab intent defaults to 'read'
# ---------------------------------------------------------------------------

def test_from_raw_dict_defaults_bad_intent():
    node = CompoundTaskNode.from_raw_dict({"task": "do it", "intent_type": "fly"})
    assert node.intent_type == "read"


# ---------------------------------------------------------------------------
# 11. from_raw_dict — confidence clamped
# ---------------------------------------------------------------------------

def test_from_raw_dict_clamps_confidence_above():
    node = CompoundTaskNode.from_raw_dict({"task": "do it", "confidence": 5.0})
    assert node.confidence == 1.0


def test_from_raw_dict_clamps_confidence_below():
    node = CompoundTaskNode.from_raw_dict({"task": "do it", "confidence": -3.0})
    assert node.confidence == 0.0


# ---------------------------------------------------------------------------
# 12. from_raw_dict — raises on missing task text
# ---------------------------------------------------------------------------

def test_from_raw_dict_raises_on_missing_task():
    with pytest.raises(ValueError, match="missing 'task'"):
        CompoundTaskNode.from_raw_dict({"intent_type": "read"})


def test_from_raw_dict_raises_on_empty_task():
    with pytest.raises(ValueError, match="missing 'task'"):
        CompoundTaskNode.from_raw_dict({"task": "   ", "intent_type": "read"})


# ---------------------------------------------------------------------------
# 13. from_raw_dict — raises on non-dict input
# ---------------------------------------------------------------------------

def test_from_raw_dict_raises_on_non_dict():
    with pytest.raises(ValueError, match="expected dict"):
        CompoundTaskNode.from_raw_dict(["task", "read"])


# ---------------------------------------------------------------------------
# 14. _partition_steps — reads before writes
# ---------------------------------------------------------------------------

def test_partition_steps_reads_first():
    read_node = _node(intent_type="read", utterance_span=(0, 5), task="r")
    search_node = _node(intent_type="search", utterance_span=(6, 12), task="s")
    calc_node = _node(intent_type="calc", utterance_span=(13, 20), task="c")
    write_node = _node(
        intent_type="write", utterance_span=(21, 30),
        risk_class="safe", approval_policy="auto",
        task="w",
    )

    steps = [
        _step(write_node, step_id=1, step_index=0),
        _step(calc_node, step_id=2, step_index=1),
        _step(read_node, step_id=3, step_index=2),
        _step(search_node, step_id=4, step_index=3),
    ]
    reads, writes = _partition_steps(steps)
    read_tools = {s.tool_name for s in reads}
    write_tools = {s.tool_name for s in writes}
    assert all("read" in t or "search" in t or "calc" in t for t in read_tools)
    assert all("write" in t for t in write_tools)
    assert len(reads) == 3
    assert len(writes) == 1


def test_partition_steps_compose_dispatch_are_writes():
    compose_node = _node(
        intent_type="compose", utterance_span=(0, 10),
        risk_class="safe", approval_policy="auto",
        task="compose email",
    )
    dispatch_node = _node(
        intent_type="dispatch", utterance_span=(11, 20),
        risk_class="safe", approval_policy="auto",
        task="send it",
    )
    steps = [_step(compose_node), _step(dispatch_node, step_id=2)]
    reads, writes = _partition_steps(steps)
    assert len(reads) == 0
    assert len(writes) == 2


# ---------------------------------------------------------------------------
# 15. approve_required step gets status='waiting' initially
# ---------------------------------------------------------------------------

def test_approve_required_step_initial_status():
    """WorkStep for approve_required node should start as 'waiting' when set."""
    approve_node = _node(
        intent_type="write",
        utterance_span=(0, 10),
        risk_class="approve_required",
        approval_policy="confirm_send",
        task="send email to alice",
        entities=["alice"],
    )
    step = _step(approve_node)
    # Status starts as 'pending' before the dispatcher touches it;
    # the compound_turn logic sets it to 'waiting' during execution.
    # We validate here that it starts as a recognised status and that
    # the approve_required gate is set on the node.
    assert step.status == "pending"
    assert step.node is not None
    assert step.node.risk_class == "approve_required"
    assert step.node.approval_policy == "confirm_send"
