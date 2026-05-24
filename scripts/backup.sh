#!/bin/zsh
# Sprint 7F encrypted backup — tar + age pipeline.
# Packs: data/hikari.db (via sqlite3 .backup for WAL safety), .env,
# secrets/, and a keychain export. Encrypts with age. Retains last 14.
#
# Run manually:   ./scripts/backup.sh
# Run via launchd: see ./scripts/install_backup.sh (daily at 03:00).
#
# Failure modes are intentional silent-failures (logged via launchd stderr).
# Never raise — backup must not bring down the bot.

set -euo pipefail

umask 077

REPO_DIR="$(cd "$(dirname "${(%):-%x}")/.." && pwd)"
SRC="$REPO_DIR/data/hikari.db"
BACKUP_DIR="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki/projects/hikari-agent/backups"
RETAIN_DAYS=14

if [ ! -f "$SRC" ]; then
    echo "backup: source $SRC does not exist; skipping." >&2
    exit 0
fi

mkdir -p "$BACKUP_DIR"

BACKUP_TAR_AGE="$BACKUP_DIR/hikari-$(date +%Y%m%d).tar.age"

# Skip if today's backup already exists (idempotent for re-runs).
if [ -f "$BACKUP_TAR_AGE" ]; then
    echo "backup: $BACKUP_TAR_AGE already present; skipping."
    exit 0
fi

# Determine age recipient key
RECIPIENT_KEY="${HIKARI_BACKUP_AGE_RECIPIENT:-$HOME/.config/hikari/backup_age.pub}"
if [ ! -f "$RECIPIENT_KEY" ]; then
    echo "backup: missing age recipient at $RECIPIENT_KEY" >&2
    echo "backup: generate one with: bash $REPO_DIR/scripts/age_keygen.sh" >&2
    exit 1
fi

# Verify age binary is available
AGE_BIN="$(command -v age || true)"
if [ -z "$AGE_BIN" ]; then
    echo "backup: age not found in PATH — cannot encrypt backup." >&2
    echo "backup: install via: brew install age" >&2
    exit 1
fi

# --- Build a clean SQLite snapshot (WAL-safe) ---
SQLITE_BIN="$(command -v sqlite3 || true)"
if [ -z "$SQLITE_BIN" ]; then
    echo "backup: sqlite3 not found in PATH; cannot safely back up a WAL database." >&2
    exit 1
fi

TMP_DIR=$(mktemp -d -t hikari-bak.XXXXXX)
chmod 700 "$TMP_DIR"
DB_SNAP="$TMP_DIR/hikari.db"
"$SQLITE_BIN" "$SRC" ".backup '$DB_SNAP'"

# --- Assemble a tarball in a temp file ---
TMP_TAR=$(mktemp -t hikari-bak.XXXXXX.tar)
chmod 600 "$TMP_TAR"

cleanup() {
    [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ] && rm -rf "$TMP_DIR"
    [ -n "${TMP_TAR:-}" ] && [ -f "$TMP_TAR" ] && rm -f "$TMP_TAR"
}
trap cleanup EXIT INT TERM

# Start tar with the DB snapshot
tar --create --file "$TMP_TAR" -C "$TMP_DIR" hikari.db

# Append .env if present
if [ -f "$REPO_DIR/.env" ]; then
    tar --append --file "$TMP_TAR" -C "$REPO_DIR" .env 2>/dev/null || true
fi

# Append secrets/ dir if present
if [ -d "$REPO_DIR/secrets" ]; then
    tar --append --file "$TMP_TAR" -C "$REPO_DIR" secrets 2>/dev/null || true
fi

# Keychain export (best effort — may fail without user session)
KC_TMP="$TMP_DIR/keychain.p12"
security export -k login.keychain -t internet -f openssl -P "" -o "$KC_TMP" 2>/dev/null || true
if [ -f "$KC_TMP" ]; then
    tar --append --file "$TMP_TAR" -C "$TMP_DIR" keychain.p12 2>/dev/null || true
fi

# Append cloudflared config if present
if [ -d "$HOME/.cloudflared" ]; then
    tar --append --file "$TMP_TAR" -C "$HOME" .cloudflared 2>/dev/null || true
fi

# --- Encrypt with age ---
"$AGE_BIN" -R "$RECIPIENT_KEY" -o "$BACKUP_TAR_AGE" "$TMP_TAR"

# Cleanup temp files
rm -rf "$TMP_DIR"
rm -f "$TMP_TAR"

SIZE_KB=$(($(stat -f%z "$BACKUP_TAR_AGE") / 1024))
echo "backup: wrote $BACKUP_TAR_AGE (${SIZE_KB} KB)"

# Retention: 14 days — prune oldest .tar.age files
find "$BACKUP_DIR" -name 'hikari-*.tar.age' -mtime +$RETAIN_DAYS -print -delete 2>/dev/null | while read -r OLD; do
    echo "backup: pruned $OLD"
done

# Also prune any legacy plaintext .db backups beyond retention
ls -t "$BACKUP_DIR"/hikari-*.db 2>/dev/null | tail -n +$((RETAIN_DAYS + 1)) | while read -r OLD; do
    rm -- "$OLD"
    echo "backup: pruned legacy $OLD"
done
