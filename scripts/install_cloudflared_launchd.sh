#!/usr/bin/env bash
# Sprint 7F: install the com.hikari.tunnel LaunchAgent (cloudflared tunnel).
#
# Prerequisite: cloudflared must be installed (brew install cloudflared) and
# the tunnel must already be created and authenticated:
#   cloudflared tunnel login
#   cloudflared tunnel create hikari-mcp
#   cloudflared tunnel route dns hikari-mcp hikari.your-domain.com
#
# Usage:
#   ./scripts/install_cloudflared_launchd.sh             # install + load
#   ./scripts/install_cloudflared_launchd.sh --uninstall

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.hikari.tunnel.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
SERVICE_NAME="gui/$(id -u)/com.hikari.tunnel"
LOG_DIR="$REPO_DIR/data/logs"

if [ "${1:-}" = "--uninstall" ]; then
    echo "uninstalling $SERVICE_NAME ..."
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "removed $PLIST_PATH"
    exit 0
fi

# Locate cloudflared
CLOUDFLARED_BIN="$(command -v cloudflared || echo "/opt/homebrew/bin/cloudflared")"
if [ ! -x "$CLOUDFLARED_BIN" ]; then
    echo "error: cloudflared not found at $CLOUDFLARED_BIN" >&2
    echo "  Install via: brew install cloudflared" >&2
    echo "  Then authenticate: cloudflared tunnel login" >&2
    exit 1
fi

# Verify tunnel credentials exist
if [ ! -d "$HOME/.cloudflared" ]; then
    echo "error: ~/.cloudflared not found" >&2
    echo "  Run: cloudflared tunnel login && cloudflared tunnel create hikari-mcp" >&2
    exit 1
fi

OPERATOR="$(whoami)"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

sed \
    -e "s|__OPERATOR__|$OPERATOR|g" \
    -e "s|/opt/homebrew/bin/cloudflared|$CLOUDFLARED_BIN|g" \
    "$REPO_DIR/scripts/launchd_cloudflared.plist" > "$PLIST_PATH"

# Patch paths
sed -i "" \
    -e "s|/Users/$OPERATOR/agents/hikari-agent|$REPO_DIR|g" \
    "$PLIST_PATH"

echo "wrote $PLIST_PATH"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "$SERVICE_NAME"

echo ""
echo "installed com.hikari.tunnel (cloudflared tunnel run hikari-mcp)."
echo "  status:    launchctl print $SERVICE_NAME"
echo "  logs:      tail -f $LOG_DIR/cloudflared.out.log"
echo "  errors:    tail -f $LOG_DIR/cloudflared.err.log"
echo "  smoke test: curl -H \"Authorization: Bearer \$HIKARI_MCP_SECRET\" https://your-tunnel-url/mcp"
echo ""
echo "to uninstall: $0 --uninstall"
