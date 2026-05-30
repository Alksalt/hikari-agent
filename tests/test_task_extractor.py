"""Tests for typed task extractor + CompoundTaskNode validation (Sprint A Wave 3)."""
from __future__ import annotations

import json
import pytest


# ---------------------------------------------------------------------------
# CompoundTaskNode.from_raw_dict
# ---------------------------------------------------------------------------

def test_from_raw_dict_minimal():
    from agents.work_packet import CompoundTaskNode
    n = CompoundTaskNode.from_raw_dict({"task": "check weather"})
    assert n.task == "check weather"
    assert n.intent_type == "read"
    assert n.utterance_span == (0, 0)
    assert n.risk_class == "safe"
    assert n.approval_policy == "auto"
    assert 0.0 <= n.confidence <= 1.0


def test_from_raw_dict_full():
    from agents.work_packet import CompoundTaskNode
    n = CompoundTaskNode.from_raw_dict({
        "task": "send email to alex",
        "intent_type": "write",
        "utterance_span": [0, 19],
        "entities": ["alex"],
        "time_refs": ["tomorrow"],
        "risk_class": "approve_required",
        "approval_policy": "confirm_send",
        "confidence": 0.8,
        "voice_uncertainty": True,
    })
    assert n.intent_type == "write"
    assert n.entities == ["alex"]
    assert n.time_refs == ["tomorrow"]
    assert n.risk_class == "approve_required"
    assert n.approval_policy == "confirm_send"
    assert n.confidence == 0.8
    assert n.voice_uncertainty is True


def test_from_raw_dict_clamps_confidence():
    from agents.work_packet import CompoundTaskNode
    n = CompoundTaskNode.from_raw_dict({"task": "x", "confidence": 2.5})
    assert n.confidence == 1.0
    n2 = CompoundTaskNode.from_raw_dict({"task": "x", "confidence": -0.5})
    assert n2.confidence == 0.0


def test_from_raw_dict_oov_intent_falls_back_to_read():
    from agents.work_packet import CompoundTaskNode
    n = CompoundTaskNode.from_raw_dict({"task": "x", "intent_type": "fly_to_moon"})
    assert n.intent_type == "read"


def test_from_raw_dict_clamps_span_to_text():
    from agents.work_packet import CompoundTaskNode
    n = CompoundTaskNode.from_raw_dict(
        {"task": "x", "utterance_span": [-5, 999]},
        full_text="hello world",
    )
    assert n.utterance_span == (0, 11)


def test_from_raw_dict_missing_task_raises():
    from agents.work_packet import CompoundTaskNode
    with pytest.raises(ValueError):
        CompoundTaskNode.from_raw_dict({"intent_type": "read"})


# ---------------------------------------------------------------------------
# validate_nodes
# ---------------------------------------------------------------------------

def test_validate_nodes_empty_list_errors():
    from agents.work_packet import validate_nodes
    assert validate_nodes([]) == ["no nodes — extractor returned empty list"]


def test_validate_nodes_ok():
    from agents.work_packet import CompoundTaskNode, validate_nodes
    nodes = [
        CompoundTaskNode(
            intent_type="read", utterance_span=(0, 10),
            risk_class="safe", approval_policy="auto",
            confidence=0.9, task="check weather",
        ),
        CompoundTaskNode(
            intent_type="search", utterance_span=(11, 20),
            risk_class="safe", approval_policy="auto",
            confidence=0.8, task="find email",
        ),
    ]
    assert validate_nodes(nodes, full_text="x" * 30) == []


def test_validate_nodes_overlapping_spans():
    from agents.work_packet import CompoundTaskNode, validate_nodes
    nodes = [
        CompoundTaskNode(
            intent_type="read", utterance_span=(0, 15),
            risk_class="safe", approval_policy="auto",
            confidence=0.9, task="a",
        ),
        CompoundTaskNode(
            intent_type="read", utterance_span=(10, 20),
            risk_class="safe", approval_policy="auto",
            confidence=0.9, task="b",
        ),
    ]
    errs = validate_nodes(nodes)
    assert any("overlap" in e for e in errs)


def test_validate_nodes_risk_policy_mismatch():
    from agents.work_packet import CompoundTaskNode, validate_nodes
    nodes = [
        CompoundTaskNode(
            intent_type="write", utterance_span=(0, 10),
            risk_class="approve_required", approval_policy="auto",  # mismatch
            confidence=0.9, task="send email",
        ),
    ]
    errs = validate_nodes(nodes)
    assert any("approve_required" in e and "confirm_send" in e for e in errs)


def test_validate_nodes_negative_span():
    from agents.work_packet import CompoundTaskNode, validate_nodes
    nodes = [
        CompoundTaskNode(
            intent_type="read", utterance_span=(-1, 5),
            risk_class="safe", approval_policy="auto",
            confidence=0.9, task="x",
        ),
    ]
    errs = validate_nodes(nodes)
    assert any("negative" in e for e in errs)


def test_validate_nodes_time_ref_clean():
    from agents.work_packet import CompoundTaskNode, validate_nodes
    # Common time refs should pass
    nodes = [
        CompoundTaskNode(
            intent_type="write", utterance_span=(0, 5),
            risk_class="safe", approval_policy="auto",
            confidence=0.9, task="do x",
            time_refs=["in 1h", "tomorrow", "9am"],
        ),
    ]
    errs = validate_nodes(nodes)
    # Filter to only time-ref errors
    time_errs = [e for e in errs if "time_ref" in e]
    assert time_errs == []


# ---------------------------------------------------------------------------
# should_extract — the compound-routing gate
#
# Regression guard for the dominant "feels dumb" bug: the bare Ukrainian
# conjunction "та" ("and") used to match _COMPOUND_RE, so ordinary Ukrainian
# chat (the user's language) ≥8 words misrouted onto the stateless, memory-less
# compound path. should_extract must fire ONLY on genuine multi-task
# enumerators, never on a lone conjunction.
# ---------------------------------------------------------------------------

def test_should_extract_bare_ukrainian_and_does_not_trigger():
    from tools.dispatch.task_extractor import should_extract
    # 8+ words, contains "та" as a plain conjunction — must NOT route to compound.
    assert should_extract("розкажи мені будь ласка про погоду та новини сьогодні") is False


def test_should_extract_bare_also_then_plus_do_not_trigger():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("i was thinking about this also could you help me here") is False
    assert should_extract("so what happened then with the whole thing you mentioned") is False
    assert should_extract("також хотіла спитати як справи у тебе сьогодні взагалі") is False


def test_should_extract_plain_ukrainian_chat_does_not_trigger():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("привіт як ти себе почуваєш сьогодні цього чудового ранку") is False


def test_should_extract_real_multitask_connectives_trigger():
    from tools.dispatch.task_extractor import should_extract
    assert should_extract("check the weather and also send an email to alex now") is True
    assert should_extract("додай подію в календар а також постав ремайндер перед нею") is True
    assert should_extract("перевір пошту і ще подивись що там по календарю на завтра") is True
    assert should_extract("look at the calendar and then schedule the meeting for friday") is True


def test_should_extract_respects_min_words():
    from tools.dispatch.task_extractor import should_extract
    # Real connective but under the 8-word floor → not worth the extraction call.
    assert should_extract("check weather and also email") is False


# ---------------------------------------------------------------------------
# extract_typed_nodes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_typed_nodes_two_tasks(monkeypatch):
    from tools.dispatch import task_extractor

    async def _fake_aux(prompt, *, system=None, max_tokens=1024):
        return json.dumps([
            {
                "task": "check weather",
                "intent_type": "read",
                "utterance_span": [0, 13],
                "entities": ["weather"],
                "time_refs": [],
                "risk_class": "safe",
                "approval_policy": "auto",
                "confidence": 0.95,
                "voice_uncertainty": False,
            },
            {
                "task": "send email",
                "intent_type": "write",
                "utterance_span": [14, 30],
                "entities": ["email"],
                "time_refs": [],
                "risk_class": "approve_required",
                "approval_policy": "confirm_send",
                "confidence": 0.9,
                "voice_uncertainty": False,
            },
        ])

    monkeypatch.setattr("agents.runtime.run_aux_composition", _fake_aux)
    nodes = await task_extractor.extract_typed_nodes("check weather and also send email")
    assert len(nodes) == 2
    assert nodes[0].intent_type == "read"
    assert nodes[1].intent_type == "write"
    assert nodes[1].risk_class == "approve_required"


@pytest.mark.asyncio
async def test_extract_typed_nodes_bad_json_raises(monkeypatch):
    from tools.dispatch import task_extractor

    async def _fake_aux(prompt, *, system=None, max_tokens=1024):
        return "not json at all"

    monkeypatch.setattr("agents.runtime.run_aux_composition", _fake_aux)
    with pytest.raises(ValueError):
        await task_extractor.extract_typed_nodes("hello world")


@pytest.mark.asyncio
async def test_extract_typed_nodes_strips_fences(monkeypatch):
    from tools.dispatch import task_extractor

    async def _fake_aux(prompt, *, system=None, max_tokens=1024):
        return '```json\n[{"task": "x", "intent_type": "read"}]\n```'

    monkeypatch.setattr("agents.runtime.run_aux_composition", _fake_aux)
    nodes = await task_extractor.extract_typed_nodes("hello world")
    assert len(nodes) == 1
    assert nodes[0].task == "x"


@pytest.mark.asyncio
async def test_extract_typed_nodes_empty_list_raises(monkeypatch):
    from tools.dispatch import task_extractor

    async def _fake_aux(prompt, *, system=None, max_tokens=1024):
        return "[]"

    monkeypatch.setattr("agents.runtime.run_aux_composition", _fake_aux)
    with pytest.raises(ValueError):
        await task_extractor.extract_typed_nodes("hello world")
