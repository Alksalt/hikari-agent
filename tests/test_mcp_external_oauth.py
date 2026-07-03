"""Phase 14 — OAuth 2.1 + PKCE + DCR for the external MCP server.

Covers:
  * DCR (/register) happy + bad-input paths
  * Discovery (/.well-known/*) shape
  * /authorize GET param validation + cookie issuance
  * /authorize POST passphrase compare + rate limiter
  * /token authorization_code grant (happy + PKCE + binding + single-use)
  * /token refresh_token grant (rotation + family revocation)
  * AuthMiddleware: bearer + OAuth + 401 with WWW-Authenticate
  * RateLimiter sliding-window math
  * log_scrub redactions for OAuth secret shapes
"""

from __future__ import annotations

import base64
import hashlib
import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db

# ---------- shared fixtures ----------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh SQLite per test, schema sentinel reset, env scrubbed."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("HIKARI_MCP_SECRET", "test-bearer-secret")
    monkeypatch.setenv("HIKARI_OAUTH_OWNER_PASSPHRASE", "correct-horse")
    monkeypatch.setenv("HIKARI_OAUTH_COOKIE_SECRET", "test-cookie-key-32-bytes-xxxxxxxx")
    importlib.reload(db)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    # Isolate tests from operator config: TestClient uses plain HTTP, so
    # state cookies must NOT be Secure (Secure cookies are dropped on http://).
    # Force behind_tls_proxy=False here regardless of engagement.yaml.
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None, _orig=config.get: (
            False if key == "mcp_external.behind_tls_proxy" else _orig(key, default)
        ),
    )
    # Reset rate limiters — module-level singletons survive across tests.
    # register_limiter now counts every /register (success included), so without
    # a per-test reset the shared window trips after a handful of registrations.
    from mcp_external._rate_limit import passphrase_limiter
    from mcp_external.oauth import register_limiter
    passphrase_limiter.reset()
    register_limiter.reset()
    yield


def _pkce_pair(verifier: str = "test-verifier-1234567890abcdefghijklmnop") -> tuple[str, str]:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _client():
    """Build a Starlette TestClient against the OAuth routes only (no MCP mount)."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from mcp_external.oauth import oauth_routes
    app = Starlette(routes=oauth_routes)
    return TestClient(app)


# ---------- DCR ----------

def test_dcr_happy_path():
    c = _client()
    r = c.post(
        "/register",
        json={"client_name": "test-cli", "redirect_uris": ["http://localhost:9999/cb"]},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"]
    assert body["token_endpoint_auth_method"] == "none"
    assert "code" in body["response_types"]
    # Audit row written.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT event_type, client_id FROM oauth_audit_log WHERE event_type = ?",
            ("register",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["client_id"] == body["client_id"]


def test_dcr_rejects_missing_redirect_uris():
    c = _client()
    r = c.post("/register", json={"client_name": "x"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


def test_dcr_rejects_empty_redirect_uris():
    c = _client()
    r = c.post("/register", json={"redirect_uris": []})
    assert r.status_code == 400


def test_dcr_rejects_non_http_redirect_uri():
    c = _client()
    r = c.post("/register", json={"redirect_uris": ["javascript:alert(1)"]})
    assert r.status_code == 400


def test_dcr_rejects_non_json():
    c = _client()
    r = c.post("/register", content="not-json", headers={"content-type": "text/plain"})
    assert r.status_code == 400


# ---------- discovery ----------

def test_protected_resource_metadata():
    c = _client()
    r = c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert body["bearer_methods_supported"] == ["header"]
    assert body["scopes_supported"] == ["mcp"]
    assert body["authorization_servers"]


def test_authorization_server_metadata():
    c = _client()
    r = c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


# ---------- /authorize GET ----------

def _register_client(c, redirect_uri: str = "http://localhost:9999/cb") -> str:
    r = c.post("/register",
               json={"client_name": "t", "redirect_uris": [redirect_uri]})
    return r.json()["client_id"]


def test_authorize_get_renders_form_with_valid_params():
    c = _client()
    cid = _register_client(c)
    _, challenge = _pkce_pair()
    r = c.get("/authorize", params={
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": "http://localhost:9999/cb",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    }, follow_redirects=False)
    assert r.status_code == 200
    assert "passphrase" in r.text.lower()
    assert "hikari_oauth_state" in r.headers.get("set-cookie", "")


def test_authorize_get_rejects_missing_state():
    c = _client()
    cid = _register_client(c)
    _, challenge = _pkce_pair()
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": "http://localhost:9999/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }, follow_redirects=False)
    # client+redirect ok → error returned via redirect, not 400.
    assert r.status_code == 302
    assert "error=invalid_request" in r.headers["location"]


def test_authorize_get_rejects_non_s256():
    c = _client()
    cid = _register_client(c)
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": "http://localhost:9999/cb",
        "code_challenge": "x", "code_challenge_method": "plain",
        "state": "s",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "error=invalid_request" in r.headers["location"]


def test_authorize_get_rejects_unknown_client():
    c = _client()
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": "nope",
        "redirect_uri": "http://localhost:9999/cb",
        "code_challenge": "x", "code_challenge_method": "S256",
        "state": "s",
    }, follow_redirects=False)
    # Unknown client → plain 400 HTML (can't trust the redirect_uri).
    assert r.status_code == 400


def test_authorize_get_rejects_unregistered_redirect_uri():
    c = _client()
    cid = _register_client(c, "http://localhost:9999/cb")
    _, challenge = _pkce_pair()
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": "http://evil.example/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "s",
    }, follow_redirects=False)
    assert r.status_code == 400


# ---------- /authorize POST ----------

def _do_authorize_get(c, cid, challenge, state="xyz"):
    return c.get("/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": "http://localhost:9999/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state,
    }, follow_redirects=False)


def test_authorize_post_happy_path_redirects_with_code():
    c = _client()
    cid = _register_client(c)
    _, challenge = _pkce_pair()
    _do_authorize_get(c, cid, challenge)
    r = c.post("/authorize", data={"passphrase": "correct-horse"},
               follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("http://localhost:9999/cb?")
    assert "code=" in loc and "state=xyz" in loc
    # Audit row.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT event_type FROM oauth_audit_log WHERE event_type = 'authorize_granted'"
        ).fetchall()
    assert len(rows) == 1


def test_authorize_post_rejects_wrong_passphrase():
    c = _client()
    cid = _register_client(c)
    _, challenge = _pkce_pair()
    _do_authorize_get(c, cid, challenge)
    r = c.post("/authorize", data={"passphrase": "wrong"}, follow_redirects=False)
    assert r.status_code == 401
    assert "incorrect" in r.text.lower()
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT event_type FROM oauth_audit_log WHERE event_type = 'passphrase_fail'"
        ).fetchall()
    assert len(rows) == 1


def test_authorize_post_no_cookie_returns_400():
    c = _client()
    # No prior GET → no cookie.
    r = c.post("/authorize", data={"passphrase": "correct-horse"})
    assert r.status_code == 400


def test_authorize_post_rate_limit_kicks_in():
    c = _client()
    cid = _register_client(c)
    _, challenge = _pkce_pair()
    # Saturate the limiter (default max_attempts=5).
    for _ in range(5):
        _do_authorize_get(c, cid, challenge)
        c.post("/authorize", data={"passphrase": "wrong"})
    # 6th attempt → 429.
    _do_authorize_get(c, cid, challenge)
    r = c.post("/authorize", data={"passphrase": "wrong"})
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}


# ---------- /token: authorization_code grant ----------

def _full_dance_to_code(c) -> tuple[str, str, str]:
    """Run DCR + authorize + return (client_id, code, verifier)."""
    cid = _register_client(c)
    verifier, challenge = _pkce_pair()
    _do_authorize_get(c, cid, challenge)
    r = c.post("/authorize", data={"passphrase": "correct-horse"},
               follow_redirects=False)
    loc = r.headers["location"]
    # extract code= from the redirect URL
    from urllib.parse import parse_qs, urlparse
    code = parse_qs(urlparse(loc).query)["code"][0]
    return cid, code, verifier


def test_token_authorization_code_happy_path():
    c = _client()
    cid, code, verifier = _full_dance_to_code(c)
    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost:9999/cb",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] >= 60
    assert r.headers["cache-control"] == "no-store"


def test_token_pkce_verifier_mismatch_rejected():
    c = _client()
    cid, code, _ = _full_dance_to_code(c)
    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": "wrong-verifier",
        "client_id": cid, "redirect_uri": "http://localhost:9999/cb",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_code_single_use():
    c = _client()
    cid, code, verifier = _full_dance_to_code(c)
    r1 = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost:9999/cb",
    })
    assert r1.status_code == 200
    r2 = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost:9999/cb",
    })
    assert r2.status_code == 400


def test_token_client_id_mismatch_rejected():
    c = _client()
    cid, code, verifier = _full_dance_to_code(c)
    other = _register_client(c, "http://other.example/cb")
    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": other,  # wrong
        "redirect_uri": "http://localhost:9999/cb",
    })
    assert r.status_code == 400


def test_token_redirect_uri_mismatch_rejected():
    c = _client()
    cid, code, verifier = _full_dance_to_code(c)
    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid,
        "redirect_uri": "http://localhost:9999/different",
    })
    assert r.status_code == 400


def test_token_unsupported_grant_type():
    c = _client()
    r = c.post("/token", data={"grant_type": "client_credentials"})
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_token_expired_auth_code_rejected():
    """Codes have a TTL — expired ones should fail at /token redemption,
    distinctly from PKCE failure (though /token returns identical errors)."""
    c = _client()
    cid = _register_client(c)
    verifier, challenge = _pkce_pair()
    # Mint a code directly with negative TTL (immediately expired) — skips
    # the /authorize flow but uses the same code-consume path.
    code = db.oauth_code_mint(
        cid, "http://localhost:9999/cb", challenge, "S256", ttl_seconds=-1,
    )
    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost:9999/cb",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_redirect_uri_rejects_embedded_credentials_at_dcr():
    """Open DCR exploit guard — redirect_uri with embedded credentials
    (https://attacker@victim.com) must be rejected at registration time."""
    c = _client()
    r = c.post("/register", json={
        "client_name": "evil",
        "redirect_uris": ["https://attacker@victim.example/cb"],
    })
    assert r.status_code == 400


def test_token_endpoint_touches_client_last_used_at():
    """Successful token issuance bumps oauth_clients.last_used_at."""
    c = _client()
    cid, code, verifier = _full_dance_to_code(c)
    before = db.oauth_client_get(cid)
    assert before is not None
    last_used_before = before.get("last_used_at")
    c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost:9999/cb",
    })
    after = db.oauth_client_get(cid)
    assert after is not None
    last_used_after = after.get("last_used_at")
    assert last_used_after is not None
    assert last_used_after != last_used_before


def test_oauth_cleanup_sweeper_removes_expired_and_revoked():
    """oauth_cleanup_expired is meant to be called from the daily reflection;
    confirm both branches (expired codes/tokens, old-revoked tokens) work."""
    reg = db.oauth_client_register("sweeper", ["http://localhost/cb"])
    cid = reg["client_id"]
    # 1 expired code + 1 expired token + 1 fresh access token
    db.oauth_code_mint(cid, "http://localhost/cb", "x", "S256", ttl_seconds=-5)
    db.oauth_token_mint(cid, "access", ttl_seconds=-5)
    keep_token = db.oauth_token_mint(cid, "access", ttl_seconds=600)
    n = db.oauth_cleanup_expired()
    assert n == 2
    # Fresh token still validates (OAuth 2.1 path — checks oauth_tokens table).
    assert db._oauth2_token_validate(keep_token) is not None


def _cap_clients_at(monkeypatch, maxc: int) -> None:
    monkeypatch.setattr(
        db, "_cfg_get",
        lambda key, default=None, _orig=db._cfg_get: (
            maxc if key == "oauth.max_registered_clients" else _orig(key, default)
        ),
    )


def test_oauth_register_evicts_dead_client_on_ceiling(monkeypatch):
    """DCR is public; on a ceiling hit the oldest client with no tokens and no
    codes is evicted so the owner can still add a connector."""
    _cap_clients_at(monkeypatch, 2)
    reg1 = db.oauth_client_register("first", ["http://localhost/cb"])
    reg2 = db.oauth_client_register("second", ["http://localhost/cb"])
    # Make reg1 unambiguously the oldest so the eviction target is deterministic.
    with db._conn() as c:
        c.execute(
            "UPDATE oauth_clients SET created_at = '2000-01-01 00:00:00' "
            "WHERE client_id = ?",
            (reg1["client_id"],),
        )
    reg3 = db.oauth_client_register("third", ["http://localhost/cb"])
    assert reg3["client_id"]
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
    assert n == 2
    assert db.oauth_client_get(reg1["client_id"]) is None      # oldest evicted
    assert db.oauth_client_get(reg2["client_id"]) is not None
    assert db.oauth_client_get(reg3["client_id"]) is not None


def test_oauth_register_fails_when_all_clients_have_tokens(monkeypatch):
    """When every client at the ceiling holds a token, none is evictable — the
    registration fails rather than deleting a live connector."""
    _cap_clients_at(monkeypatch, 2)
    reg1 = db.oauth_client_register("live1", ["http://localhost/cb"])
    reg2 = db.oauth_client_register("live2", ["http://localhost/cb"])
    db.oauth_token_mint(reg1["client_id"], "access", ttl_seconds=600)
    db.oauth_token_mint(reg2["client_id"], "access", ttl_seconds=600)
    with pytest.raises(ValueError, match="ceiling"):
        db.oauth_client_register("third", ["http://localhost/cb"])
    assert db.oauth_client_get(reg1["client_id"]) is not None
    assert db.oauth_client_get(reg2["client_id"]) is not None


def test_oauth_token_consume_refresh_atomic():
    """Two concurrent rotation calls on the same refresh — only one wins."""
    reg = db.oauth_client_register("atomic", ["http://localhost/cb"])
    cid = reg["client_id"]
    refresh = db.oauth_token_mint(cid, "refresh", ttl_seconds=600)
    # Also create an access child to confirm family sweep.
    access_child = db.oauth_token_mint(
        cid, "access", parent_token=refresh, ttl_seconds=600,
    )
    first = db.oauth_token_consume_refresh(refresh, cid)
    second = db.oauth_token_consume_refresh(refresh, cid)
    assert first is not None
    assert second is None  # racing caller loses
    # Child access also revoked in the same transaction.
    assert db._oauth2_token_validate(access_child) is None


def test_oauth_token_consume_refresh_wrong_client_rejected():
    reg1 = db.oauth_client_register("a", ["http://localhost/cb"])
    reg2 = db.oauth_client_register("b", ["http://localhost/cb"])
    refresh = db.oauth_token_mint(reg1["client_id"], "refresh", ttl_seconds=600)
    assert db.oauth_token_consume_refresh(refresh, reg2["client_id"]) is None
    # And it must NOT have been revoked by the failed attempt (OAuth 2.1 path).
    assert db._oauth2_token_validate(refresh) is not None


# ---------- /token: refresh_token grant + rotation ----------

def _get_initial_tokens(c) -> tuple[str, str, str]:
    cid, code, verifier = _full_dance_to_code(c)
    r = c.post("/token", data={
        "grant_type": "authorization_code",
        "code": code, "code_verifier": verifier,
        "client_id": cid, "redirect_uri": "http://localhost:9999/cb",
    })
    body = r.json()
    return cid, body["access_token"], body["refresh_token"]


def test_refresh_token_rotates_and_revokes_old_family():
    c = _client()
    cid, access1, refresh1 = _get_initial_tokens(c)
    r = c.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh1, "client_id": cid,
    })
    assert r.status_code == 200
    body = r.json()
    access2, refresh2 = body["access_token"], body["refresh_token"]
    assert access2 != access1
    assert refresh2 != refresh1
    # Old refresh AND old access (its child) are revoked.
    # These use the OAuth 2.1 path (oauth_tokens table) since they were minted via oauth_token_mint.
    assert db._oauth2_token_validate(refresh1) is None
    assert db._oauth2_token_validate(access1) is None
    # New pair works.
    assert db._oauth2_token_validate(access2)
    assert db._oauth2_token_validate(refresh2)


def test_refresh_token_reuse_after_rotation_rejected():
    c = _client()
    cid, _, refresh1 = _get_initial_tokens(c)
    c.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh1, "client_id": cid,
    })
    # Replay old refresh → 400.
    r2 = c.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh1, "client_id": cid,
    })
    assert r2.status_code == 400


def test_refresh_token_wrong_client_id_rejected():
    c = _client()
    cid, _, refresh1 = _get_initial_tokens(c)
    other = _register_client(c, "http://other.example/cb")
    r = c.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh1, "client_id": other,
    })
    assert r.status_code == 400


# ---------- AuthMiddleware ----------

@pytest.mark.asyncio
async def test_middleware_oauth_path_bypasses_auth():
    """OAuth dance/discovery paths must be reachable without Authorization."""
    from mcp_external.launch import AuthMiddleware
    inner_called = {"v": False}

    async def fake_app(scope, receive, send):
        inner_called["v"] = True

    mw = AuthMiddleware(fake_app)
    scope = {"type": "http", "path": "/register", "headers": []}
    await mw(scope, lambda: None, lambda msg: None)
    assert inner_called["v"]


@pytest.mark.asyncio
async def test_middleware_well_known_path_bypasses_auth():
    from mcp_external.launch import AuthMiddleware
    inner_called = {"v": False}

    async def fake_app(scope, receive, send):
        inner_called["v"] = True

    mw = AuthMiddleware(fake_app)
    scope = {"type": "http",
             "path": "/.well-known/oauth-protected-resource",
             "headers": []}
    await mw(scope, lambda: None, lambda msg: None)
    assert inner_called["v"]


@pytest.mark.asyncio
async def test_middleware_accepts_valid_oauth_access_token(monkeypatch):
    from mcp_external.launch import AuthMiddleware
    # Sprint A: public_base_url is now resolved via PUBLIC_BASE_URL env var
    # (public_base_url_env: PUBLIC_BASE_URL in engagement.yaml).
    # Set the env var so the audience validation matches the token's aud claim.
    _TEST_BASE_URL = "https://hikari.alksalt.com"
    monkeypatch.setenv("PUBLIC_BASE_URL", _TEST_BASE_URL)
    reg = db.oauth_client_register("t", ["http://localhost/cb"])
    access = db.oauth_token_mint(
        reg["client_id"], "access", ttl_seconds=600,
        scope=f"mcp aud:{_TEST_BASE_URL}"
    )

    inner_called = {"v": False}

    async def fake_app(scope, receive, send):
        inner_called["v"] = True

    mw = AuthMiddleware(fake_app)
    scope = {
        "type": "http", "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {access}".encode())],
    }
    await mw(scope, lambda: None, lambda msg: None)
    assert inner_called["v"]
    # Scope state populated.
    assert scope["state"]["auth_method"] == "oauth"
    assert scope["state"]["oauth_client_id"] == reg["client_id"]


@pytest.mark.asyncio
async def test_middleware_rejects_refresh_token_in_authorization_header():
    """Refresh tokens are NOT bearer-usable — only access tokens."""
    from mcp_external.launch import AuthMiddleware
    reg = db.oauth_client_register("t", ["http://localhost/cb"])
    refresh = db.oauth_token_mint(reg["client_id"], "refresh", ttl_seconds=600)
    sent: list = []

    async def fake_app(scope, receive, send):
        pass  # should not be called

    mw = AuthMiddleware(fake_app)
    scope = {
        "type": "http", "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {refresh}".encode())],
    }
    async def _send(m):
        sent.append(m)
    await mw(scope, lambda: None, _send)
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_middleware_401_includes_www_authenticate_with_resource_metadata():
    from mcp_external.launch import AuthMiddleware

    async def fake_app(scope, receive, send):
        pass

    mw = AuthMiddleware(fake_app)
    sent: list = []
    scope = {"type": "http", "path": "/mcp",
             "headers": [],
             "server": ("127.0.0.1", 8765),
             "scheme": "http"}
    async def _send(m):
        sent.append(m)
    await mw(scope, lambda: None, _send)
    assert sent[0]["status"] == 401
    hdrs = dict(sent[0]["headers"])
    challenge = hdrs[b"www-authenticate"].decode()
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource" in challenge


@pytest.mark.asyncio
async def test_middleware_expired_oauth_token_rejected():
    from mcp_external.launch import AuthMiddleware
    reg = db.oauth_client_register("t", ["http://localhost/cb"])
    expired = db.oauth_token_mint(reg["client_id"], "access", ttl_seconds=-10)
    sent: list = []

    async def fake_app(scope, receive, send):
        pass

    mw = AuthMiddleware(fake_app)
    scope = {"type": "http", "path": "/mcp",
             "headers": [(b"authorization", f"Bearer {expired}".encode())]}
    async def _send(m):
        sent.append(m)
    await mw(scope, lambda: None, _send)
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_middleware_rejects_oauth_token_without_aud():
    """RFC 8707 — access tokens without an audience binding must be rejected."""
    from mcp_external.launch import AuthMiddleware
    reg = db.oauth_client_register("t", ["http://localhost/cb"])
    # Mint without any scope (no aud: suffix).
    access = db.oauth_token_mint(reg["client_id"], "access", ttl_seconds=600, scope=None)
    sent: list = []

    async def fake_app(scope, receive, send):
        pass  # must not be reached

    mw = AuthMiddleware(fake_app)
    scope = {
        "type": "http", "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {access}".encode())],
    }
    async def _send(m):
        sent.append(m)
    await mw(scope, lambda: None, _send)
    assert sent[0]["status"] == 401


# ---------- RateLimiter math ----------

def test_rate_limiter_sliding_window():
    from mcp_external._rate_limit import RateLimiter
    rl = RateLimiter(
        max_attempts_key="nope.max", window_seconds_key="nope.window",
        max_attempts_default=3, window_seconds_default=60,
    )
    ip = "1.2.3.4"
    assert rl.check(ip)
    for _ in range(3):
        rl.record_failure(ip)
    assert not rl.check(ip)
    rl.reset(ip)
    assert rl.check(ip)


# ---------- log_scrub patterns ----------

def _apply_scrub(text: str) -> str:
    from agents.log_scrub import _PATTERNS
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def test_log_scrub_redacts_oauth_secret_shapes():
    long = "A" * 50  # easily clears the 32+ urlsafe-char threshold
    cases = [
        (f'"client_secret": "{long}"', "[REDACTED-OAUTH-CLIENT-SECRET]"),
        (f'"access_token": "{long}"', "[REDACTED-OAUTH-ACCESS-TOKEN]"),
        (f'"refresh_token": "{long}"', "[REDACTED-OAUTH-REFRESH-TOKEN]"),
        (f'"code_verifier": "{long}"', "[REDACTED-OAUTH-CODE-VERIFIER]"),
        (f"http://x/cb?code={long}&state=x", "[REDACTED-OAUTH-CODE]"),
    ]
    for sample, marker in cases:
        out = _apply_scrub(sample)
        assert long not in out, f"raw secret not redacted in: {sample!r}"
        assert marker in out, f"missing redaction marker in: {out!r}"


def test_log_scrub_does_not_over_redact_english():
    benign = [
        "the access token expires in 1 hour",
        "code verifier mismatch in the spec",
        "client secret should be rotated",
    ]
    for s in benign:
        # Replacement should not be triggered — the patterns require the literal
        # token name followed by a quote/colon/equals + 32+ char base64url body.
        assert "REDACTED" not in _apply_scrub(s), f"over-redaction in: {s!r}"
