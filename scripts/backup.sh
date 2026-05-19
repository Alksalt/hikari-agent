#!/bin/zsh
# Phase 8 daily backup — copy data/hikari.db to the alt-wiki vault on iCloud
# Drive. iCloud handles cross-device sync + version history. Keep the last 14.
#
# Run manually:   ./scripts/backup.sh
# Run via launchd: see ./scripts/install_backup.sh (daily at 03:00).
#
# Failure modes are intentional silent-failures (logged via launchd stderr).
# Never raise — backup must not bring down the bot.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${(%):-%x}")/.." && pwd)"
SRC="$REPO_DIR/data/hikari.db"
DEST_DIR="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki/projects/hikari-agent/backups"
RETAIN_DAYS=14

if [ ! -f "$SRC" ]; then
    echo "backup: source $SRC does not exist; skipping." >&2
    exit 0
fi

mkdir -p "$DEST_DIR"

DEST="$DEST_DIR/hikari-$(date +%Y%m%d).db"
# Skip if today's backup already exists (idempotent for re-runs).
if [ -f "$DEST" ]; then
    echo "backup: $DEST already present; skipping."
    exit 0
fi

# CRITICAL: hikari.db runs in SQLite WAL mode. A plain `cp` would copy the
# main file without the un-checkpointed WAL pages, producing a corrupt /
# stale backup. Use sqlite3's online backup API which is atomic, snapshots
# the database including uncommitted WAL, and emits a single clean file.
SQLITE_BIN="$(command -v sqlite3 || true)"
if [ -z "$SQLITE_BIN" ]; then
    echo "backup: sqlite3 not found in PATH; cannot safely back up a WAL database." >&2
    exit 1
fi
"$SQLITE_BIN" "$SRC" ".backup '$DEST'"
SIZE_KB=$(($(stat -f%z "$DEST") / 1024))
echo "backup: wrote $DEST (${SIZE_KB} KB)"

# Prune oldest beyond RETAIN_DAYS entries. `ls -t` is newest first; tail keeps
# everything after the first RETAIN_DAYS lines.
ls -t "$DEST_DIR"/hikari-*.db 2>/dev/null | tail -n +$((RETAIN_DAYS + 1)) | while read -r OLD; do
    rm -- "$OLD"
    echo "backup: pruned $OLD"
done
