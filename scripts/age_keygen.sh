#!/usr/bin/env bash
# Sprint 7F: one-shot age keypair generator for Hikari backup encryption.
#
# Creates:
#   ~/.config/hikari/backup_age.key  (private key, mode 600)
#   ~/.config/hikari/backup_age.pub  (public key, mode 644)
#
# The public key is what backup.sh and install_backup.sh reference.
# STORE THE PRIVATE KEY SOMEWHERE SAFE OFF THIS MACHINE before relying on
# encrypted backups — without it, you cannot decrypt your backups.
#
# Usage:
#   bash scripts/age_keygen.sh

set -euo pipefail

if ! command -v age-keygen >/dev/null 2>&1; then
    echo "error: age-keygen not found — install via: brew install age" >&2
    exit 1
fi

KEY_DIR="${HOME}/.config/hikari"
KEY_FILE="$KEY_DIR/backup_age.key"
PUB_FILE="$KEY_DIR/backup_age.pub"

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [ -f "$KEY_FILE" ]; then
    echo "key already exists at $KEY_FILE — refusing to overwrite" >&2
    echo "if you really want a new key, delete it first: rm $KEY_FILE $PUB_FILE" >&2
    exit 1
fi

age-keygen -o "$KEY_FILE"
chmod 600 "$KEY_FILE"

# Extract public key from the generated key file comment line.
grep -F "public key:" "$KEY_FILE" | sed 's/^# *public key: *//' > "$PUB_FILE"
chmod 644 "$PUB_FILE"

echo "keypair generated:"
echo "  private: $KEY_FILE (mode 600)"
echo "  public:  $PUB_FILE (mode 644)"
echo ""
echo "IMPORTANT: store $KEY_FILE somewhere SAFE OFF this machine before relying on backups."
echo "Without the private key, encrypted backups are unrecoverable."
