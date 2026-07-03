"""Unified auth CLI for hikari-agent.

Usage:
    uv run python -m scripts.auth google grant [--add <scope>[,<scope>...]]
    uv run python -m scripts.auth google status
    uv run python -m scripts.auth google revoke

    uv run python -m scripts.auth notion grant
    uv run python -m scripts.auth notion status
    uv run python -m scripts.auth notion revoke

    uv run python -m scripts.auth github paste
    uv run python -m scripts.auth github status
    uv run python -m scripts.auth github revoke
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(msg: str) -> None:
    print(msg)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Google sub-commands
# ---------------------------------------------------------------------------

def _google_grant(extra_scopes: list[str]) -> int:
    """Run the Google InstalledAppFlow and write tokens to keychain."""
    CLIENT_FILE = Path(__file__).parent.parent / "secrets" / "google_oauth_client.json"
    if not CLIENT_FILE.exists():
        _err(f"missing {CLIENT_FILE}")
        _err("  -> download from Cloud Console -> Credentials -> OAuth client ID (Desktop)")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        _err("google-auth-oauthlib not installed.")
        _err("  -> uv add --dev google-auth-oauthlib")
        return 1

    # https://mail.google.com/ is the broadest Gmail scope — it covers
    # gmail.modify, gmail.readonly, gmail.send, etc. (see auth/scope_match.py).
    # gmail.modify is therefore redundant here and has been removed to avoid
    # requesting duplicate permissions.  If you do NOT need full Gmail access,
    # remove https://mail.google.com/ from --add and use the narrower
    # gmail.readonly / gmail.send scopes instead.
    BASE_SCOPES = [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/presentations",
    ]

    scopes = list(BASE_SCOPES)
    if extra_scopes:
        for s in extra_scopes:
            if s not in scopes:
                scopes.append(s)

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), scopes)
    flow.redirect_uri = "http://127.0.0.1:8910/"
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print()
    print("=" * 70)
    print("OPEN THIS URL in your browser:")
    print()
    print(auth_url)
    print()
    print("Click Allow. Redirect goes to 127.0.0.1:8910 — listening now.")
    print("=" * 70)
    print(flush=True)

    creds = flow.run_local_server(
        host="127.0.0.1",
        bind_addr="127.0.0.1",
        port=8910,
        access_type="offline",
        prompt="consent",
        open_browser=False,
    )

    from datetime import UTC, datetime
    now = datetime.now(UTC)
    expires_at = (
        creds.expiry.replace(tzinfo=UTC).isoformat()
        if creds.expiry
        else now.isoformat()
    )

    payload = {
        "client_id": flow.client_config["client_id"],
        "client_secret": flow.client_config["client_secret"],
        "access_token": creds.token or "",
        "refresh_token": creds.refresh_token or "",
        "scope": " ".join(scopes),
        "expires_at": expires_at,
        # Timestamp of this grant, distinct from the access-token expiry.
        "granted_at": now.isoformat(),
    }

    from auth.google import write_grant_to_keychain
    write_grant_to_keychain(payload)

    _ok(
        "google grant: keychain item 'hikari-google' updated."
        " you can delete the GOOGLE_* lines from .env."
    )
    return 0


def _google_status() -> int:
    from auth.google import read_grant_from_keychain
    grant = read_grant_from_keychain()
    if not grant:
        _err("no google grant in keychain. run: uv run python -m scripts.auth google grant")
        return 1
    print(json.dumps({
        # granted_at records when the OAuth flow completed (not the token expiry).
        "granted_at": grant.get("granted_at", "unknown"),
        "expires_at": grant.get("expires_at", "unknown"),
        "refresh_token_present": bool(grant.get("refresh_token")),
        # Note: 'scopes' reflects what was requested at grant time; actual
        # live token scopes may differ — run `google status` after a new grant
        # or call current_scopes() for a live tokeninfo probe.
        "scopes_requested_at_grant": grant.get("scope", ""),
    }, indent=2))
    return 0


def _google_revoke() -> int:
    from auth.google import GoogleProvider
    from auth.store import default_store
    store = default_store()
    provider = GoogleProvider(store)
    if provider.revoke():
        _ok("google revoke: keychain item 'hikari-google' deleted.")
        return 0
    _err("google revoke: keychain item 'hikari-google' delete failed.")
    return 1


# ---------------------------------------------------------------------------
# Notion sub-commands
# ---------------------------------------------------------------------------

def _notion_grant() -> int:
    try:
        from auth.notion import run_pkce_flow
    except ImportError as e:
        _err(f"auth.notion import failed: {e}")
        return 1
    try:
        run_pkce_flow()
        _ok("notion grant: tokens persisted to keychain.")
        return 0
    except Exception as e:
        _err(f"notion grant failed: {e}")
        return 1


def _notion_status() -> int:
    from auth.notion import _load_client, _load_token
    client = _load_client()
    token = _load_token()
    if not token:
        _err("no notion token in keychain. run: uv run python -m scripts.auth notion grant")
        return 1
    print(json.dumps({
        "workspace_id": token.get("workspace_id", ""),
        "workspace_name": token.get("workspace_name", ""),
        "scopes": token.get("scopes", ""),
        "last_refreshed_at": token.get("refreshed_at", "unknown"),
        "client_id": (client or {}).get("client_id", ""),
    }, indent=2))
    return 0


def _notion_revoke() -> int:
    from auth.notion import NotionOAuthProvider
    provider = NotionOAuthProvider()
    if provider.revoke():
        _ok("notion revoke: keychain items 'hikari-notion' and 'hikari-notion-client' deleted.")
        return 0
    _err("notion revoke: keychain items 'hikari-notion' / 'hikari-notion-client' delete failed.")
    return 1


# ---------------------------------------------------------------------------
# GitHub sub-commands
# ---------------------------------------------------------------------------

def _github_paste() -> int:
    pat = getpass.getpass("GitHub PAT (input hidden): ")
    try:
        from auth.github import paste_and_persist
        blob = paste_and_persist(pat)
    except Exception as e:
        _err(f"github paste failed: {e}")
        return 1
    _ok(
        f"github token persisted. "
        f"kind={blob['kind']} login={blob['login']} scopes={blob['scopes']}"
    )
    return 0


def _github_status() -> int:
    from auth.github import _load_pat
    blob = _load_pat()
    if not blob:
        _err("no github token in keychain. run: uv run python -m scripts.auth github paste")
        return 1
    print(json.dumps({
        "kind": blob.get("kind", ""),
        "login": blob.get("login", ""),
        "scopes": blob.get("scopes", []),
        "token_prefix": str(blob.get("token", ""))[:10] + "...",
    }, indent=2))
    return 0


def _github_revoke() -> int:
    from auth.github import GitHubPATProvider
    provider = GitHubPATProvider()
    if provider.revoke():
        _ok("github revoke: keychain item 'hikari-github' deleted.")
        return 0
    _err("github revoke: keychain item 'hikari-github' delete failed.")
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.auth",
        description="Manage hikari-agent OAuth tokens in macOS Keychain.",
    )
    sub = parser.add_subparsers(dest="provider")
    sub.required = True

    # google
    g = sub.add_parser("google")
    gsub = g.add_subparsers(dest="cmd")
    gsub.required = True
    ggrant = gsub.add_parser("grant")
    ggrant.add_argument("--add", dest="extra_scopes", default="",
                        help="comma-separated extra scopes to add to the grant")
    gsub.add_parser("status")
    gsub.add_parser("revoke")

    # notion
    n = sub.add_parser("notion")
    nsub = n.add_subparsers(dest="cmd")
    nsub.required = True
    nsub.add_parser("grant")
    nsub.add_parser("status")
    nsub.add_parser("revoke")

    # github
    gh = sub.add_parser("github")
    ghsub = gh.add_subparsers(dest="cmd")
    ghsub.required = True
    ghsub.add_parser("paste")
    ghsub.add_parser("status")
    ghsub.add_parser("revoke")

    args = parser.parse_args(argv)

    if args.provider == "google":
        if args.cmd == "grant":
            extras = [s.strip() for s in (args.extra_scopes or "").split(",") if s.strip()]
            return _google_grant(extras)
        elif args.cmd == "status":
            return _google_status()
        elif args.cmd == "revoke":
            return _google_revoke()

    elif args.provider == "notion":
        if args.cmd == "grant":
            return _notion_grant()
        elif args.cmd == "status":
            return _notion_status()
        elif args.cmd == "revoke":
            return _notion_revoke()

    elif args.provider == "github":
        if args.cmd == "paste":
            return _github_paste()
        elif args.cmd == "status":
            return _github_status()
        elif args.cmd == "revoke":
            return _github_revoke()

    return 0


if __name__ == "__main__":
    sys.exit(main())
