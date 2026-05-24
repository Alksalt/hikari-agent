#!/usr/bin/env bash
# Sprint 7F: install the com.hikari.deadman LaunchAgent (runs every 5 min).
#
# Usage:
#   ./scripts/install_deadman.sh             # install + load
#   ./scripts/install_deadman.sh --uninstall

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.hikari.deadman.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
SERVICE_NAME="gui/$(id -u)/com.hikari.deadman"
LOG_DIR="$REPO_DIR/data/logs"

if [ "${1:-}" = "--uninstall" ]; then
    echo "uninstalling $SERVICE_NAME ..."
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "removed $PLIST_PATH"
    exit 0
fi

# Require HIKARI_DEADMAN_BOT_TOKEN and OWNER_TELEGRAM_ID
DEADMAN_TOKEN="${HIKARI_DEADMAN_BOT_TOKEN:-}"
OWNER_ID="${OWNER_TELEGRAM_ID:-}"

if [ -z "$DEADMAN_TOKEN" ]; then
    echo "error: HIKARI_DEADMAN_BOT_TOKEN is not set" >&2
    echo "  Create a separate Telegram bot via @BotFather and set the token." >&2
    exit 1
fi

if [ -z "$OWNER_ID" ]; then
    echo "error: OWNER_TELEGRAM_ID is not set" >&2
    exit 1
fi

OPERATOR="$(whoami)"
UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

# Substitute placeholders into the template.
sed \
    -e "s|__OPERATOR__|$OPERATOR|g" \
    -e "s|__DEADMAN_TOKEN__|$DEADMAN_TOKEN|g" \
    -e "s|__OWNER__|$OWNER_ID|g" \
    -e "s|/Users/__OPERATOR__/.local/bin/uv|$UV_BIN|g" \
    "$REPO_DIR/scripts/launchd_deadman.plist" > "$PLIST_PATH"

# Also patch the WorkingDirectory to the actual repo path.
sed -i "" "s|/Users/$OPERATOR/agents/hikari-agent|$REPO_DIR|g" "$PLIST_PATH"

chmod 600 "$PLIST_PATH"

echo "wrote $PLIST_PATH"

# Bootout if already loaded, then bootstrap fresh.
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "$SERVICE_NAME"

echo ""
echo "installed. deadman check fires every 5 minutes."
echo "  logs:      tail -f $LOG_DIR/deadman.out.log"
echo "  errors:    tail -f $LOG_DIR/deadman.err.log"
echo "  dry-run:   uv run python scripts/dead_man.py --dry-run"
echo ""
echo "to uninstall: $0 --uninstall"
