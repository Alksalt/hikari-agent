"""Retry behaviour of agents.runtime._call_aux_llm.

Exercises the two-attempt retry loop around httpx.AsyncClient.post:
  - 429 Too Many Requests on attempt 1 → retries, returns result on attempt 2
  - 503 Service Unavailable on attempt 1 → retries, returns result on attempt 2
  - Transport error (ConnectError / ReadTimeout) on attempt 1 → retries
  - 400 Bad Request (non-retryable HTTP code) → raises immediately (no retry)
  - Transport error on both attempts → raises on attempt 2

The function is imported via its private path; the asyncio.sleep() call inside
the retry path is patched to a no-op so tests don't pay real wall-clock time.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_response(content: str = "pong") -> MagicMock:
    """Build a minimal mock httpx.Response that looks like a successful
    OpenRouter completion reply."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    payload = {
        "choices": [
            {"message": {"content": content}}
        ]
    }
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    resp.raise_for_status = MagicMock()  # no-op for 2xx
    return resp


def _error_response(status: int, body: str = "error") -> MagicMock:
    """Build a mock httpx.Response with an error status code."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = body

    def _raise():
        raise httpx.HTTPStatusError(
            f"HTTP {status}",
            request=MagicMock(),
            response=resp,
        )

    resp.raise_for_status = _raise
    return resp


# Patch target: the class method that AsyncClient.post resolves to.
_POST_TARGET = "httpx.AsyncClient.post"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_429_retries_and_succeeds(monkeypatch):
    """429 on attempt 1 → sleep 2s → attempt 2 succeeds → returns content."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-429")
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    post_mock = AsyncMock(side_effect=[
        _error_response(429, "rate limited"),
        _ok_response("hello"),
    ])

    with patch(_POST_TARGET, post_mock), patch("asyncio.sleep", _fake_sleep):
        from agents.runtime import _call_aux_llm
        result = await _call_aux_llm("ping")

    assert result == "hello"
    assert post_mock.call_count == 2
    assert sleep_calls == [2.0]


async def test_503_retries_and_succeeds(monkeypatch):
    """503 on attempt 1 → sleep 2s → attempt 2 succeeds → returns content."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-503")
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    post_mock = AsyncMock(side_effect=[
        _error_response(503, "service unavailable"),
        _ok_response("world"),
    ])

    with patch(_POST_TARGET, post_mock), patch("asyncio.sleep", _fake_sleep):
        from agents.runtime import _call_aux_llm
        result = await _call_aux_llm("ping")

    assert result == "world"
    assert post_mock.call_count == 2
    assert sleep_calls == [2.0]


async def test_connect_error_retries_and_succeeds(monkeypatch):
    """httpx.ConnectError on attempt 1 → sleep 2s → attempt 2 succeeds."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-connect")
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    post_mock = AsyncMock(side_effect=[
        httpx.ConnectError("connection refused"),
        _ok_response("recovered"),
    ])

    with patch(_POST_TARGET, post_mock), patch("asyncio.sleep", _fake_sleep):
        from agents.runtime import _call_aux_llm
        result = await _call_aux_llm("ping")

    assert result == "recovered"
    assert post_mock.call_count == 2
    assert sleep_calls == [2.0]


async def test_read_timeout_retries_and_succeeds(monkeypatch):
    """httpx.ReadTimeout on attempt 1 → sleep 2s → attempt 2 succeeds."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-timeout")
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    post_mock = AsyncMock(side_effect=[
        httpx.ReadTimeout("read timeout"),
        _ok_response("came back"),
    ])

    with patch(_POST_TARGET, post_mock), patch("asyncio.sleep", _fake_sleep):
        from agents.runtime import _call_aux_llm
        result = await _call_aux_llm("ping")

    assert result == "came back"
    assert post_mock.call_count == 2
    assert sleep_calls == [2.0]


async def test_400_raises_immediately_no_retry(monkeypatch):
    """400 Bad Request is non-retryable — must raise on first attempt, no retry."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-400")
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    post_mock = AsyncMock(return_value=_error_response(400, "bad request"))

    with patch(_POST_TARGET, post_mock), patch("asyncio.sleep", _fake_sleep):
        from agents.runtime import _call_aux_llm
        with pytest.raises(httpx.HTTPStatusError):
            await _call_aux_llm("ping")

    # Only one call — no retry on 400
    assert post_mock.call_count == 1
    assert sleep_calls == []


async def test_transport_error_both_attempts_raises(monkeypatch):
    """ConnectError on both attempts → raises the transport exception after attempt 2."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-both-fail")
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    post_mock = AsyncMock(side_effect=httpx.ConnectError("still down"))

    with patch(_POST_TARGET, post_mock), patch("asyncio.sleep", _fake_sleep):
        from agents.runtime import _call_aux_llm
        with pytest.raises(httpx.ConnectError):
            await _call_aux_llm("ping")

    assert post_mock.call_count == 2
    assert sleep_calls == [2.0]


async def test_429_both_attempts_raises(monkeypatch):
    """429 on attempt 1, 429 again on attempt 2 → raises HTTPStatusError."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-429-both")
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    post_mock = AsyncMock(side_effect=[
        _error_response(429, "still rate limited"),
        _error_response(429, "still rate limited"),
    ])

    with patch(_POST_TARGET, post_mock), patch("asyncio.sleep", _fake_sleep):
        from agents.runtime import _call_aux_llm
        with pytest.raises(httpx.HTTPStatusError):
            await _call_aux_llm("ping")

    assert post_mock.call_count == 2
    assert sleep_calls == [2.0]


async def test_missing_api_key_raises_runtime_error():
    """Missing OPENROUTER_API_KEY → RuntimeError before any HTTP call."""
    import os
    # Ensure the key is absent (monkeypatch can't remove it in all contexts,
    # so we save/restore manually).
    original = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        from agents.runtime import _call_aux_llm
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            await _call_aux_llm("ping")
    finally:
        if original is not None:
            os.environ["OPENROUTER_API_KEY"] = original
