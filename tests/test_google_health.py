"""Bug 1 fix (live 2026-05-21): startup probe for Google Workspace OAuth.

The upstream google-workspace-mcp package never rotates the refresh token,
and the OAuth app is in Testing mode (Google force-expires refresh tokens
after 7 days). Without this probe, the first failure surfaces as a
user-visible 401 mid-conversation.
"""
from __future__ import annotations

import httpx
import pytest

from agents.google_health import probe_google_token


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "GOOGLE_WORKSPACE_CLIENT_ID",
        "GOOGLE_WORKSPACE_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.asyncio
async def test_probe_returns_missing_env_when_none_set():
    healthy, reason = await probe_google_token()
    assert healthy is False
    assert reason.startswith("missing_env:")
    # All three should be listed.
    for name in (
        "GOOGLE_WORKSPACE_CLIENT_ID",
        "GOOGLE_WORKSPACE_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
    ):
        assert name in reason


@pytest.mark.asyncio
async def test_probe_returns_missing_env_for_partial_config(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid")
    # Only one of three set — should still flag missing_env.
    healthy, reason = await probe_google_token()
    assert healthy is False
    assert "GOOGLE_WORKSPACE_CLIENT_SECRET" in reason
    assert "GOOGLE_WORKSPACE_REFRESH_TOKEN" in reason


@pytest.mark.asyncio
async def test_probe_returns_healthy_on_200(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "1//ARefreshTokenLooksLikeThis")

    def _handler(request: httpx.Request) -> httpx.Response:
        # Verify the form payload — Google's required fields.
        assert b"grant_type=refresh_token" in request.content
        assert b"refresh_token=1%2F%2FARefreshTokenLooksLikeThis" in request.content
        return httpx.Response(200, json={
            "access_token": "ya29.fake",
            "expires_in": 3599,
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
            "token_type": "Bearer",
        })

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: _MockClient(_handler),
    )

    healthy, reason = await probe_google_token()
    assert healthy is True
    assert reason == ""


@pytest.mark.asyncio
async def test_probe_returns_invalid_grant_on_expired_token(monkeypatch):
    """The canonical Testing-mode 7-day-expiry failure: Google returns 400 with
    error=invalid_grant. Probe must surface this verbatim so the operator
    knows to re-run setup_google_oauth.py."""
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "stale_token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        })

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: _MockClient(_handler),
    )

    healthy, reason = await probe_google_token()
    assert healthy is False
    assert reason == "invalid_grant"


@pytest.mark.asyncio
async def test_probe_returns_network_on_transport_error(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "tok")

    class _ExplodingClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _ExplodingClient)

    healthy, reason = await probe_google_token()
    assert healthy is False
    assert reason == "network:ConnectError"


@pytest.mark.asyncio
async def test_probe_returns_http_code_on_non_json_5xx(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "tok")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: _MockClient(_handler),
    )

    healthy, reason = await probe_google_token()
    assert healthy is False
    # Either http_503 (json parse failed) or the parsed error if 503 had one.
    assert reason in {"http_503", "unknown"}


class _MockClient:
    """Minimal async-context-manager httpx client driven by a request handler."""
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *, data=None, **kwargs):
        # Build a request to match what the real client constructs so the
        # handler can inspect its body.
        req = httpx.Request("POST", url, data=data or {})
        return self._handler(req)
