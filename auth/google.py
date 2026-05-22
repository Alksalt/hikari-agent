"""Google OAuth provider.

current_scopes() implementation:
  1. Check runtime_state cache (auth.google.scopes + auth.google.scopes_checked_at).
     If fresh (< 24h), return cached set.
  2. POST to oauth2.googleapis.com/token (refresh flow) to get an access_token.
  3. GET /oauth2/v1/tokeninfo?access_token=<tok> to read the granted scopes.
  4. Cache the result; return the scope set.

Credentials are read from TokenStore first; env vars (GOOGLE_WORKSPACE_*) are
the fallback. On first successful env-var read, write-through to store.

Network-failure policy: return empty set, do NOT write cache, so next call retries.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

from auth.providers import Provider
from auth.store import TokenStore

logger = logging.getLogger(__name__)

_SCOPES_CACHE_KEY = "auth.google.scopes"
_SCOPES_CHECKED_AT_KEY = "auth.google.scopes_checked_at"
_SCOPES_CACHE_TTL_HOURS = 24

_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_TOKENINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v1/tokeninfo"


class GoogleProvider(Provider):
    """Google OAuth2 provider backed by a refresh-token flow."""

    name = "google"

    def __init__(self, store: TokenStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Credential loading
    # ------------------------------------------------------------------

    def _creds(self) -> tuple[str, str, str] | None:
        """Return (client_id, client_secret, refresh_token) or None.

        Checks store first, then env vars. On first successful env-var read,
        writes through to store.
        """
        # Try store
        client_id = self._store.get("google", "client_id")
        client_secret = self._store.get("google", "client_secret")
        refresh_token = self._store.get("google", "refresh_token")
        if client_id and client_secret and refresh_token:
            return client_id, client_secret, refresh_token

        # Fall back to env
        client_id = os.environ.get("GOOGLE_WORKSPACE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_WORKSPACE_CLIENT_SECRET")
        refresh_token = os.environ.get("GOOGLE_WORKSPACE_REFRESH_TOKEN")
        if not (client_id and client_secret and refresh_token):
            return None

        # Write-through to store (best-effort)
        try:
            self._store.set("google", "client_id", client_id)
            self._store.set("google", "client_secret", client_secret)
            self._store.set("google", "refresh_token", refresh_token)
        except Exception as exc:
            logger.debug("GoogleProvider: store write-through failed: %r", exc)

        return client_id, client_secret, refresh_token

    # ------------------------------------------------------------------
    # current_scopes
    # ------------------------------------------------------------------

    async def current_scopes(self) -> set[str]:
        """Return the set of scopes granted to the current refresh token.

        Uses a 24-hour runtime_state cache. On network failure returns
        an empty set without updating the cache.
        """
        from storage import db

        # Cache check
        cached_raw = db.runtime_get(_SCOPES_CACHE_KEY)
        checked_raw = db.runtime_get(_SCOPES_CHECKED_AT_KEY)
        if cached_raw and checked_raw:
            try:
                checked = datetime.fromisoformat(checked_raw)
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=UTC)
                if datetime.now(UTC) - checked < timedelta(hours=_SCOPES_CACHE_TTL_HOURS):
                    return set(cached_raw.split()) if cached_raw.strip() else set()
            except (ValueError, TypeError):
                pass  # corrupt cache — re-probe

        # Fetch fresh
        creds = self._creds()
        if not creds:
            logger.info(
                "GoogleProvider.current_scopes: missing credentials; returning empty set"
            )
            return set()

        client_id, client_secret, refresh_token = creds
        try:
            async with httpx.AsyncClient(timeout=10.0) as cli:
                tok_resp = await cli.post(
                    _TOKEN_ENDPOINT,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                tok_resp.raise_for_status()
                access_token = tok_resp.json().get("access_token") or ""
                if not access_token:
                    logger.warning(
                        "GoogleProvider.current_scopes: token endpoint returned no access_token"
                    )
                    return set()

                info_resp = await cli.get(
                    _TOKENINFO_ENDPOINT,
                    params={"access_token": access_token},
                )
                info_resp.raise_for_status()
                scopes_str = str(info_resp.json().get("scope") or "")
        except Exception:
            logger.exception(
                "GoogleProvider.current_scopes: probe failed; returning empty set (no cache)"
            )
            return set()

        scopes = set(scopes_str.split())
        # Cache
        db.runtime_set(_SCOPES_CACHE_KEY, scopes_str)
        db.runtime_set(_SCOPES_CHECKED_AT_KEY, datetime.now(UTC).isoformat())
        logger.debug("GoogleProvider.current_scopes: cached %d scopes", len(scopes))
        return scopes

    # ------------------------------------------------------------------
    # refresh / revoke
    # ------------------------------------------------------------------

    async def refresh(self) -> str:
        """Refresh the token; return the new access_token (or empty string)."""
        creds = self._creds()
        if not creds:
            return ""
        client_id, client_secret, refresh_token = creds
        try:
            async with httpx.AsyncClient(timeout=10.0) as cli:
                resp = await cli.post(
                    _TOKEN_ENDPOINT,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                resp.raise_for_status()
                return str(resp.json().get("access_token") or "")
        except Exception:
            logger.exception("GoogleProvider.refresh: failed")
            return ""

    def revoke(self) -> None:
        """Clear stored Google credentials from the store."""
        try:
            self._store.clear("google")
        except Exception as exc:
            logger.debug("GoogleProvider.revoke: %r", exc)
