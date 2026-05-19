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
  4. OAuth consent screen: External, Testing, add your own email under
     "Test users". Sensitive scopes for unverified apps mean the refresh
     token will expire after 7 days unless the app is published.
  5. Run:
       uv run python scripts/setup_google_oauth.py
"""
from __future__ import annotations

import sys
from pathlib import Path

CLIENT_FILE = Path(__file__).parent.parent / "secrets" / "google_oauth_client.json"

# Broad scope set — pick what google-workspace-mcp tools you actually want
# Hikari to be able to call. Gmail.modify covers read + draft + send.
SCOPES = [
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
    # access_type=offline + prompt=consent ensures we get a refresh_token
    # even on re-auth (otherwise the second time around you only get an
    # access token).
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
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
    print("(refresh token expires in 7 days while the app is in Testing mode.")
    print(" re-run this script to get a fresh one.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
