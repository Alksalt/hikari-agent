#!/bin/bash
# Install hikari-agent as a launchd LaunchAgent — restarts on ANY exit (crash,
# clean SIGTERM, sleep/logout) and on reboot. See the KeepAlive note below.
#
# Usage:
#   ./scripts/install_launchd.sh         # install + bootstrap
#   ./scripts/install_launchd.sh --uninstall
#
# Logs land in ~/Library/Logs/hikari.{log,err}.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.hikari.agent.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
SERVICE_NAME="gui/$(id -u)/com.hikari.agent"

UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
    echo "error: uv not found in PATH. install uv first." >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    echo "uninstalling $SERVICE_NAME ..."
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "removed $PLIST_PATH"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hikari.agent</string>

    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string>
        <string>run</string>
        <string>python</string>
        <string>-m</string>
        <string>agents.telegram_bridge</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <!-- Unconditional KeepAlive: relaunch on ANY exit, including a graceful
         exit-0. python-telegram-bot's run_polling() returns 0 on SIGTERM
         (sleep / logout / launchctl stop / kickstart), and the old
         {SuccessfulExit:false} told launchd to relaunch ONLY on failure — so a
         clean SIGTERM left the bot silently dead until a human rebooted her.
         ThrottleInterval below caps any crash-loop rate. -->
    <key>KeepAlive</key>
    <true/>

    <key>ProcessType</key>
    <string>Interactive</string>

    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/hikari.log</string>

    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/hikari.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>ThrottleInterval</key>
    <integer>15</integer>
</dict>
</plist>
EOF

echo "wrote $PLIST_PATH"

# Bootout if already loaded, then bootstrap fresh.
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "$SERVICE_NAME"
launchctl kickstart -k "$SERVICE_NAME"

echo
echo "installed + started. check it:"
echo "  launchctl print $SERVICE_NAME | head -20"
echo "  tail -f ~/Library/Logs/hikari.log"
echo
echo "to stop: launchctl bootout gui/\$(id -u) $PLIST_PATH"
echo "to uninstall: $0 --uninstall"
