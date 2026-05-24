#!/usr/bin/env bash
# Sprint 7F: bare-metal restore procedure for Hikari from an encrypted backup.
#
# Usage:
#   bash scripts/restore.sh <hikari-YYYYMMDD.tar.age>
#   bash scripts/restore.sh --dry-run <hikari-YYYYMMDD.tar.age>
#
# DRY_RUN=1 bash scripts/restore.sh <archive>  — print steps without executing
#
# The script decrypts the archive, extracts it, then guides you through the
# manual steps needed to complete the restore.
#
# TMP_ROOT is NOT deleted automatically so the operator can copy files out.
# Print its path at exit. Remove manually when done.

set -euo pipefail

umask 077

# Parse --dry-run flag and optional archive positional arg.
DRY_RUN="${DRY_RUN:-0}"
ARCHIVE=""
for _arg in "$@"; do
    case "$_arg" in
        --dry-run) DRY_RUN=1 ;;
        *) ARCHIVE="$_arg" ;;
    esac
done

if [ -z "$ARCHIVE" ] && [ "$DRY_RUN" != "1" ]; then
    echo "usage: $0 [--dry-run] <hikari-YYYYMMDD.tar.age>" >&2
    exit 1
fi
# In dry-run without an archive, use a placeholder so the script can show steps.
ARCHIVE="${ARCHIVE:-/path/to/hikari-YYYYMMDD.tar.age}"

TMP_ROOT=$(mktemp -d -t hikari-restore.XXXXXX)
chmod 700 "$TMP_ROOT"
TAR_PATH="$TMP_ROOT/hikari-restore.tar"
EXTRACT_DIR="$TMP_ROOT/extracted"

cleanup() {
    # Intentionally does NOT delete TMP_ROOT so the operator can copy files.
    # The path is printed at the end of the script. Remove manually when done.
    :
}
trap cleanup EXIT INT TERM

KEY_FILE="${HIKARI_BACKUP_AGE_KEY:-$HOME/.config/hikari/backup_age.key}"

if ! command -v age >/dev/null 2>&1 && [ "$DRY_RUN" != "1" ]; then
    echo "error: age not found in PATH — install via: brew install age" >&2
    exit 1
fi

if [ ! -f "$ARCHIVE" ] && [ "$DRY_RUN" != "1" ]; then
    echo "error: archive not found: $ARCHIVE" >&2
    exit 1
fi

if [ ! -f "$KEY_FILE" ] && [ "$DRY_RUN" != "1" ]; then
    echo "error: private key not found at $KEY_FILE" >&2
    echo "       set HIKARI_BACKUP_AGE_KEY to the path of your backup_age.key" >&2
    exit 1
fi

step() { echo ""; echo "STEP: $*"; }
run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "  (dry-run) $*"
    else
        "$@"
    fi
}

step "1. decrypt $ARCHIVE → $TAR_PATH (via age -i $KEY_FILE)"
run age -d -i "$KEY_FILE" -o "$TAR_PATH" "$ARCHIVE"

step "2. extract tarball into $EXTRACT_DIR/"
run mkdir -p "$EXTRACT_DIR"
run tar -xf "$TAR_PATH" -C "$EXTRACT_DIR"

step "3. operator action required: review $EXTRACT_DIR/, copy desired files into place"
echo "  - hikari.db  → ~/agents/hikari-agent/data/hikari.db"
echo "  - .env       → ~/agents/hikari-agent/.env"
echo "  - secrets/   → ~/agents/hikari-agent/secrets/"
echo "  - keychain.p12 → security import $EXTRACT_DIR/keychain.p12 -k login.keychain"
echo "  - .cloudflared/ → ~/.cloudflared/"

step "4. re-grant OAuth (Google, Notion, GitHub):"
echo "  uv run python scripts/auth.py google grant"
echo "  uv run python scripts/auth.py notion grant"
echo "  uv run python scripts/auth.py github grant"

step "5. re-install launchd jobs:"
echo "  bash scripts/install_launchd.sh"
echo "  bash scripts/install_backup.sh"
echo "  bash scripts/install_deadman.sh"
echo "  bash scripts/install_external_mcp_launchd.sh"
echo "  bash scripts/install_cloudflared_launchd.sh"

step "DONE — restored to $EXTRACT_DIR/. Manual copy required (see step 3)."
echo ""
echo "EXTRACT DIR: $EXTRACT_DIR"
echo "TMP ROOT:    $TMP_ROOT"
echo "(not auto-deleted — run: rm -rf $TMP_ROOT  when done)"
