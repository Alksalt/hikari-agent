"""Tests for agents.runtime.run_isolated_dialogue — multi-turn isolated persona session."""
from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


class _FakeClient:
    """Mimics ClaudeSDKClient: query() enqueues a scripted reply,
    receive_response() yields it. Records every query for assertions."""

    instances: list[_FakeClient] = []

    def __init__(self, options=None):
        self.options = options
        self.queries: list[str] = []
        self._replies = iter(["first reply", "second reply"])
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt: str):
        self.queries.append(prompt)

    async def receive_response(self):
        yield _FakeAssistantMessage(next(self._replies))


@pytest.mark.asyncio
async def test_dialogue_returns_one_reply_per_prompt_in_one_session(monkeypatch):
    _FakeClient.instances.clear()
    import agents.runtime as runtime

    # isinstance checks in the collector must accept the fakes
    with (
        patch.object(runtime, "ClaudeSDKClient", _FakeClient),
        patch.object(runtime, "AssistantMessage", _FakeAssistantMessage),
        patch.object(runtime, "TextBlock", _FakeBlock),
    ):
        replies = await runtime.run_isolated_dialogue(
            ["question one", "pushback two"]
        )

    assert replies == ["first reply", "second reply"]
    # ONE client session for the whole dialogue — that's the point
    assert len(_FakeClient.instances) == 1
    assert _FakeClient.instances[0].queries == ["question one", "pushback two"]


@pytest.mark.asyncio
async def test_dialogue_empty_prompts_returns_empty_no_client(monkeypatch):
    _FakeClient.instances.clear()
    import agents.runtime as runtime

    with patch.object(runtime, "ClaudeSDKClient", _FakeClient):
        replies = await runtime.run_isolated_dialogue([])

    assert replies == []
    assert _FakeClient.instances == []


@pytest.mark.asyncio
async def test_dialogue_disables_memory_injection(monkeypatch):
    """Eval sessions must not fire the inject_memory hook — it mutates live
    runtime_state (last_user_message, pending handoffs) on every query."""
    _FakeClient.instances.clear()
    import agents.runtime as runtime

    seen: dict = {}
    real_build = runtime._build_options

    def spy_build(**kwargs):
        seen.update(kwargs)
        return real_build(**kwargs)

    with (
        patch.object(runtime, "_build_options", spy_build),
        patch.object(runtime, "ClaudeSDKClient", _FakeClient),
        patch.object(runtime, "AssistantMessage", _FakeAssistantMessage),
        patch.object(runtime, "TextBlock", _FakeBlock),
    ):
        await runtime.run_isolated_dialogue(["q1", "q2"])

    assert seen.get("inject_memory_enabled") is False


@pytest.mark.asyncio
async def test_isolated_turn_disables_memory_injection(monkeypatch):
    """Same contract for the single-turn sibling used by the @slow persona
    eval — parity with run_isolated_dialogue."""
    _FakeClient.instances.clear()
    import agents.runtime as runtime

    seen: dict = {}
    real_build = runtime._build_options

    def spy_build(**kwargs):
        seen.update(kwargs)
        return real_build(**kwargs)

    with (
        patch.object(runtime, "_build_options", spy_build),
        patch.object(runtime, "ClaudeSDKClient", _FakeClient),
        patch.object(runtime, "AssistantMessage", _FakeAssistantMessage),
        patch.object(runtime, "TextBlock", _FakeBlock),
    ):
        reply = await runtime.run_isolated_turn("q1")

    assert reply == "first reply"
    assert seen.get("inject_memory_enabled") is False
