"""Phase 8 — bounded LLM rewrite for filter-flagged outbound replies.

Covers:
  - bounded_rewrite returns the SDK's text on success
  - bounded_rewrite returns None on SDK exception
  - bounded_rewrite returns None on empty SDK output
  - rewrite_or_fallback ships the rewrite when it passes the second filter pass
  - rewrite_or_fallback falls back to a short in-voice phrase when the rewrite
    still drifts
  - rewrite_or_fallback falls back when bounded_rewrite returns None
  - detection_only mode ships the original (back-compat for opt-out)
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import config, post_filter
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    post_filter.reload_patterns()
    yield
    post_filter.reload_patterns()


def _patch_sdk_returning(monkeypatch, text_blocks: list[str]) -> dict[str, int]:
    """Patch ClaudeSDKClient inside agents.post_filter to yield ``text_blocks``
    from receive_response. Returns a counter dict for assertions about how
    often the SDK was constructed.

    Now patches agents.post_filter.ClaudeSDKClient directly (module-level
    import) rather than monkeypatching builtins.__import__.
    """
    counter = {"opens": 0, "queries": 0}

    class _FakeAssistant:
        def __init__(self, blocks):
            self.content = blocks

    class _FakeTextBlock:
        def __init__(self, text):
            self.text = text

    class _FakeClient:
        def __init__(self, options=None):
            self.options = options

        async def __aenter__(self):
            counter["opens"] += 1
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            counter["queries"] += 1

        async def receive_response(self):
            yield _FakeAssistant([_FakeTextBlock(t) for t in text_blocks])

    monkeypatch.setattr(post_filter, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(post_filter, "AssistantMessage", _FakeAssistant)
    monkeypatch.setattr(post_filter, "TextBlock", _FakeTextBlock)
    monkeypatch.setattr(post_filter, "ClaudeAgentOptions",
                        lambda **kw: SimpleNamespace(**kw))
    return counter


# ---------- bounded_rewrite ----------

@pytest.mark.asyncio
async def test_bounded_rewrite_returns_sdk_text(monkeypatch):
    counter = _patch_sdk_returning(monkeypatch, ["ugh. fine. here."])
    out = await post_filter.bounded_rewrite(
        "I'd be happy to help with that great question!",
        instruction="strip assistant patter",
        mood="tired",
    )
    assert out == "ugh. fine. here."
    assert counter["opens"] == 1
    assert counter["queries"] == 1


@pytest.mark.asyncio
async def test_bounded_rewrite_returns_none_on_empty_output(monkeypatch):
    _patch_sdk_returning(monkeypatch, [""])
    out = await post_filter.bounded_rewrite(
        "Sure thing! Happy to help.", instruction="x",
    )
    assert out is None


@pytest.mark.asyncio
async def test_bounded_rewrite_returns_none_on_sdk_exception(monkeypatch):
    class _BoomClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            raise RuntimeError("simulated SDK failure")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(post_filter, "ClaudeSDKClient", _BoomClient)
    monkeypatch.setattr(post_filter, "ClaudeAgentOptions",
                        lambda **kw: SimpleNamespace(**kw))

    out = await post_filter.bounded_rewrite("x", instruction="y")
    assert out is None


@pytest.mark.asyncio
async def test_bounded_rewrite_empty_inputs_return_none():
    assert await post_filter.bounded_rewrite("", instruction="x") is None
    assert await post_filter.bounded_rewrite("x", instruction="") is None


@pytest.mark.asyncio
async def test_bounded_rewrite_handles_braces_in_text(monkeypatch):
    """Original text with literal braces must not crash template formatting."""
    counter = _patch_sdk_returning(monkeypatch, ["clean."])
    out = await post_filter.bounded_rewrite(
        'reply contained {"key": "value"}',
        instruction="rewrite",
    )
    assert out == "clean."
    assert counter["opens"] == 1


# ---------- rewrite_or_fallback ----------

def _filter_result(needs_rewrite: bool, instruction: str = "fix") -> post_filter.FilterResult:
    return post_filter.FilterResult(
        text="original",
        refusal_short_replaced=False,
        refusal_hits=["(?i)\\bas an? AI\\b"],
        sycophancy_triggered=False,
        sycophancy_violations=[],
        needs_llm_rewrite=needs_rewrite,
        rewrite_instruction=instruction,
    )


@pytest.mark.asyncio
async def test_rewrite_or_fallback_ships_clean_rewrite(monkeypatch):
    _patch_sdk_returning(monkeypatch, ["ugh. fine. here's the thing."])
    fr = _filter_result(needs_rewrite=True)
    out = await post_filter.rewrite_or_fallback(
        "as an AI, I cannot help.", fr, mood="tired", where="bridge",
    )
    assert out == "ugh. fine. here's the thing."


@pytest.mark.asyncio
async def test_rewrite_or_fallback_falls_back_when_rewrite_still_drifts(monkeypatch):
    # Rewrite output STILL contains "as an AI" — should fall back.
    _patch_sdk_returning(
        monkeypatch,
        ["as an AI, I cannot help with that."],
    )
    fr = _filter_result(needs_rewrite=True)
    out = await post_filter.rewrite_or_fallback(
        "I'd be happy to help with that great question!",
        fr, mood=None, where="bridge",
    )
    pool = config.get("refusal_filter.short_replacements") or []
    assert out in pool


@pytest.mark.asyncio
async def test_rewrite_or_fallback_falls_back_on_sdk_failure(monkeypatch):
    # Patch bounded_rewrite directly to return None (SDK failure path).
    async def fake_rewrite(text, instruction, mood=None):
        return None
    monkeypatch.setattr(post_filter, "bounded_rewrite", fake_rewrite)

    fr = _filter_result(needs_rewrite=True)
    out = await post_filter.rewrite_or_fallback(
        "as an AI, I'd be happy to help.", fr, mood=None, where="bridge",
    )
    pool = config.get("refusal_filter.short_replacements") or []
    assert out in pool


@pytest.mark.asyncio
async def test_rewrite_or_fallback_detection_only_ships_original(monkeypatch, tmp_path):
    cfg_text = (
        "post_filter:\n"
        '  rewrite_strategy: "detection_only"\n'
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    # bounded_rewrite must NOT be called in detection_only mode.
    called = {"n": 0}

    async def fake_rewrite(text, instruction, mood=None):
        called["n"] += 1
        return "this won't ship"
    monkeypatch.setattr(post_filter, "bounded_rewrite", fake_rewrite)

    fr = _filter_result(needs_rewrite=True)
    out = await post_filter.rewrite_or_fallback(
        "as an AI, I cannot help.", fr, mood=None, where="bridge",
    )
    assert out == "as an AI, I cannot help."
    assert called["n"] == 0


# ---------- Fix 4 — bounded_rewrite records cost on ResultMessage ----------

def _patch_sdk_with_result_message(monkeypatch, text_blocks: list[str], usage: dict):
    """Patch ClaudeSDKClient to yield AssistantMessage then ResultMessage."""

    class _FakeAssistant:
        def __init__(self, blocks):
            self.content = blocks

    class _FakeTextBlock:
        def __init__(self, text):
            self.text = text

    class _FakeResultMessage:
        def __init__(self, usage_dict):
            self.usage = usage_dict
            self.model_usage = None  # no per-model breakdown in test

    class _FakeClient:
        def __init__(self, options=None):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield _FakeAssistant([_FakeTextBlock(t) for t in text_blocks])
            yield _FakeResultMessage(usage)

    monkeypatch.setattr(post_filter, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(post_filter, "AssistantMessage", _FakeAssistant)
    monkeypatch.setattr(post_filter, "ResultMessage", _FakeResultMessage)
    monkeypatch.setattr(post_filter, "TextBlock", _FakeTextBlock)
    monkeypatch.setattr(post_filter, "ClaudeAgentOptions",
                        lambda **kw: SimpleNamespace(**kw))


@pytest.mark.asyncio
async def test_bounded_rewrite_records_cost_on_result_message(monkeypatch):
    """Fix 4: bounded_rewrite calls _record_llm_cost with path='bounded_rewrite'
    and fallback_model matching the configured model when a ResultMessage with
    usage arrives."""
    usage = {"input_tokens": 42, "output_tokens": 7}
    _patch_sdk_with_result_message(monkeypatch, ["ugh. fine."], usage)

    cost_calls: list[dict] = []

    def _fake_record_llm_cost(model_usage, *, path, fallback_model, fallback_usage):
        cost_calls.append({
            "model_usage": model_usage,
            "path": path,
            "fallback_model": fallback_model,
            "fallback_usage": fallback_usage,
        })

    import agents.runtime as _rt
    monkeypatch.setattr(_rt, "_record_llm_cost", _fake_record_llm_cost)

    out = await post_filter.bounded_rewrite(
        "I'd be happy to help!", instruction="rewrite"
    )
    assert out == "ugh. fine."
    assert len(cost_calls) == 1
    assert cost_calls[0]["path"] == "bounded_rewrite"
    assert cost_calls[0]["fallback_model"] == str(
        config.get("post_filter.rewrite_model", "claude-sonnet-4-6")
    )
    assert cost_calls[0]["fallback_usage"] == usage


@pytest.mark.asyncio
async def test_bounded_rewrite_cost_logging_failure_does_not_break_rewrite(monkeypatch):
    """Fix 4: if _record_llm_cost raises, bounded_rewrite still returns the text."""
    usage = {"input_tokens": 10, "output_tokens": 3}
    _patch_sdk_with_result_message(monkeypatch, ["done."], usage)

    def _boom(*args, **kwargs):
        raise RuntimeError("telemetry down")

    import agents.runtime as _rt
    monkeypatch.setattr(_rt, "_record_llm_cost", _boom)

    out = await post_filter.bounded_rewrite("original", instruction="rewrite")
    assert out == "done."


# ---------- Fix 2 — rewrite_or_fallback returns markdown-stripped second.text ----------

@pytest.mark.asyncio
async def test_rewrite_or_fallback_returns_markdown_stripped_text(monkeypatch):
    """Fix 2: rewrite_or_fallback returns second.text (markdown stripped), not
    the raw rewrite string from bounded_rewrite."""
    # bounded_rewrite will return text with markdown bold
    raw_rewrite = "**ugh**. fine. here's the thing."
    # After _strip_chat_markdown, bold is stripped → "ugh. fine. here's the thing."
    expected_stripped = "ugh. fine. here's the thing."

    async def _fake_rewrite(text, instruction, mood=None):
        return raw_rewrite

    monkeypatch.setattr(post_filter, "bounded_rewrite", _fake_rewrite)

    fr = _filter_result(needs_rewrite=True)
    out = await post_filter.rewrite_or_fallback(
        "as an AI, I cannot help.", fr, mood=None, where="bridge",
    )
    # Must be the filtered/stripped version, not the raw rewrite
    assert out == expected_stripped
    assert "**" not in out
