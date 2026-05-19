"""Entrypoint for the external MCP server.

Usage::

    uv run python -m mcp_external.launch

Wraps the FastMCP Streamable HTTP app with a bearer-token Starlette
middleware so only requests carrying the right ``Authorization: Bearer ...``
header reach the MCP layer. Binds to the configured host/port.

The intended deployment is BEHIND Cloudflare Tunnel — the tunnel terminates
TLS at the edge and forwards to ``127.0.0.1:<bind_port>`` here. See
``scripts/install_cloudflared.md`` for the tunnel setup.
"""

from __future__ import annotations

import logging
import sys

from agents import config as cfg
from agents.log_scrub import install_root_filter

from .server import build_server, check_bearer_token

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(cfg.get("mcp_external.enabled", False))


def _bind_host() -> str:
    return str(cfg.get("mcp_external.bind_host", "127.0.0.1"))


def _bind_port() -> int:
    return int(cfg.get("mcp_external.bind_port", 8765))


class BearerAuthMiddleware:
    """ASGI middleware enforcing ``Authorization: Bearer <HIKARI_MCP_SECRET>``.

    Lightweight — no Starlette dep needed because FastMCP already brings
    one and we operate at the ASGI protocol level.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Non-HTTP (lifespan, websocket) traffic passes through.
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1", errors="ignore")
        if not check_bearer_token(auth):
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({
                "type": "http.response.body",
                "body": b"401 unauthorized\n",
            })
            return
        await self.app(scope, receive, send)


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    install_root_filter()

    if not _enabled():
        logger.error(
            "mcp_external is disabled (config: mcp_external.enabled). "
            "Flip the flag and set HIKARI_MCP_SECRET first."
        )
        return 2

    secret_env = str(cfg.get("mcp_external.secret_env", "HIKARI_MCP_SECRET"))
    import os
    if not os.environ.get(secret_env):
        logger.error(
            "%s is not set — refusing to start an unauthenticated server.",
            secret_env,
        )
        return 3

    server = build_server()
    app = server.streamable_http_app()
    app = BearerAuthMiddleware(app)

    import uvicorn
    host, port = _bind_host(), _bind_port()
    logger.info("hikari-external listening on %s:%s (Streamable HTTP MCP)",
                host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
