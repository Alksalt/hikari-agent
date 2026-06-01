"""Sprint A — Phase A regression tests for the external MCP boundary fixes.

Covers:
  * Fix 1: build_server() extends allowed_hosts from PUBLIC_BASE_URL env var.
  * Fix 2: _client_ip() prefers CF-Connecting-IP / X-Forwarded-For when trusted.
  * Fix 3: /register IP-keyed rate limiter kicks in after register_max_attempts.
  * Fix 4a: invalid resource indicator at /token returns invalid_target (not silent).
  * Fix 4b: token without resource gets aud bound to server base URL.
  * Fix 4c: launch.py _send_401 includes error=invalid_token JSON body on aud mismatch.
  * Fix 5: limit params clamped to [1, max_read_limit]; non-int rejected.
  * Fix 6: main() sets logging.Formatter.converter to time.gmtime (UTC).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import time
from pathlib import Path

import pytest

from agents import config
from storage import db

# ---------- shared fixtures ----------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-bearer-secret")
    monkeypatch.setenv("HIKARI_OAUTH_OWNER_PASSPHRASE", "correct-horse")
    monkeypatch.setenv("HIKARI_OAUTH_COOKIE_SECRET", "test-cookie-key-32-bytes-xxxxxxxx")
    importlib.reload(db)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            False if key == "mcp_external.behind_tls_proxy" else _orig(key, default)
        ),
    )
    from mcp_external._rate_limit import passphrase_limiter
    passphrase_limiter.reset()
    from mcp_external.oauth import register_limiter
    register_limiter.reset()
    yield


def _pkce_pair(verifier: str = "test-verifier-1234567890abcdefghijklmnop") -> tuple[str, str]:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _client():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from mcp_external.oauth import oauth_routes
    app = Starlette(routes=oauth_routes)
    return TestClient(app)


# ---------- Fix 1: build_server allowed_hosts reads PUBLIC_BASE_URL ----------

def test_build_server_extends_allowed_hosts_from_env(monkeypatch):
    """build_server() must include the PUBLIC_BASE_URL hostname in allowed_hosts
    when public_base_url_env points to PUBLIC_BASE_URL."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://hikari.example.com")
    # Ensure the config key is set to point at PUBLIC_BASE_URL.
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            "PUBLIC_BASE_URL" if key == "mcp_external.public_base_url_env"
            else _orig(key, default)
        ),
    )
    from mcp_external.server import build_server
    srv = build_server()
    # Inspect the security settings via srv.settings.transport_security.
    security = srv.settings.transport_security
    hosts = list(security.allowed_hosts)
    assert "hikari.example.com" in hosts, f"hostname missing from allowed_hosts: {hosts}"
    assert "hikari.example.com:*" in hosts


def test_build_server_no_env_no_config_uses_defaults(monkeypatch):
    """When neither PUBLIC_BASE_URL nor public_base_url is set,
    allowed_hosts only contains the default localhost entries."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            None if key in ("mcp_external.public_base_url_env",
                            "mcp_external.public_base_url")
            else _orig(key, default)
        ),
    )
    from mcp_external.server import build_server
    srv = build_server()
    security = srv.settings.transport_security
    hosts = set(security.allowed_hosts)
    assert hosts == {"127.0.0.1:*", "localhost:*", "[::1]:*"}


# ---------- Fix 2: _client_ip honours forwarded headers ----------

def _make_request(headers: dict[str, str], client_host: str = "127.0.0.1"):
    """Build a minimal Starlette Request with the given headers and client."""

    from starlette.requests import Request
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 12345),
    }
    return Request(scope)


def test_client_ip_returns_host_when_trust_disabled(monkeypatch):
    """Without trusted_forwarded_ip, always use request.client.host."""
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            False if key in ("mcp_external.trusted_forwarded_ip",
                             "mcp_external.behind_tls_proxy")
            else _orig(key, default)
        ),
    )
    from mcp_external.oauth import _client_ip
    req = _make_request(
        {"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"},
        client_host="127.0.0.1",
    )
    assert _client_ip(req) == "127.0.0.1"


def test_client_ip_prefers_cf_connecting_ip_when_trusted(monkeypatch):
    """When trusted_forwarded_ip + behind_tls_proxy, prefer CF-Connecting-IP."""
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            True if key in ("mcp_external.trusted_forwarded_ip",
                            "mcp_external.behind_tls_proxy")
            else _orig(key, default)
        ),
    )
    from mcp_external.oauth import _client_ip
    req = _make_request(
        {"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"},
        client_host="127.0.0.1",
    )
    assert _client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_to_xff_when_no_cf(monkeypatch):
    """When CF-Connecting-IP absent, use left-most X-Forwarded-For entry."""
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            True if key in ("mcp_external.trusted_forwarded_ip",
                            "mcp_external.behind_tls_proxy")
            else _orig(key, default)
        ),
    )
    from mcp_external.oauth import _client_ip
    req = _make_request(
        {"x-forwarded-for": "9.10.11.12, 192.168.1.1"},
        client_host="127.0.0.1",
    )
    assert _client_ip(req) == "9.10.11.12"


def test_client_ip_falls_back_to_host_when_no_headers(monkeypatch):
    """When trusted but no forwarded headers, fall back to client.host."""
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            True if key in ("mcp_external.trusted_forwarded_ip",
                            "mcp_external.behind_tls_proxy")
            else _orig(key, default)
        ),
    )
    from mcp_external.oauth import _client_ip
    req = _make_request({}, client_host="127.0.0.1")
    assert _client_ip(req) == "127.0.0.1"


# ---------- Fix 3: /register rate limit ----------

def test_register_rate_limit_blocks_after_max_attempts(monkeypatch):
    """/register is rate-limited per IP; exceeding the limit returns 429."""
    from mcp_external.oauth import register_limiter
    # Override max to a small value for the test.
    _original_max = register_limiter.max_attempts
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            3 if key == "mcp_external.oauth.register_max_attempts"
            else _orig(key, default)
        ),
    )
    register_limiter.reset()
    c = _client()
    # 3 bad requests (each records a failure internally).
    for _ in range(3):
        c.post("/register", json={"redirect_uris": ["javascript:x"]})
    # 4th → 429.
    r = c.post("/register",
               json={"redirect_uris": ["http://localhost/cb"]})
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}


def test_register_rate_limit_counts_successful_registrations(monkeypatch):
    """FIX 1 regression: well-formed /register calls MUST count toward the
    per-IP window even when they succeed.  A flood of valid DCR requests
    must be bounded — the (cap+1)-th well-formed request returns 429."""
    from mcp_external.oauth import register_limiter
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            3 if key == "mcp_external.oauth.register_max_attempts"
            else _orig(key, default)
        ),
    )
    register_limiter.reset()
    c = _client()
    # 3 well-formed registrations — each should succeed (201).
    for i in range(3):
        r = c.post(
            "/register",
            json={"client_name": f"client-{i}",
                  "redirect_uris": [f"http://localhost:{9000 + i}/cb"]},
        )
        assert r.status_code == 201, (
            f"registration {i} should succeed, got {r.status_code}: {r.text}"
        )
    # 4th well-formed request → 429 (cap exhausted by successes).
    r = c.post(
        "/register",
        json={"client_name": "flood-client",
              "redirect_uris": ["http://localhost:9999/cb"]},
    )
    assert r.status_code == 429, (
        f"expected 429 after cap, got {r.status_code}: {r.text}"
    )
    assert "retry-after" in {k.lower() for k in r.headers}


def test_register_happy_path_still_works():
    """A valid registration succeeds when under the rate limit."""
    c = _client()
    r = c.post("/register",
               json={"client_name": "sprint-a-test",
                     "redirect_uris": ["http://localhost:9999/cb"]})
    assert r.status_code == 201
    assert r.json()["client_id"]


# ---------- Fix 4a: invalid resource → invalid_target at /token ----------

def test_token_invalid_resource_returns_invalid_target():
    """If a non-http(s) resource is supplied at /token, return invalid_target."""
    c = _client()
    cid = c.post("/register", json={"redirect_uris": ["http://localhost/cb"]}).json()["client_id"]
    verifier, challenge = _pkce_pair()
    c.get("/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": "http://localhost/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "s",
    }, follow_redirects=False)
    r_post = c.post("/authorize", data={"passphrase": "correct-horse"},
                    follow_redirects=False)
    from urllib.parse import parse_qs, urlparse
    code = parse_qs(urlparse(r_post.headers["location"]).query)["code"][0]

    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost/cb",
        "resource": "javascript:evil()",  # invalid — not http(s)
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_target"


# ---------- Fix 4b: no-resource token gets aud bound to base URL ----------

def test_token_without_resource_has_aud_in_response():
    """When no resource is provided, the token response MUST include an aud
    bound to the server's own base URL (not missing)."""
    c = _client()
    cid = c.post("/register", json={"redirect_uris": ["http://localhost/cb"]}).json()["client_id"]
    verifier, challenge = _pkce_pair()
    c.get("/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": "http://localhost/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "s",
    }, follow_redirects=False)
    r_post = c.post("/authorize", data={"passphrase": "correct-horse"},
                    follow_redirects=False)
    from urllib.parse import parse_qs, urlparse
    code = parse_qs(urlparse(r_post.headers["location"]).query)["code"][0]

    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost/cb",
        # No resource parameter.
    })
    assert r.status_code == 200
    body = r.json()
    # aud must be present and non-empty.
    assert "aud" in body, f"aud missing from token response: {body}"
    assert body["aud"], "aud must be a non-empty string"


# ---------- Fix 4c: _send_401 structured error on aud mismatch ----------

@pytest.mark.asyncio
async def test_send_401_with_error_returns_json_body():
    """_send_401(error='invalid_token') sends a JSON body with error field."""
    from mcp_external.launch import AuthMiddleware

    async def fake_app(scope, receive, send):
        pass

    mw = AuthMiddleware(fake_app)
    sent: list[dict] = []

    async def _send(m):
        sent.append(m)

    await mw._send_401(
        {"server": ("127.0.0.1", 8765), "scheme": "http"},
        _send,
        error="invalid_token",
        error_description="audience mismatch",
    )
    start = sent[0]
    body = sent[1]
    assert start["status"] == 401
    hdrs = dict(start["headers"])
    assert b"application/json" in hdrs[b"content-type"]
    challenge = hdrs[b"www-authenticate"].decode()
    assert 'error="invalid_token"' in challenge

    import json as _json
    parsed = _json.loads(body["body"])
    assert parsed["error"] == "invalid_token"
    assert "audience mismatch" in parsed.get("error_description", "")


@pytest.mark.asyncio
async def test_send_401_no_error_returns_plain_text():
    """_send_401() without error keeps plain-text body for backward compat."""
    from mcp_external.launch import AuthMiddleware

    async def fake_app(scope, receive, send):
        pass

    mw = AuthMiddleware(fake_app)
    sent: list[dict] = []

    async def _send(m):
        sent.append(m)

    await mw._send_401(
        {"server": ("127.0.0.1", 8765), "scheme": "http"},
        _send,
    )
    hdrs = dict(sent[0]["headers"])
    assert b"text/plain" in hdrs[b"content-type"]
    assert sent[1]["body"] == b"401 unauthorized\n"


@pytest.mark.asyncio
async def test_middleware_rejects_token_without_aud_with_invalid_token_error(monkeypatch):
    """Token with no aud → 401 with JSON error=invalid_token body."""
    from mcp_external.launch import AuthMiddleware

    _TEST_BASE_URL = "https://hikari.example.com"
    monkeypatch.setenv("PUBLIC_BASE_URL", _TEST_BASE_URL)
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            "PUBLIC_BASE_URL" if key == "mcp_external.public_base_url_env"
            else (False if key == "mcp_external.behind_tls_proxy"
                  else _orig(key, default))
        ),
    )

    reg = db.oauth_client_register("t", ["http://localhost/cb"])
    # Mint with no scope (no aud: suffix).
    access = db.oauth_token_mint(reg["client_id"], "access", ttl_seconds=600, scope=None)

    async def fake_app(scope, receive, send):
        pass

    mw = AuthMiddleware(fake_app)
    sent: list[dict] = []

    async def _send(m):
        sent.append(m)

    scope = {
        "type": "http", "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {access}".encode())],
    }
    await mw(scope, lambda: None, _send)
    assert sent[0]["status"] == 401
    import json as _json
    body = _json.loads(sent[1]["body"])
    assert body["error"] == "invalid_token"


# ---------- Fix 5: limit clamping ----------

def test_clamp_limit_basic():
    from mcp_external.server import _clamp_limit
    # Normal values.
    assert _clamp_limit(10, 8) == 10
    assert _clamp_limit(0, 8) == 8    # 0 means "use default"
    assert _clamp_limit(None, 8) == 8  # None also means "use default"


def test_clamp_limit_enforces_max(monkeypatch):
    """Limits above _MAX_LIMIT are clamped down."""
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            20 if key == "mcp_external.max_read_limit"
            else _orig(key, default)
        ),
    )
    from mcp_external.server import _clamp_limit
    assert _clamp_limit(1000, 8) == 20
    assert _clamp_limit(21, 5) == 20
    assert _clamp_limit(20, 5) == 20
    assert _clamp_limit(19, 5) == 19


def test_clamp_limit_enforces_min(monkeypatch):
    """Limits below 1 are clamped up; 0 uses default."""
    from mcp_external.server import _clamp_limit
    assert _clamp_limit(-5, 8) == 1  # negative → clamped to 1
    assert _clamp_limit(0, 3) == 3   # 0 → treated as "use default"


def test_clamp_limit_rejects_non_int():
    from mcp_external.server import _clamp_limit
    with pytest.raises(ValueError, match="limit must be an integer"):
        _clamp_limit("abc", 8)  # type: ignore[arg-type]


# ---------- Fix 6: UTC logger ----------

def test_main_sets_utc_formatter():
    """main() must set logging.Formatter.converter = time.gmtime before
    handlers are installed, so log timestamps are UTC."""
    import logging as _logging
    original = _logging.Formatter.converter

    # We import launch and call main partially — just check the side-effect
    # of the Formatter.converter assignment without actually starting uvicorn.
    # We monkeypatch uvicorn.run to a no-op.
    import sys

    # Temporarily inject a no-op uvicorn.
    import types
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules.setdefault("uvicorn", fake_uvicorn)

    try:
        # Patch the parts that would try to start an actual server.
        import unittest.mock as mock

        from mcp_external import launch
        with mock.patch("mcp_external.launch._enabled", return_value=True), \
             mock.patch("mcp_external.launch.build_server") as mock_build, \
             mock.patch("uvicorn.run"):
            mock_srv = mock.MagicMock()
            mock_srv.streamable_http_app.return_value = mock.MagicMock()
            mock_build.return_value = mock_srv
            try:
                launch.main()
            except Exception:
                pass  # We only care about the side-effect, not completion.
        assert _logging.Formatter.converter is time.gmtime, (
            f"Formatter.converter should be time.gmtime, got {_logging.Formatter.converter}"
        )
    finally:
        _logging.Formatter.converter = original  # restore for other tests
