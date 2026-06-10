"""run_internal_text — stateless no-tool no-persona SDK aux call.

This is the OAuth-subscription replacement for the OpenRouter aux path
(`_call_aux_llm`). Contract pins:
- stripped options: caller's system prompt (never PERSONA.md), zero MCP
  servers, zero allowed tools, no hooks, no gatekeeper, no project settings;
- returns "" on SDK failure AND on leaked sdk-error strings (the
  evening_diary guard), matching what aux callers already handle;
- cost recorded under path="aux_sdk" so /cockpit can tell subscription aux
  spend from OpenRouter aux ("aux_llm") and internal-control ("ephemeral").
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk._errors import ProcessError


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


def _result_message(usage=None):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="aux-session",
        usage=usage,
    )


class _FakeClient:
    """Mimics ClaudeSDKClient for a single canned exchange."""

    def __init__(self, options=None, *, text="ok", usage=None, raise_on_query=None):
        self.options = options
        self._text = text
        self._usage = usage
        self._raise = raise_on_query

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        if self._raise is not None:
            raise self._raise

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(self._text)], model="fake")
        yield _result_message(self._usage)


def test_aux_options_are_stripped():
    """No persona, no tools, no hooks, no project settings, single turn."""
    import agents.runtime as runtime

    opts = runtime._build_aux_options(system="be terse", model="claude-haiku-4-5")
    assert opts.system_prompt == "be terse", "must use caller system, never _persona()"
    assert opts.mcp_servers in ({}, None) or not opts.mcp_servers
    assert not opts.allowed_tools
    assert opts.max_turns == 1
    assert opts.setting_sources in ([], None) or not opts.setting_sources
    assert not opts.hooks
    assert opts.can_use_tool is None
    assert opts.resume is None
    assert opts.model == "claude-haiku-4-5"
    assert opts.fallback_model != opts.model, "SDK refuses identical model+fallback"


def test_aux_options_fallback_never_equals_model():
    import agents.runtime as runtime

    opts = runtime._build_aux_options(
        system="x", model=runtime.MODEL_FALLBACK)
    assert opts.fallback_model != opts.model


@pytest.mark.asyncio
async def test_returns_text_and_records_aux_sdk_cost(monkeypatch):
    import agents.runtime as runtime

    recorded: list[dict] = []

    def fake_record(model_usage, *, path, fallback_model, fallback_usage):
        recorded.append({"path": path, "model": fallback_model, "usage": fallback_usage})

    monkeypatch.setattr(runtime, "_record_llm_cost", fake_record)
    monkeypatch.setattr(
        runtime, "ClaudeSDKClient",
        lambda options=None: _FakeClient(
            options, text="classified: yes",
            usage={"input_tokens": 10, "output_tokens": 3}),
    )

    out = await runtime.run_internal_text("classify this")
    assert out == "classified: yes"
    assert recorded, "cost must be recorded"
    assert recorded[0]["path"] == "aux_sdk"


@pytest.mark.asyncio
async def test_default_model_is_haiku(monkeypatch):
    import agents.runtime as runtime

    seen_options = []

    def factory(options=None):
        seen_options.append(options)
        return _FakeClient(options)

    monkeypatch.setattr(runtime, "ClaudeSDKClient", factory)
    await runtime.run_internal_text("x")
    assert seen_options[0].model == runtime.MODEL_HAIKU


@pytest.mark.asyncio
async def test_process_error_returns_empty(monkeypatch):
    import agents.runtime as runtime

    monkeypatch.setattr(
        runtime, "ClaudeSDKClient",
        lambda options=None: _FakeClient(
            options, raise_on_query=ProcessError("boom", exit_code=1, stderr="x")),
    )
    out = await runtime.run_internal_text("x")
    assert out == ""


@pytest.mark.asyncio
async def test_leaked_sdk_error_string_returns_empty(monkeypatch):
    """A 'Failed to authenticate. API Error: 401 ...' TextBlock must not be
    returned as if it were a model answer (it became a fake heartbeat once)."""
    import agents.runtime as runtime

    monkeypatch.setattr(
        runtime, "ClaudeSDKClient",
        lambda options=None: _FakeClient(
            options, text="Failed to authenticate. API Error: 401 unauthorized"),
    )
    out = await runtime.run_internal_text("x")
    assert out == ""


@pytest.mark.asyncio
async def test_daily_reflection_survives_empty_sdk_replies(monkeypatch):
    """End-to-end resilience pin: when the SDK aux path returns "" (the new
    failure mode — OpenRouter raised instead, which ABORTED the whole cycle
    at the outer except), the 09:00 cycle must keep going: "" parses to {}
    ("nothing to record"), extraction no-ops, and the mechanical maintenance
    blocks still run (DECISIONS: one bad LLM reply must never skip the whole
    cycle)."""
    import agents.runtime as runtime
    from agents import reflection
    from storage import db

    db.insert_episode("2026-06-09", "stand-up")

    calls: list[str] = []

    async def empty_internal_text(prompt, **kw):
        calls.append(prompt)
        return ""

    # Patch the REAL underlying SDK call — run_reflection_call stays live so
    # the wrapper → run_internal_text → "" → {} → maintenance path is exercised.
    monkeypatch.setattr(runtime, "run_internal_text", empty_internal_text)

    await reflection.run_daily_reflection()

    # ≥2 proves the cycle continued PAST extraction into the maintenance
    # blocks (consolidation / seeder also route through the aux path).
    assert len(calls) >= 2, "maintenance must still run after an empty extraction reply"


@pytest.mark.asyncio
async def test_wrappers_route_through_sdk_not_openrouter(monkeypatch):
    """run_reflection_call / run_aux_composition must hit run_internal_text,
    never _call_aux_llm (OpenRouter stays only for synchronous pre-reply
    classifiers: stickers + task_extractor)."""
    import agents.runtime as runtime

    called: list[tuple[str, dict]] = []

    async def fake_internal_text(prompt, **kw):
        called.append((prompt, kw))
        return "yaml: ok"

    async def boom(*a, **kw):
        raise AssertionError("_call_aux_llm must not be hit by the SDK wrappers")

    monkeypatch.setattr(runtime, "run_internal_text", fake_internal_text)
    monkeypatch.setattr(runtime, "_call_aux_llm", boom)

    assert await runtime.run_reflection_call("reflect") == "yaml: ok"
    assert await runtime.run_aux_composition("compose", max_tokens=300) == "yaml: ok"
    assert len(called) == 2
