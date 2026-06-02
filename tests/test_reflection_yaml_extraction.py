"""Reflection YAML extraction + non-mapping guards.

Regression coverage for the production failure modes where the cheap aux LLM
(OpenRouter / DeepSeek) did not honour "YAML only" and reflection either
crashed or silently skipped:

  - reflection_after_task crashed at `for f in data.get('facts')` on a prose
    reply (the 04b7f7b guard only covered run_daily_reflection).
  - invalid/truncated YAML and fence-wrapped output failed to parse.

LLM + DB calls are stubbed so these are deterministic and need no network.
"""
from __future__ import annotations

import logging

import pytest

from agents import reflection

# ---- pure: _strip_fences ----------------------------------------------------

def test_strip_fences_clean_yaml_block():
    raw = "```yaml\nnew_facts:\n  - {subject: a}\n```"
    assert reflection._strip_fences(raw) == "new_facts:\n  - {subject: a}"


def test_strip_fences_leading_prose_before_fence():
    # model prefaced the fence with prose — startswith('```') was False, so the
    # old impl left the fence in and the parse failed.
    raw = "Here's the reflection:\n```yaml\nnew_facts: []\n```"
    assert reflection._strip_fences(raw) == "new_facts: []"


def test_strip_fences_unclosed_fence_truncated():
    # hit max_tokens before the closing ``` — opening fence must still be stripped.
    raw = "```yaml\nnew_facts:\n  - {subject: 'user', predicate: 'x'"
    out = reflection._strip_fences(raw)
    assert not out.startswith("```")
    assert out.startswith("new_facts:")


def test_strip_fences_bare_fence_no_lang():
    raw = "```\nkey: value\n```"
    assert reflection._strip_fences(raw) == "key: value"


def test_strip_fences_no_fence_passthrough():
    raw = "new_facts:\n  - {subject: a}"
    assert reflection._strip_fences(raw) == raw


# ---- pure: _parse_yaml_mapping ----------------------------------------------

def test_parse_mapping_valid():
    assert reflection._parse_yaml_mapping("a: 1\nb: 2", context="t") == {"a": 1, "b": 2}


def test_parse_mapping_empty_is_empty_dict():
    # genuine "nothing to record" — not an error; callers proceed with no updates.
    assert reflection._parse_yaml_mapping("", context="t") == {}


def test_parse_mapping_prose_is_none():
    assert reflection._parse_yaml_mapping("just a sentence, no yaml", context="t") is None


def test_parse_mapping_fenced_with_prose_is_dict():
    raw = "Sure:\n```yaml\nfacts: []\n```"
    assert reflection._parse_yaml_mapping(raw, context="t") == {"facts": []}


def test_parse_mapping_truncated_yaml_is_none():
    raw = "facts:\n  - {subject: 'a', predicate:"  # unterminated flow mapping
    assert reflection._parse_yaml_mapping(raw, context="t") is None


def test_parse_mapping_logs_raw_on_prose(caplog):
    with caplog.at_level(logging.WARNING):
        reflection._parse_yaml_mapping("PROSE_SENTINEL_123 not yaml", context="ctxlabel")
    # raw content + context must be surfaced so silent degradation is diagnosable.
    assert "PROSE_SENTINEL_123" in caplog.text
    assert "ctxlabel" in caplog.text


# ---- regression: reflection_after_task non-mapping reply --------------------

@pytest.mark.asyncio
async def test_reflection_after_task_prose_does_not_crash(monkeypatch):
    """Previously crashed at `for f in data.get('facts')` with
    `AttributeError: 'str' object has no attribute 'get'` on a prose reply."""
    fake_row = {
        "status": "done",
        "result_summary": "did the thing",
        "prompt": "do the thing",
        "meta_json": "{}",
        "cost_usd": 0.1,
        "tool_use_count": 2,
    }
    monkeypatch.setattr(reflection.db, "bg_task_get", lambda _tid: fake_row)

    async def prose(_prompt):
        return "No facts worth keeping from this task."

    monkeypatch.setattr(reflection, "run_reflection_call", prose)

    def _boom_insert(*a, **k):
        raise AssertionError("insert_fact must not be called on a prose reply")

    monkeypatch.setattr(reflection.db, "insert_fact", _boom_insert)

    # Must return cleanly, no AttributeError.
    await reflection.reflection_after_task("t1")
