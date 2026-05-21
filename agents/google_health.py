"""Bug 1 fix (live 2026-05-21): cheap startup probe for the Google Workspace
OAuth refresh token.

The upstream ``google-workspace-mcp`` package never rotates the refresh token
and silently 401s when it expires. The OAuth app is currently in Testing mode
in Google Cloud Console, where Google force-expires refresh tokens after 7
days. Without a probe, the first failure surfaces as a user-visible 401
mid-conversation (or worse, as an SDK error string shipped as a chat reply).

This module exchanges the stored refresh token for an access token directly
against Google's OAuth endpoint — no LLM call, no MCP subprocess. The
``calendar_heartbeat_healthy`` runtime_state row is already consumed by
``agents.scheduler._calendar_creds_healthy``; we just have to populate it.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Env var names match the upstream `google-workspace-mcp` package contract.
_REQUIRED_ENV_VARS = (
    "GOOGLE_WORKSPACE_CLIENT_ID",
    "GOOGLE_WORKSPACE_CLIENT_SECRET",
    "GOOGLE_WORKSPACE_REFRESH_TOKEN",
)


async def probe_google_token(timeout_sec: float = 10.0) -> tuple[bool, str]:
    """Exchange the stored refresh token for an access token.

    Returns ``(healthy, reason)``. ``reason`` is the empty string on success
    and a short tag on failure (``missing_env:…`` / ``invalid_grant`` /
    ``network:…`` / ``http_<code>``). On ``invalid_grant`` the refresh token
    has been revoked or hit Google's Testing-mode 7-day expiry — the user
    must re-run ``scripts/setup_google_oauth.py`` and restart the bot.
    """
    missing = [k for k in _REQUIRED_ENV_VARS if not os.environ.get(k, "").strip()]
    if missing:
        return False, f"missing_env:{','.join(missing)}"

    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": os.environ["GOOGLE_WORKSPACE_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_WORKSPACE_CLIENT_SECRET"],
                    "refresh_token": os.environ["GOOGLE_WORKSPACE_REFRESH_TOKEN"],
                    "grant_type": "refresh_token",
                },
            )
    except httpx.HTTPError as e:
        return False, f"network:{type(e).__name__}"
    except Exception as e:  # noqa: BLE001
        # Defensive — don't let an unexpected exception in a startup probe
        # take down post_init. The fallback "env vars present → assume healthy"
        # path in the scheduler still applies if we mark unhealthy on a fluke.
        logger.exception("google_health: unexpected error during token probe")
        return False, f"unexpected:{type(e).__name__}"

    if resp.status_code == 200:
        return True, ""

    try:
        body = resp.json()
        err = str(body.get("error") or body.get("error_description") or "unknown")
    except Exception:  # noqa: BLE001
        err = f"http_{resp.status_code}"
    return False, err
