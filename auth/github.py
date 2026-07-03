"""GitHub PAT provider with X-OAuth-Scopes detection.

PAT kinds:
  classic (ghp_...): GET /user returns X-OAuth-Scopes header with comma-separated scopes.
  fine-grained (github_pat_...): X-OAuth-Scopes is absent/empty; we mark scopes=['*']
    so scope precheck always passes (fine-grained PATs can't be introspected at read time).

Keychain item: 'hikari-github', key 'token' — JSON blob:
  {token, scopes: [...], kind: 'classic'|'fine-grained', login: <str>}

Runtime: agents/runtime.py injects the token from keychain into the GitHub MCP
subprocess env as GITHUB_PERSONAL_ACCESS_TOKEN before spawning.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from auth.providers import Provider
from auth.store import TokenStore, default_store

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com/user"
_TOKEN_KEY = "token"


def _load_pat() -> dict | None:
    raw = default_store().get("github", _TOKEN_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def paste_and_persist(pat: str) -> dict:
    """Validate a PAT against GitHub, detect scopes, and persist to keychain.

    Args:
        pat: The raw Personal Access Token string (from getpass or stdin).

    Returns:
        The persisted blob dict: {token, scopes, kind, login}.

    Raises:
        httpx.HTTPStatusError: when GitHub returns non-200.
        ValueError: when the token is empty.
    """
    if not pat or not pat.strip():
        raise ValueError("github PAT is empty")
    pat = pat.strip()

    resp = httpx.get(
        _GITHUB_API,
        headers={
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10.0,
    )
    resp.raise_for_status()

    login = resp.json().get("login", "")
    scopes_header = (resp.headers.get("X-OAuth-Scopes") or "").strip()

    if scopes_header:
        scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
        kind = "classic"
    else:
        scopes = ["*"]
        kind = "fine-grained"

    blob = {
        "token": pat,
        "scopes": scopes,
        "kind": kind,
        "login": login,
    }
    default_store().set("github", _TOKEN_KEY, json.dumps(blob))
    return blob


class GitHubPATProvider(Provider):
    """GitHub Personal Access Token provider.

    current_scopes():
      - classic PAT: returns the X-OAuth-Scopes detected at paste time (set[str]).
      - fine-grained PAT: returns {'*'} — scope precheck always passes.
      - no keychain entry: falls back to GITHUB_PERSONAL_ACCESS_TOKEN env var.

    refresh(): returns the stored PAT (PATs don't expire on their own).
    revoke(): deletes the keychain entry; returns True on success.
    """

    name = "github"

    def __init__(self, store: TokenStore | None = None) -> None:
        self._store = store or default_store()

    async def current_scopes(self) -> set[str]:
        blob = _load_pat()
        if blob:
            scopes = blob.get("scopes") or []
            return set(scopes)
        # Legacy env-var fallback.
        if os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN"):
            return {"_present"}
        return set()

    async def refresh(self) -> str:
        blob = _load_pat()
        if blob:
            return str(blob.get("token") or "")
        return os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or ""

    def revoke(self) -> bool:
        try:
            self._store.clear("github")
            return True
        except Exception as exc:
            logger.warning("GitHubPATProvider.revoke: %r", exc)
            return False
