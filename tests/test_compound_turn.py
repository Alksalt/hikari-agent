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
    """A failing subtask should not crash the whole compound turn."""
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
    assert "failed" in result or "bad" in result
