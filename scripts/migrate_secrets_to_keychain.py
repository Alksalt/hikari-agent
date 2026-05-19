#!/usr/bin/env python3
"""Migrate hikari-agent secrets from `.env` into the macOS Keychain.

Daemon-critical keys are stored under service `hikari` with key name as the
generic-password label. Read them later via:

    security find-generic-password -a hikari -s TELEGRAM_BOT_TOKEN -w

After running this:
  1. Verify each key can be read back (the script does this at the end).
  2. Move `.env` to `.env.predeprecated` (manual; the script doesn't delete it).
  3. Update `agents/telegram_bridge.main()` if you want it to read from Keychain
     directly instead of `.env` (or use a `.env.keychain` shim — see end of file).

Usage:
    uv run python scripts/migrate_secrets_to_keychain.py [--dry-run]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ENV_FILE = REPO_ROOT / ".env"

# Daemon-critical: must survive reboots, no human in the loop.
KEYCHAIN_KEYS = {
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENROUTER_API_KEY",
    "OWNER_TELEGRAM_ID",
    # Phase 4 (only if present)
    "TAVILY_API_KEY",
}

# File-shaped or interactive: leave in 1Password / .env for now.
SKIP_KEYS = {
    "GOOGLE_SERVICE_ACCOUNT_JSON",  # file path or JSON blob — use op run
    "NOTION_TOKEN",                  # interactive setup — use op run
}


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def keychain_set(label: str, value: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  [dry-run] would set {label} ({len(value)} chars)")
        return True
    # -U updates if exists; -s = service, -a = account, -w = password value
    result = subprocess.run(
        ["security", "add-generic-password", "-U",
         "-a", "hikari", "-s", label, "-w", value],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  FAIL {label}: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def keychain_get(label: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "hikari", "-s", label, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be migrated without changing keychain.")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE,
                        help=f"Path to .env (default: {ENV_FILE})")
    args = parser.parse_args()

    env = read_env(args.env_file)
    if not env:
        print(f"no env vars read from {args.env_file}", file=sys.stderr)
        return 1

    print(f"=== migrating from {args.env_file} to macOS Keychain ===\n")
    migrated = 0
    skipped = 0
    failed = 0
    for k, v in env.items():
        if k in SKIP_KEYS:
            print(f"  skip {k} (use 1Password op run instead)")
            skipped += 1
            continue
        if k not in KEYCHAIN_KEYS:
            print(f"  skip {k} (not on keychain allowlist)")
            skipped += 1
            continue
        ok = keychain_set(k, v, dry_run=args.dry_run)
        if ok:
            migrated += 1
            if not args.dry_run:
                # Verify round-trip
                round_trip = keychain_get(k)
                if round_trip != v:
                    print(f"  WARN: roundtrip mismatch for {k}", file=sys.stderr)
                    failed += 1
                else:
                    print(f"  ok   {k}")
        else:
            failed += 1

    print(f"\n=== {migrated} migrated, {skipped} skipped, {failed} failed ===")
    if not args.dry_run and migrated > 0:
        print("\nnext steps:")
        print("  1. confirm it still works: `uv run hikari-agent` (still reads .env for now)")
        print("  2. when ready, replace .env with .env.keychain that does:")
        print("     export TELEGRAM_BOT_TOKEN=$(security find-generic-password "
              "-a hikari -s TELEGRAM_BOT_TOKEN -w)")
        print("     (etc for each migrated key — see this script's docstring)")
        print("  3. then `mv .env .env.predeprecated && source .env.keychain` in launchd setup")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
