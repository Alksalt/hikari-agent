"""Entrypoint for the external MCP server.

Usage::

    uv run python -m mcp_external.launch

Wraps the FastMCP Streamable HTTP app with an auth middleware that accepts
EITHER the static service-token bearer (``HIKARI_MCP_SECRET``) OR an OAuth 2.1
access token issued via the local OAuth endpoints. Composes a parent Starlette
app that mounts both the OAuth dance routes and the FastMCP HTTP app.

The intended deployment is BEHIND Cloudflare Tunnel — the tunnel terminates
TLS at the edge and forwards to ``127.0.0.1:<bind_port>`` here. See
``scripts/install_cloudflared.md`` for the tunnel setup.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys

from agents import config as cfg
from agents.log_scrub import install_root_filter
from storage import db

from . import server as server_module
from .server import build_server

logger = logging.getLogger(__name__)

# Sentinel prefix used by oauth.py to encode RFC 8707 audience into scope field.
_AUD_SCOPE_PREFIX = " aud:"


def _extract_token_aud(scope: str | None) -> str | None:
    """Parse the " aud:<uri>" suffix that oauth.py encodes into the scope field.

    Returns the bound audience URI, or None if no audience was encoded.
    Tokens without an audience claim bypass audience validation (backward compat).
    """
    if not scope:
        return None
    idx = scope.find(_AUD_SCOPE_PREFIX)
    if idx == -1:
        return None
    aud = scope[idx + len(_AUD_SCOPE_PREFIX):].strip()
    return aud or None


def _enabled() -> bool:
    return bool(cfg.get("mcp_external.enabled", False))


def _bind_host() -> str:
    return str(cfg.get("mcp_external.bind_host", "127.0.0.1"))


def _bind_port() -> int:
    return int(cfg.get("mcp_external.bind_port", 8765))


def _secret_env_name() -> str:
    return str(cfg.get("mcp_external.secret_env", "HIKARI_MCP_SECRET"))


def _public_base_url(scope: dict) -> str:
    """Return the externally-visible base URL for building OAuth metadata
    pointers. Prefer the configured ``mcp_external.public_base_url``; fall back
    to reconstructing from the ASGI ``scope`` (scheme + host)."""
    configured = cfg.get("mcp_external.public_base_url")
    if configured:
        return str(configured).rstrip("/")
    # Derive from scope. ``scope["server"]`` is (host, port) or None.
    server = scope.get("server") or ("127.0.0.1", 0)
    host, port = server[0], server[1]
    # ASGI doesn't always expose scheme; default to "http" behind the tunnel.
    scheme = scope.get("scheme") or "http"
    if (scheme == "http" and port and port != 80) or (
        scheme == "https" and port and port != 443
    ):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


class AuthMiddleware:
    """Authenticates HTTP requests via either:

      (a) static bearer matching ``HIKARI_MCP_SECRET`` (service token), or
      (b) OAuth 2.1 access token from the local ``oauth_tokens`` table.

    OAuth discovery + dance endpoints under ``OAUTH_PATH_PREFIXES`` bypass
    auth — they need to be reachable BY the unauthenticated client to
    complete the OAuth flow.

    Per-request auth attribution is recorded into ``server._auth_context`` so
    audit log rows attribute calls to the right principal.
    """

    def __init__(self, app):
        self.app = app
        # Import lazily so the module is importable even before Worker 1 lands
        # ``mcp_external/oauth.py`` in the tree. Worst case (oauth missing):
        # no bypass paths, but bearer-only auth still works.
        try:
            from .oauth import OAUTH_PATH_PREFIXES  # type: ignore
            self._oauth_prefixes = tuple(OAUTH_PATH_PREFIXES)
        except Exception:  # pragma: no cover — defensive for parallel build
            self._oauth_prefixes = ()

    async def __call__(self, scope, receive, send):
        # Non-HTTP (lifespan, websocket) traffic passes through unauthenticated.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        # OAuth dance + discovery endpoints MUST be reachable unauthenticated.
        if self._oauth_prefixes and path.startswith(self._oauth_prefixes):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_auth = headers.get(b"authorization", b"").decode(
            "latin-1", errors="ignore"
        ).strip()
        # Strip optional "Bearer " prefix (case-insensitive).
        token = raw_auth
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        auth_info: dict | None = None

        # Path 1: static service-token bearer.
        if token:
            configured_secret = os.environ.get(_secret_env_name(), "")
            if configured_secret and secrets.compare_digest(
                token, configured_secret
            ):
                auth_info = {"auth_method": "bearer"}

        # Path 2a: hashed bearer token (oauth_token_hashes table).
        if auth_info is None and token:
            try:
                row = db.oauth_token_validate(token)
            except Exception:  # pragma: no cover — DB hiccup shouldn't 500 to 401
                logger.exception("auth middleware: oauth_token_validate (hashed) failed")
                row = None
            if row:
                auth_info = {
                    "auth_method": "bearer_hashed",
                    "oauth_owner": row.get("owner"),
                }

        # Path 2b: full OAuth 2.1 access token (oauth_tokens table).
        if auth_info is None and token:
            try:
                row2 = db._oauth2_token_validate(token)
            except Exception:  # pragma: no cover — DB hiccup shouldn't 500 to 401
                logger.exception("auth middleware: _oauth2_token_validate failed")
                row2 = None
            if row2 and row2.get("token_type") == "access":
                # RFC 8707 audience validation: token MUST carry an aud binding.
                # Tokens without aud are rejected — spec requires audience binding.
                token_aud = _extract_token_aud(row2.get("scope"))
                if token_aud is None:
                    logger.warning(
                        "auth middleware: RFC 8707 — token has no audience binding; "
                        "rejecting (cycle this token)"
                    )
                    await self._send_401(scope, send)
                    return
                server_base = _public_base_url(scope).rstrip("/")
                if token_aud.rstrip("/") != server_base:
                    logger.warning(
                        "auth middleware: RFC 8707 audience mismatch — "
                        "token aud=%r, server=%r; rejecting",
                        token_aud, server_base,
                    )
                    await self._send_401(scope, send)
                    return
                auth_info = {
                    "auth_method": "oauth",
                    "oauth_client_id": row2.get("client_id"),
                }

        if auth_info is None:
            await self._send_401(scope, send)
            return

        # Record attribution into per-request scope state (preserve any
        # existing state dict the server may have populated during lifespan).
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state.update(auth_info)

        # Propagate to the contextvar so FastMCP-handled tool calls can read it.
        ctx_token = server_module.set_auth_context(auth_info)
        try:
            await self.app(scope, receive, send)
        finally:
            server_module.reset_auth_context(ctx_token)

    async def _send_401(self, scope, send) -> None:
        base = _public_base_url(scope)
        challenge = (
            f'Bearer realm="hikari", '
            f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
        )
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"www-authenticate", challenge.encode("latin-1")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b"401 unauthorized\n",
        })


# Backward-compat alias — the existing tests import this name. The class now
# handles both bearer and OAuth, but bearer-only flows behave identically to
# the old single-purpose middleware.
BearerAuthMiddleware = AuthMiddleware


def main() -> int:
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    _log_dir = Path(__file__).resolve().parent.parent / "data" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    _rot = RotatingFileHandler(
        _log_dir / "mcp_external.log",
        maxBytes=20_000_000, backupCount=5, encoding="utf-8",
    )
    _rot.setFormatter(_fmt)
    _stderr = logging.StreamHandler()
    _stderr.setFormatter(_fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_rot)
    root.addHandler(_stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    install_root_filter()

    # Match telegram_bridge: load .env so HIKARI_MCP_SECRET / HIKARI_OAUTH_*
    # are visible even when launched outside an env-aware shell.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not _enabled():
        logger.error(
            "mcp_external is disabled (config: mcp_external.enabled). "
            "Flip the flag and set HIKARI_MCP_SECRET and/or "
            "HIKARI_OAUTH_OWNER_PASSPHRASE first."
        )
        return 2

    secret_env = _secret_env_name()
    bearer_set = bool(os.environ.get(secret_env))
    oauth_set = bool(os.environ.get("HIKARI_OAUTH_OWNER_PASSPHRASE"))
    if not (bearer_set or oauth_set):
        logger.error(
            "neither %s nor HIKARI_OAUTH_OWNER_PASSPHRASE is set — refusing to "
            "start an unauthenticated server. Set at least one.",
            secret_env,
        )
        return 3
    if not bearer_set:
        logger.info(
            "%s not set — running OAuth-only (no service-token shortcut).",
            secret_env,
        )
    if not oauth_set:
        logger.info(
            "HIKARI_OAUTH_OWNER_PASSPHRASE not set — running bearer-only "
            "(OAuth dance disabled at the owner-approval step)."
        )

    # Build the FastMCP server + its raw ASGI app. FastMCP.streamable_http_app()
    # returns a plain ASGI callable (NOT a mountable Starlette app), so we wrap
    # it via Mount("/", app=...) inside a parent Starlette to compose with the
    # OAuth dance routes.
    from starlette.applications import Starlette
    from starlette.routing import Mount

    fastmcp_app = build_server().streamable_http_app()

    try:
        from .oauth import oauth_routes  # type: ignore
    except Exception:  # pragma: no cover — defensive for parallel build
        logger.warning(
            "mcp_external.oauth not importable yet — running without OAuth "
            "routes mounted. Bearer auth still works."
        )
        oauth_routes = []

    # FastMCP's streamable_http_app has its own lifespan that starts the
    # session manager's task group. Starlette does NOT recursively invoke
    # lifespans of mounted sub-apps, so we forward it explicitly on the
    # parent — otherwise every /mcp request 500s with
    # "Task group is not initialized. Make sure to use run()."
    parent = Starlette(
        routes=[*oauth_routes, Mount("/", app=fastmcp_app)],
        lifespan=lambda _app: fastmcp_app.router.lifespan_context(_app),
    )
    app = AuthMiddleware(parent)

    import uvicorn
    host, port = _bind_host(), _bind_port()
    logger.info(
        "hikari-external listening on %s:%s (Streamable HTTP MCP + OAuth)",
        host, port,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
