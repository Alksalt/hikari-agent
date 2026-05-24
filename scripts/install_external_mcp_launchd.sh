#!/usr/bin/env bash
# Sprint 7F: install the com.hikari.mcp LaunchAgent (mcp_external server).
#
# Usage:
#   ./scripts/install_external_mcp_launchd.sh             # install + load
#   ./scripts/install_external_mcp_launchd.sh --uninstall

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.hikari.mcp.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
SERVICE_NAME="gui/$(id -u)/com.hikari.mcp"
LOG_DIR="$REPO_DIR/data/logs"

if [ "${1:-}" = "--uninstall" ]; then
    echo "uninstalling $SERVICE_NAME ..."
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "removed $PLIST_PATH"
    exit 0
fi

OPERATOR="$(whoami)"
UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
HOME_BIN="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

sed \
    -e "s|__OPERATOR__|$OPERATOR|g" \
    -e "s|/Users/__OPERATOR__/.local/bin/uv|$UV_BIN|g" \
    "$REPO_DIR/scripts/launchd_mcp_external.plist" > "$PLIST_PATH"

# Patch working directory and PATH to actual repo/home paths.
sed -i "" \
    -e "s|/Users/$OPERATOR/agents/hikari-agent|$REPO_DIR|g" \
    -e "s|/Users/__OPERATOR__/.local/bin|$HOME/.local/bin|g" \
    "$PLIST_PATH"

echo "wrote $PLIST_PATH"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "$SERVICE_NAME"

echo ""
echo "installed com.hikari.mcp (external MCP server)."
echo "  status:  launchctl print $SERVICE_NAME"
echo "  logs:    tail -f $LOG_DIR/mcp_external.out.log"
echo "  errors:  tail -f $LOG_DIR/mcp_external.err.log"
echo ""
echo "to uninstall: $0 --uninstall"
