"""OAuth 2.1 + PKCE + Dynamic Client Registration for the external MCP server.

Implements the minimum surface ChatGPT / claude.ai / iPhone need to add a
custom-connector against ``mcp_external`` over Cloudflare Tunnel:

* RFC 9728 protected-resource metadata
* RFC 8414 authorization-server metadata
* RFC 7591 open dynamic client registration (public clients, no secret)
* RFC 7636 PKCE S256 (no ``plain``)
* Authorization code + refresh-token grants with rotation + family revocation

Design — locked, don't second-guess:
* Single-user system. Consent is a one-input passphrase form at /authorize,
  compared constant-time against ``HIKARI_OAUTH_OWNER_PASSPHRASE``.
* /authorize params are stashed in a signed cookie via ``itsdangerous`` so
  that the POST submission can't be forged with attacker-controlled hidden
  form fields (confused-deputy mitigation).
* Tokens are opaque random strings stored in SQLite — see
  ``storage.db.oauth_*`` for the persistence layer.
* No errors distinguish between "wrong code" vs "wrong verifier" — the
  token endpoint always returns ``invalid_grant`` so callers can't probe.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
from typing import Any
from urllib.parse import urlencode, urlparse

from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeTimedSerializer,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from agents import config as cfg
from storage import db

from ._rate_limit import passphrase_limiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------

_PASSPHRASE_ENV = "HIKARI_OAUTH_OWNER_PASSPHRASE"
_COOKIE_SECRET_ENV = "HIKARI_OAUTH_COOKIE_SECRET"
_MCP_SECRET_ENV = "HIKARI_MCP_SECRET"
_STATE_COOKIE = "hikari_oauth_state"
_STATE_TTL_SECONDS = 600
_SIGNER_SALT = "hikari-oauth-authorize-state"


def _access_ttl() -> int:
    return int(cfg.get("mcp_external.oauth.access_token_ttl_seconds", 3600))


def _refresh_ttl() -> int:
    return int(cfg.get("mcp_external.oauth.refresh_token_ttl_seconds", 2592000))


def _code_ttl() -> int:
    return int(cfg.get("mcp_external.oauth.auth_code_ttl_seconds", 600))


def _scopes_supported() -> list[str]:
    raw = cfg.get("mcp_external.oauth.scopes_supported", ["mcp"])
    return list(raw) if raw else ["mcp"]


def _passphrase_window_seconds() -> int:
    # Default matches mcp_external/_rate_limit.py's RateLimiter default so the
    # Retry-After header doesn't lie about the actual rate-limit window when
    # the yaml key is absent (test envs etc.).
    return int(cfg.get("mcp_external.oauth.passphrase_window_seconds", 300))


def _public_base_url(request: Request) -> str:
    """Configured public origin (used in discovery docs) or fall back to the
    inbound request's scheme+host. Trailing slash stripped."""
    configured = cfg.get("mcp_external.public_base_url")
    if configured:
        return str(configured).rstrip("/")
    url = request.url
    host = url.netloc or url.hostname or ""
    return f"{url.scheme}://{host}".rstrip("/")


def _cookie_signing_key() -> bytes:
    """Resolve the signing key for the /authorize state cookie.

    Prefer a dedicated ``HIKARI_OAUTH_COOKIE_SECRET``. Otherwise derive a
    stable key from ``HIKARI_MCP_SECRET`` via HKDF-style HMAC. Refuse to
    operate without one — silent fallback would let an attacker forge state.
    """
    explicit = os.environ.get(_COOKIE_SECRET_ENV, "").strip()
    if explicit:
        return explicit.encode("utf-8")
    base = os.environ.get(_MCP_SECRET_ENV, "").strip()
    if not base:
        raise RuntimeError(
            f"oauth: neither {_COOKIE_SECRET_ENV} nor {_MCP_SECRET_ENV} is set; "
            "refusing to sign authorize-state cookies."
        )
    return hmac.new(base.encode("utf-8"), b"oauth-cookie-secret", hashlib.sha256).digest()


def _state_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_cookie_signing_key(), salt=_SIGNER_SALT)


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client and client.host else "unknown"


def _is_https(request: Request) -> bool:
    """True if the externally-visible request is HTTPS.

    Direct check first, then ``X-Forwarded-Proto`` (Cloudflare Tunnel and
    most reverse proxies set this), then the config flag
    ``mcp_external.behind_tls_proxy`` (which the operator sets to True when
    the server runs behind a TLS-terminating proxy on plain HTTP locally —
    in that case the inbound scheme is always ``http`` even though the
    public connection is HTTPS, and we still want to mark cookies Secure).
    """
    scheme = (request.url.scheme or "").lower()
    if scheme == "https":
        return True
    fwd = request.headers.get("x-forwarded-proto", "").lower()
    if fwd and "https" in fwd.split(",")[0].strip():
        return True
    if bool(cfg.get("mcp_external.behind_tls_proxy", False)):
        return True
    return False


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------

def _valid_http_url(value: str) -> bool:
    """An http(s) URL with no embedded credentials.

    Embedded credentials (``https://attacker@victim.com/cb``) are rejected
    because the netloc-truthy check would otherwise pass — and at redirect
    time the browser sends the auth code to a URL the attacker controlled
    the construction of, which is an open-DCR exploit vector.
    """
    try:
        p = urlparse(value)
    except (ValueError, TypeError):
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.netloc:
        return False
    if p.username or p.password:
        return False
    return True


def _redirect_with_error(redirect_uri: str, state: str | None,
                        error: str, description: str | None = None) -> RedirectResponse:
    params = {"error": error}
    if description:
        params["error_description"] = description
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


# ---------------------------------------------------------------------------
# 1. RFC 9728 — protected resource metadata
# ---------------------------------------------------------------------------

async def protected_resource_metadata(request: Request) -> JSONResponse:
    base = _public_base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": _scopes_supported(),
        "bearer_methods_supported": ["header"],
    })


# ---------------------------------------------------------------------------
# 2. RFC 8414 — authorization server metadata
# ---------------------------------------------------------------------------

async def authorization_server_metadata(request: Request) -> JSONResponse:
    base = _public_base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": _scopes_supported(),
    })


# ---------------------------------------------------------------------------
# 3. RFC 7591 — dynamic client registration
# ---------------------------------------------------------------------------

def _dcr_error(description: str) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_client_metadata", "error_description": description},
        status_code=400,
    )


async def register_client(request: Request) -> JSONResponse:
    """Open DCR — anyone can mint a public client_id. PKCE is the gate."""
    try:
        body: Any = await request.json()
    except Exception:
        return _dcr_error("body must be application/json")
    if not isinstance(body, dict):
        return _dcr_error("body must be a JSON object")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _dcr_error("redirect_uris must be a non-empty list")
    if not all(isinstance(u, str) for u in redirect_uris):
        return _dcr_error("redirect_uris entries must be strings")
    for u in redirect_uris:
        if not _valid_http_url(u):
            return _dcr_error(f"redirect_uri not an http(s) URL: {u!r}")

    client_name_raw = body.get("client_name")
    client_name = client_name_raw if isinstance(client_name_raw, str) else None

    try:
        reg = db.oauth_client_register(client_name, redirect_uris)
    except Exception:
        logger.exception("oauth: client registration failed")
        return _dcr_error("registration failed")

    db.oauth_audit(
        "register",
        reg["client_id"],
        _client_ip(request),
        {"client_name": client_name, "redirect_uris": redirect_uris},
    )
    return JSONResponse(reg, status_code=201)


# ---------------------------------------------------------------------------
# 4 + 5. /authorize (GET form, POST submission)
# ---------------------------------------------------------------------------

_AUTHORIZE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>hikari · authorize</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{background:#0f0f12;color:#e8e8ea;font:14px/1.5 ui-monospace,Menlo,monospace;
       margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
  .card{background:#17171b;border:1px solid #27272b;border-radius:12px;
        padding:28px 32px;max-width:380px;width:100%}
  h1{font-size:16px;margin:0 0 4px;font-weight:600;letter-spacing:.02em}
  .sub{color:#9a9aa3;margin-bottom:20px;font-size:13px}
  .row{margin-bottom:14px}
  label{display:block;font-size:12px;color:#9a9aa3;margin-bottom:6px}
  input[type=password]{width:100%;box-sizing:border-box;background:#0f0f12;
        border:1px solid #2a2a30;border-radius:6px;color:#e8e8ea;
        padding:9px 11px;font:13px ui-monospace,Menlo,monospace}
  input[type=password]:focus{outline:none;border-color:#5b6cff}
  button{width:100%;background:#5b6cff;color:#fff;border:0;border-radius:6px;
         padding:10px;font:13px ui-monospace,Menlo,monospace;cursor:pointer}
  button:hover{background:#4858e6}
  .err{background:#3a1c1f;border:1px solid #6a2a2f;color:#ffbcc0;
       padding:8px 10px;border-radius:6px;font-size:12px;margin-bottom:14px}
  .meta{color:#6a6a72;font-size:11px;margin-top:14px;word-break:break-all}
</style>
</head>
<body>
  <form class="card" method="POST" action="/authorize">
    <h1>authorize</h1>
    <div class="sub">__CLIENT__ wants access to hikari memory tools.</div>
    __ERROR__
    <div class="row">
      <label for="p">passphrase</label>
      <input id="p" type="password" name="passphrase" autocomplete="off" autofocus required>
    </div>
    <button type="submit">grant</button>
    <div class="meta">scope: __SCOPE__</div>
  </form>
</body>
</html>
"""


def _render_authorize_page(client_label: str, scope: str | None,
                          error: str | None = None) -> str:
    safe_client = html.escape(client_label or "an unknown client")
    safe_scope = html.escape(scope or "mcp")
    err_block = (
        f'<div class="err">{html.escape(error)}</div>' if error else ""
    )
    return (_AUTHORIZE_HTML
            .replace("__CLIENT__", safe_client)
            .replace("__SCOPE__", safe_scope)
            .replace("__ERROR__", err_block))


async def _authorize_get(request: Request) -> Response:
    q = request.query_params
    response_type = q.get("response_type")
    client_id = q.get("client_id")
    redirect_uri = q.get("redirect_uri")
    code_challenge = q.get("code_challenge")
    code_challenge_method = q.get("code_challenge_method")
    state = q.get("state")
    scope = q.get("scope")

    # Validate client first so we know whether it's safe to redirect errors.
    client = db.oauth_client_get(client_id) if client_id else None
    redirect_ok = (
        client is not None
        and redirect_uri
        and redirect_uri in client.get("redirect_uris", [])
    )

    missing: list[str] = []
    for name, val in (
        ("response_type", response_type),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("code_challenge", code_challenge),
        ("code_challenge_method", code_challenge_method),
        ("state", state),
    ):
        if not val:
            missing.append(name)
    if missing:
        msg = f"missing required parameter(s): {', '.join(missing)}"
        if redirect_ok:
            return _redirect_with_error(redirect_uri, state, "invalid_request", msg)
        return HTMLResponse(f"<h1>400</h1><p>{html.escape(msg)}</p>", status_code=400)

    if response_type != "code":
        if redirect_ok:
            return _redirect_with_error(redirect_uri, state, "unsupported_response_type")
        return HTMLResponse("<h1>400</h1><p>unsupported response_type</p>", status_code=400)
    if code_challenge_method != "S256":
        if redirect_ok:
            return _redirect_with_error(
                redirect_uri, state, "invalid_request",
                "code_challenge_method must be S256",
            )
        return HTMLResponse("<h1>400</h1><p>code_challenge_method must be S256</p>",
                            status_code=400)
    if not client:
        return HTMLResponse("<h1>400</h1><p>unknown client_id</p>", status_code=400)
    if not redirect_ok:
        return HTMLResponse(
            "<h1>400</h1><p>redirect_uri is not registered for this client</p>",
            status_code=400,
        )

    payload = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "state": state,
    }
    signed = _state_signer().dumps(payload)

    client_label = client.get("client_name") or client_id or "an unknown client"
    body = _render_authorize_page(str(client_label), scope)
    resp = HTMLResponse(body)
    cookie_kwargs: dict[str, Any] = {
        "max_age": _STATE_TTL_SECONDS,
        "path": "/authorize",
        "httponly": True,
        "samesite": "lax",
    }
    if _is_https(request):
        cookie_kwargs["secure"] = True
    resp.set_cookie(_STATE_COOKIE, signed, **cookie_kwargs)
    return resp


async def _authorize_post(request: Request) -> Response:
    ip = _client_ip(request)
    if not passphrase_limiter.check(ip):
        return Response(
            "rate limited\n",
            status_code=429,
            headers={"Retry-After": str(_passphrase_window_seconds())},
        )

    signed = request.cookies.get(_STATE_COOKIE)
    if not signed:
        return HTMLResponse(
            "<h1>400</h1><p>authorize session missing or expired; restart the flow</p>",
            status_code=400,
        )
    try:
        stash: dict[str, Any] = _state_signer().loads(signed, max_age=_STATE_TTL_SECONDS)
    except SignatureExpired:
        return HTMLResponse(
            "<h1>400</h1><p>authorize session expired; restart the flow</p>",
            status_code=400,
        )
    except BadSignature:
        return HTMLResponse(
            "<h1>400</h1><p>authorize session signature invalid</p>",
            status_code=400,
        )

    form = await request.form()
    submitted = str(form.get("passphrase") or "")
    expected = os.environ.get(_PASSPHRASE_ENV, "")
    if not expected:
        logger.error("oauth: %s is not set; refusing to authorize.", _PASSPHRASE_ENV)
        return HTMLResponse(
            "<h1>500</h1><p>owner passphrase not configured</p>",
            status_code=500,
        )

    if not hmac.compare_digest(submitted, expected):
        passphrase_limiter.record_failure(ip)
        db.oauth_audit("passphrase_fail", stash.get("client_id"), ip, {})
        client = db.oauth_client_get(stash["client_id"]) if stash.get("client_id") else None
        label = (client or {}).get("client_name") or stash.get("client_id") or ""
        body = _render_authorize_page(str(label), stash.get("scope"), error="incorrect")
        return HTMLResponse(body, status_code=401)

    # Match — mint the auth code and bounce back to the client.
    code = db.oauth_code_mint(
        client_id=stash["client_id"],
        redirect_uri=stash["redirect_uri"],
        code_challenge=stash["code_challenge"],
        code_challenge_method=stash["code_challenge_method"],
        scope=stash.get("scope"),
        ttl_seconds=_code_ttl(),
    )
    db.oauth_client_touch(stash["client_id"])
    db.oauth_audit(
        "authorize_granted",
        stash["client_id"],
        ip,
        {"redirect_uri": stash["redirect_uri"], "scope": stash.get("scope")},
    )

    params = {"code": code}
    if stash.get("state"):
        params["state"] = stash["state"]
    sep = "&" if "?" in stash["redirect_uri"] else "?"
    resp = RedirectResponse(
        f"{stash['redirect_uri']}{sep}{urlencode(params)}", status_code=302,
    )
    resp.delete_cookie(_STATE_COOKIE, path="/authorize")
    return resp


async def authorize(request: Request) -> Response:
    if request.method == "POST":
        return await _authorize_post(request)
    return await _authorize_get(request)


# ---------------------------------------------------------------------------
# 6. /token — authorization_code + refresh_token
# ---------------------------------------------------------------------------

_TOKEN_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _token_error(code: str = "invalid_grant",
                status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_TOKEN_NO_STORE)


def _token_success(access: str, refresh: str, scope: str | None) -> JSONResponse:
    body: dict[str, Any] = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": _access_ttl(),
    }
    if scope:
        body["scope"] = scope
    return JSONResponse(body, headers=_TOKEN_NO_STORE)


async def token(request: Request) -> Response:
    try:
        form = await request.form()
    except Exception:
        return _token_error()
    grant_type = (form.get("grant_type") or "").strip()
    ip = _client_ip(request)

    if grant_type == "authorization_code":
        code = (form.get("code") or "").strip()
        verifier = (form.get("code_verifier") or "").strip()
        client_id = (form.get("client_id") or "").strip()
        redirect_uri = (form.get("redirect_uri") or "").strip()
        if not (code and verifier and client_id and redirect_uri):
            return _token_error()
        consumed = db.oauth_code_consume(code, verifier)
        if not consumed:
            db.oauth_audit("token_denied", client_id, ip, {"reason": "code_consume_failed"})
            return _token_error()
        if consumed["client_id"] != client_id or consumed["redirect_uri"] != redirect_uri:
            # Code is already burned by consume(). Refuse and log.
            db.oauth_audit(
                "token_denied", client_id, ip,
                {"reason": "binding_mismatch"},
            )
            return _token_error()
        scope = consumed.get("scope")
        refresh = db.oauth_token_mint(
            client_id=client_id, token_type="refresh",
            scope=scope, ttl_seconds=_refresh_ttl(),
        )
        access = db.oauth_token_mint(
            client_id=client_id, token_type="access",
            parent_token=refresh, scope=scope, ttl_seconds=_access_ttl(),
        )
        db.oauth_client_touch(client_id)
        db.oauth_audit("token_issued", client_id, ip, {"scope": scope})
        return _token_success(access, refresh, scope)

    if grant_type == "refresh_token":
        old_refresh = (form.get("refresh_token") or "").strip()
        client_id = (form.get("client_id") or "").strip()
        if not (old_refresh and client_id):
            return _token_error()
        # Atomic validate+revoke. If two concurrent refresh requests race,
        # only one wins — the other gets None and bails. This also revokes
        # every access token minted under the old refresh in the same tx.
        consumed = db.oauth_token_consume_refresh(old_refresh, client_id)
        if not consumed:
            db.oauth_audit(
                "token_denied", client_id, ip,
                {"reason": "refresh_invalid_or_raced"},
            )
            return _token_error()
        scope = consumed.get("scope")
        new_refresh = db.oauth_token_mint(
            client_id=client_id, token_type="refresh",
            scope=scope, ttl_seconds=_refresh_ttl(),
        )
        new_access = db.oauth_token_mint(
            client_id=client_id, token_type="access",
            parent_token=new_refresh, scope=scope, ttl_seconds=_access_ttl(),
        )
        db.oauth_client_touch(client_id)
        db.oauth_audit("token_refreshed", client_id, ip, {"scope": scope})
        return _token_success(new_access, new_refresh, scope)

    return JSONResponse(
        {"error": "unsupported_grant_type"},
        status_code=400,
        headers=_TOKEN_NO_STORE,
    )


# ---------------------------------------------------------------------------
# routing exports
# ---------------------------------------------------------------------------

OAUTH_PATH_PREFIXES: tuple[str, ...] = (
    "/authorize",
    "/token",
    "/register",
    "/.well-known/",
)

oauth_routes: list[Route] = [
    Route(
        "/.well-known/oauth-protected-resource",
        protected_resource_metadata,
        methods=["GET"],
    ),
    Route(
        "/.well-known/oauth-authorization-server",
        authorization_server_metadata,
        methods=["GET"],
    ),
    Route("/register", register_client, methods=["POST"]),
    Route("/authorize", authorize, methods=["GET", "POST"]),
    Route("/token", token, methods=["POST"]),
]
