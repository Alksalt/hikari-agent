#!/bin/zsh
# Sprint 7F encrypted backup — tar + age pipeline.
# Packs: data/hikari.db (via sqlite3 .backup for WAL safety), .env,
# secrets/, and a keychain export. Encrypts with age. Retains last 14.
#
# Run manually:   ./scripts/backup.sh
# Run via launchd: see ./scripts/install_backup.sh (daily at 03:00).
# Self-test:      ./scripts/backup.sh --dry-run --self-test
#
# Failure modes are intentional silent-failures (logged via launchd stderr).
# Never raise — backup must not bring down the bot.

set -euo pipefail

umask 077

# Parse flags
DRY_RUN=0
SELF_TEST=0
for _arg in "$@"; do
    case "$_arg" in
        --dry-run)   DRY_RUN=1 ;;
        --self-test) SELF_TEST=1 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")/.." && pwd)"
SRC="$REPO_DIR/data/hikari.db"
BACKUP_DIR="${HIKARI_BACKUP_DIR:-$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki/projects/hikari-agent/backups}"
RETAIN_DAYS=14

if [ "$DRY_RUN" = "1" ]; then
    echo "backup: dry-run mode — no files will be written."
    if [ "$SELF_TEST" = "1" ]; then
        # Verify that required tools exist and backup dir is reachable.
        _st_ok=1
        echo "backup: self-test: checking age binary..."
        command -v age >/dev/null 2>&1 && echo "backup: self-test: age OK" || { echo "backup: self-test: age MISSING (install via: brew install age)" >&2; _st_ok=0; }
        echo "backup: self-test: checking sqlite3 binary..."
        command -v sqlite3 >/dev/null 2>&1 && echo "backup: self-test: sqlite3 OK" || { echo "backup: self-test: sqlite3 MISSING" >&2; _st_ok=0; }
        if [ "$_st_ok" = "1" ]; then
            echo "backup: self-test: PASS"
        else
            echo "backup: self-test: WARN — missing binaries (see above)" >&2
        fi
    fi
    exit 0
fi

if [ ! -f "$SRC" ]; then
    echo "backup: source $SRC does not exist; skipping." >&2
    exit 0
fi

mkdir -p "$BACKUP_DIR"

BACKUP_TAR_AGE="$BACKUP_DIR/hikari-$(date +%Y%m%d).tar.age"
BACKUP_TMP="$BACKUP_TAR_AGE.tmp"

# Skip if today's backup already exists and was written recently (≤23 h).
if [ -f "$BACKUP_TAR_AGE" ]; then
    AGE_SEC=$(( $(date +%s) - $(stat -f%m "$BACKUP_TAR_AGE") ))
    if [ "$AGE_SEC" -lt 82800 ]; then
        echo "backup: $BACKUP_TAR_AGE already present and fresh (${AGE_SEC}s); skipping."
        exit 0
    fi
    echo "backup: $BACKUP_TAR_AGE exists but is stale (${AGE_SEC}s) — re-running."
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
    # Remove the in-progress .tmp archive if a kill interrupted the encrypt step.
    [ -n "${BACKUP_TMP:-}" ] && [ -f "$BACKUP_TMP" ] && rm -f "$BACKUP_TMP"
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

# --- Encrypt with age into a .tmp file, verify, then atomically rename ---
"$AGE_BIN" -R "$RECIPIENT_KEY" -o "$BACKUP_TMP" "$TMP_TAR"

# Smoke-test: decrypt the .tmp back into a bare tar, untar the DB snapshot,
# run PRAGMA quick_check. Any failure aborts — the corrupt .tmp is removed.
VERIFY_DIR=$(mktemp -d -t hikari-bak-verify.XXXXXX)
VERIFY_TAR="$VERIFY_DIR/check.tar"
VERIFY_DB="$VERIFY_DIR/hikari.db"
verify_ok=0
if "$AGE_BIN" -d -i "${HIKARI_BACKUP_AGE_KEY:-$HOME/.config/hikari/backup_age.key}" \
        -o "$VERIFY_TAR" "$BACKUP_TMP" 2>/dev/null; then
    if tar -xf "$VERIFY_TAR" -C "$VERIFY_DIR" hikari.db 2>/dev/null && [ -f "$VERIFY_DB" ]; then
        result=$("$SQLITE_BIN" "$VERIFY_DB" "PRAGMA quick_check;" 2>/dev/null || true)
        if [ "$result" = "ok" ]; then
            verify_ok=1
        else
            echo "backup: PRAGMA quick_check returned: $result" >&2
        fi
    fi
fi
rm -rf "$VERIFY_DIR"

if [ "$verify_ok" != "1" ]; then
    rm -f "$BACKUP_TMP"
    echo "backup: smoke-test failed — encrypted archive discarded." >&2
    rm -rf "$TMP_DIR"
    rm -f "$TMP_TAR"
    exit 1
fi

# Atomic promote: rename .tmp → final only after verification passes.
mv "$BACKUP_TMP" "$BACKUP_TAR_AGE"

# Cleanup temp files
rm -rf "$TMP_DIR"
rm -f "$TMP_TAR"

SIZE_KB=$(($(stat -f%z "$BACKUP_TAR_AGE") / 1024))
echo "backup: wrote $BACKUP_TAR_AGE (${SIZE_KB} KB)"

# Retention: 14 days — prune oldest .tar.age files
find "$BACKUP_DIR" -name 'hikari-*.tar.age' -mtime +$RETAIN_DAYS -print -delete 2>/dev/null | while read -r OLD; do
    echo "backup: pruned $OLD"
done

# Also prune any legacy plaintext .db backups beyond retention.
# Use find (not glob) so zsh NOMATCH doesn't trip when there are no matches.
find "$BACKUP_DIR" -maxdepth 1 -name 'hikari-*.db' -mtime +$RETAIN_DAYS -print -delete 2>/dev/null | while read -r OLD; do
    echo "backup: pruned legacy $OLD"
done
