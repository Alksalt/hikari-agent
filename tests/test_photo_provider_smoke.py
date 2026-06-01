"""Smoke test for the OpenRouter image generation endpoint contract.

Verifies that the endpoint at OPENROUTER_IMG_URL:
- Returns a 4xx error when given a bad (fake) API key.
- The error response body has the expected JSON shape (error key present).
- The URL and request format match the OpenAI-compatible images/generations spec.

This test hits the live network. It is skipped in offline/CI environments
where SKIP_NETWORK_TESTS=1 is set. It uses a deliberately invalid key so no
actual generation occurs — it just checks that the endpoint exists and
returns the documented error shape.
"""
from __future__ import annotations

import os

import pytest

SKIP_NETWORK = os.environ.get("SKIP_NETWORK_TESTS", "0") == "1"


@pytest.mark.skipif(SKIP_NETWORK, reason="SKIP_NETWORK_TESTS=1")
@pytest.mark.asyncio
async def test_openrouter_images_endpoint_shape():
    """POST to images/generations with a fake key → 4xx + error body."""
    import httpx

    from tools.photos._shared import OPENROUTER_IMG_URL

    # Sanity: the constant must point to the OpenRouter images endpoint.
    assert "openrouter.ai" in OPENROUTER_IMG_URL
    assert "images" in OPENROUTER_IMG_URL

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                OPENROUTER_IMG_URL,
                headers={
                    "Authorization": "Bearer sk-or-v1-FAKEKEYFORSMOKETEST",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "black-forest-labs/flux.2-klein",
                    "prompt": "test",
                    "n": 1,
                },
            )
    except httpx.ConnectError:
        pytest.skip("network unavailable")

    # Must be a client error (4xx) — not a 2xx success or server error.
    assert resp.status_code != 200, (
        f"expected non-200 from bad-key request, got {resp.status_code}"
    )
    assert resp.status_code < 500, (
        f"expected 4xx from bad-key request, got server error {resp.status_code}"
    )

    # Best-effort: if the response is JSON (as per OpenAI-compat spec), it must
    # have an 'error' key. HTML responses from edge/CDN auth gates are also
    # acceptable — the endpoint exists and is protected.
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = resp.json()
        except Exception:
            pytest.fail(f"content-type is JSON but body is not parseable: {resp.text[:200]}")
        assert "error" in body, (
            f"expected 'error' key in JSON response, got: {list(body.keys())}"
        )


def test_openrouter_img_url_constant():
    """OPENROUTER_IMG_URL must point at the OpenRouter images/generations endpoint."""
    from tools.photos._shared import OPENROUTER_IMG_URL
    assert OPENROUTER_IMG_URL.startswith("https://openrouter.ai/api/v1/images/")


@pytest.mark.asyncio
async def test_call_flux_returns_none_without_api_key(monkeypatch):
    """_call_flux returns None and logs a warning when OPENROUTER_API_KEY is absent."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from tools.photos._shared import _call_flux
    result = await _call_flux("test prompt", "black-forest-labs/flux.2-klein")
    assert result is None
