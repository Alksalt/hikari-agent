#!/usr/bin/env bash
# Sprint 7F: bare-metal restore procedure for Hikari from an encrypted backup.
#
# Usage:
#   bash scripts/restore.sh <hikari-YYYYMMDD.tar.age>
#
# DRY_RUN=1 bash scripts/restore.sh <archive>  — print steps without executing
#
# The script decrypts the archive, extracts it, then guides you through the
# manual steps needed to complete the restore.

set -euo pipefail

umask 077
TMP_ROOT=$(mktemp -d -t hikari-restore.XXXXXX)
chmod 700 "$TMP_ROOT"
TAR_PATH="$TMP_ROOT/hikari-restore.tar"
EXTRACT_DIR="$TMP_ROOT/extracted"

cleanup() {
    if [ "$DRY_RUN" != "1" ] && [ -n "${TMP_ROOT:-}" ] && [ -d "$TMP_ROOT" ]; then
        find "$TMP_ROOT" -type f -exec /bin/dd if=/dev/zero of={} bs=4k count=1 conv=notrunc 2>/dev/null \; || true
        rm -rf "$TMP_ROOT"
    fi
}
trap cleanup EXIT INT TERM

ARCHIVE="${1:?usage: $0 <hikari-YYYYMMDD.tar.age>}"
KEY_FILE="${HIKARI_BACKUP_AGE_KEY:-$HOME/.config/hikari/backup_age.key}"
DRY_RUN="${DRY_RUN:-0}"

if ! command -v age >/dev/null 2>&1; then
    echo "error: age not found in PATH — install via: brew install age" >&2
    exit 1
fi

if [ ! -f "$ARCHIVE" ]; then
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
echo "WARNING: $TMP_ROOT will be wiped on shell exit — copy needed files NOW."
