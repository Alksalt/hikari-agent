"""One-time OAuth setup for google-workspace-mcp.

Walks the InstalledAppFlow: opens your browser, you click Allow, the script
catches the redirect, exchanges the code for a refresh token, and prints the
three values you paste into .env.

Usage:
  1. In Google Cloud Console: APIs & Services -> Credentials -> Create
     Credentials -> OAuth client ID -> Application type "Desktop app" ->
     download the JSON file (it contains client_id + client_secret).
  2. Save that file as secrets/google_oauth_client.json (gitignored).
  3. Make sure these APIs are enabled in the same Cloud project:
       Gmail API, Calendar API, Drive API, Docs API, Sheets API
  4. OAuth consent screen: External, **Published / In production** (click
     the "PUBLISH APP" button on the OAuth consent screen page). Leaving
     it in Testing mode causes the refresh token to expire after 7 days
     for sensitive scopes (gmail, drive). Production-without-verification
     is fine for a single-user personal app — you'll just see an
     "Unverified app" warning once on the consent screen; click
     "Advanced -> Go to <app> (unsafe)" to proceed.
  5. Run:
       uv run python scripts/setup_google_oauth.py
"""
from __future__ import annotations

import sys
from pathlib import Path

CLIENT_FILE = Path(__file__).parent.parent / "secrets" / "google_oauth_client.json"

# Broad scope set — pick what google-workspace-mcp tools you actually want
# Hikari to be able to call. The full `mail.google.com/` scope is required
# for bulk_delete + other destructive ops the morning-pile workflow needs;
# `gmail.modify` alone won't cover it. After editing this list, the user must
# re-run this script so the new refresh token carries the broader grant.
SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
]


def main() -> int:
    if not CLIENT_FILE.exists():
        print(f"missing {CLIENT_FILE}", file=sys.stderr)
        print("  -> download the OAuth Client ID JSON from Cloud Console,",
              file=sys.stderr)
        print("     save it as secrets/google_oauth_client.json", file=sys.stderr)
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("google-auth-oauthlib not installed.", file=sys.stderr)
        print("  -> uv add --dev google-auth-oauthlib", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    # Pre-print the consent URL so the user can paste it into ANY browser
    # (works when the script runs on a headless host or a different machine
    # from where you want to grant consent). Then start the local server
    # without auto-opening — user controls which browser handles it.
    flow.redirect_uri = "http://127.0.0.1:8910/"
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent",
    )
    print()
    print("=" * 70)
    print("OPEN THIS URL in any browser on any device:")
    print()
    print(auth_url)
    print()
    print("Then click Allow. The browser will redirect to 127.0.0.1:8910 —")
    print("the script is listening there now. If you authorize from a different")
    print("machine, the redirect MUST resolve to THIS host. Easiest: paste")
    print("the consent URL into a browser on this same Mac.")
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

    print()
    print("=" * 60)
    print("paste these into .env:")
    print("=" * 60)
    print(f"GOOGLE_WORKSPACE_CLIENT_ID={flow.client_config['client_id']}")
    print(f"GOOGLE_WORKSPACE_CLIENT_SECRET={flow.client_config['client_secret']}")
    print(f"GOOGLE_WORKSPACE_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)
    print()
    print("(if the OAuth consent screen is Published, this refresh token is")
    print(" permanent — no re-run needed unless you revoke access, change")
    print(" your Google password, or don't use it for 6 months. if it's")
    print(" still in Testing mode, expect a 7-day expiry — publish the app.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
