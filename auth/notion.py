"""Notion OAuth 2.1 provider with PKCE + DCR.

# Notion public OAuth returns a non-rotating long-lived access_token.
# No refresh endpoint, no rotation, no mutex needed. If a token is
# revoked by the user, run `scripts.auth notion grant` to re-authorize.

DCR endpoint: https://api.notion.com/v1/oauth/register
Authorize:    https://api.notion.com/v1/oauth/authorize
Token:        https://api.notion.com/v1/oauth/token

Flow:
1. DCR — POST /v1/oauth/register → {client_id, client_secret}.
   Persisted in keychain as 'hikari-notion-client' (JSON blob).
2. PKCE — generate code_verifier (32 random bytes, base64url) +
   code_challenge (S256 = base64url(sha256(verifier))).
3. Generate CSRF state = secrets.token_urlsafe(32).
4. Open browser at authorize URL with PKCE + state params.
5. Spin up a one-shot HTTP listener on 127.0.0.1:8765/callback.
   Verify state on callback; reject (400) on mismatch.
6. Exchange code → token; persist access_token in keychain 'hikari-notion'.

Provider class is registered in auth/providers.py and config/tools.yaml auth_providers block.
Use NotionOAuthProvider for the full OAuth path.
The legacy NotionProvider (PAT env-var) remains in auth/providers.py for
backwards compat; swap by updating config/tools.yaml auth_providers.notion.provider_class.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from auth.providers import Provider
from auth.store import TokenStore, default_store

logger = logging.getLogger(__name__)

_DCR_ENDPOINT = "https://api.notion.com/v1/oauth/register"
_AUTHORIZE_ENDPOINT = "https://api.notion.com/v1/oauth/authorize"
_TOKEN_ENDPOINT = "https://api.notion.com/v1/oauth/token"

_CALLBACK_PORT = 8765
_REDIRECT_URI = f"http://127.0.0.1:{_CALLBACK_PORT}/callback"

# Keychain key names.
_CLIENT_KEY = "client"      # JSON blob: {client_id, client_secret}
_TOKEN_KEY = "token"        # JSON blob: {access_token, workspace_id, scopes, issued_at}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


# CSRF state is stored in a module-level dict so _CallbackHandler can verify it.
_expected_state: dict[str, str] = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures the ?code= redirect param."""

    code: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # CSRF state verification.
        received_state = (params.get("state") or [None])[0]
        expected = _expected_state.get("value")
        if not received_state or received_state != expected:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(
                b"<html><body>state mismatch - possible CSRF. try again.</body></html>"
            )
            return

        code_list = params.get("code")
        if code_list:
            _CallbackHandler.code = code_list[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>authorized. you can close this tab.</body></html>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<html><body>no code. something went wrong.</body></html>")

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
        pass  # suppress default HTTP logging


def _run_local_server() -> str | None:
    """Spin up a one-shot HTTP server on _CALLBACK_PORT. Return the captured code or None."""
    _CallbackHandler.code = None
    server = HTTPServer(("127.0.0.1", _CALLBACK_PORT), _CallbackHandler)
    server.timeout = 120  # wait up to 2 minutes for the redirect
    server.handle_request()
    server.server_close()
    return _CallbackHandler.code


def dcr_register(redirect_uri: str = _REDIRECT_URI) -> dict:
    """POST /v1/oauth/register → {client_id, client_secret}.

    Persists the result to keychain and returns the raw response dict.
    """
    resp = httpx.post(
        _DCR_ENDPOINT,
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "hikari-agent",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    store = default_store()
    store.set("notion", _CLIENT_KEY, json.dumps({
        "client_id": data["client_id"],
        "client_secret": data["client_secret"],
    }))
    return data


def _load_client() -> dict | None:
    raw = default_store().get("notion", _CLIENT_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _load_token() -> dict | None:
    raw = default_store().get("notion", _TOKEN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _save_token(payload: dict) -> None:
    default_store().set("notion", _TOKEN_KEY, json.dumps(payload))


def run_pkce_flow() -> dict:
    """Full PKCE + DCR grant flow. Opens browser, waits for redirect.

    Returns the token response dict. Persists client + token to keychain.
    """
    client = _load_client()
    if not client:
        client = dcr_register()

    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    _expected_state["value"] = state

    params = {
        "client_id": client["client_id"],
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = f"{_AUTHORIZE_ENDPOINT}?{urlencode(params)}"

    print()
    print("=" * 70)
    print("OPEN THIS URL in your browser:")
    print()
    print(auth_url)
    print()
    print("Waiting for redirect on 127.0.0.1:8765/callback …")
    print("=" * 70)
    webbrowser.open(auth_url)

    code = _run_local_server()
    if not code:
        raise RuntimeError("notion PKCE flow: no authorization code received")

    resp = httpx.post(
        _TOKEN_ENDPOINT,
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": verifier,
        },
        auth=(client["client_id"], client["client_secret"]),
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()

    from datetime import UTC
    from datetime import datetime as _dt
    token_blob = {
        "access_token": data.get("access_token", ""),
        "workspace_id": data.get("workspace_id", ""),
        "workspace_name": data.get("workspace_name", ""),
        "bot_id": data.get("bot_id", ""),
        "owner": data.get("owner", {}),
        "scopes": data.get("scope", "*"),
        "issued_at": _dt.now(UTC).isoformat(),
    }
    _save_token(token_blob)
    return data


class NotionOAuthProvider(Provider):
    """Notion OAuth 2.1 provider (PKCE + DCR).

    Notion public OAuth returns a non-rotating long-lived access_token.
    current_scopes() returns {'_present'} when a token exists in keychain.
    No refresh endpoint exists; re-authorize via `scripts.auth notion grant`.
    """

    name = "notion"

    def __init__(self, store: TokenStore | None = None) -> None:
        self._store = store or default_store()

    async def current_scopes(self) -> set[str]:
        token = _load_token()
        if token and token.get("access_token"):
            return {"_present"}
        # Fall back to env var (legacy).
        if os.environ.get("NOTION_TOKEN"):
            return {"_present"}
        return set()

    async def refresh(self) -> str:
        """No-op: Notion does not issue rotating refresh tokens."""
        token = _load_token()
        if token:
            return str(token.get("access_token") or "")
        return os.environ.get("NOTION_TOKEN") or ""

    def revoke(self) -> None:
        """Delete keychain items for Notion (client + token)."""
        store = default_store()
        for key in (_CLIENT_KEY, _TOKEN_KEY):
            try:
                store.set("notion", key, "")
            except Exception:
                pass
        try:
            store.clear("notion")
        except Exception as exc:
            logger.debug("NotionOAuthProvider.revoke: %r", exc)
