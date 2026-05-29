"""Shared public-base-URL resolver for the external MCP package.

Resolution order (same in server.py, oauth.py, launch.py):
  1. ``mcp_external.public_base_url_env`` config key — the *name* of an env var
     holding the URL (indirection lets operators set PUBLIC_BASE_URL without
     hard-coding the value in config/engagement.yaml).
  2. ``mcp_external.public_base_url`` — legacy direct value (backward compat).
  3. Caller-supplied fallback (derived from the inbound request or ASGI scope).
"""

from __future__ import annotations

import os

from agents import config as cfg


def resolve_public_base_url(fallback: str = "") -> str:
    """Return the externally-visible base URL. Trailing slash stripped.

    ``fallback`` is used when neither the env-var indirection nor the legacy
    config key resolves to a non-empty string.  Callers derive it from the
    inbound request (oauth.py / launch.py) or leave it empty (server.py,
    where it's only used for allowed_hosts construction).
    """
    env_key = cfg.get("mcp_external.public_base_url_env")
    if env_key:
        val = os.environ.get(str(env_key))
        if val:
            return val.rstrip("/")
    configured = cfg.get("mcp_external.public_base_url")
    if configured:
        return str(configured).rstrip("/")
    return fallback.rstrip("/") if fallback else ""
