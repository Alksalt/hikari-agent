#!/bin/bash
# Install the Phase 8 daily backup launchd job (com.hikari.backup) — runs at
# 03:00 each day, copies data/hikari.db to the alt-wiki vault on iCloud Drive,
# prunes to the last 14 days.
#
# Usage:
#   ./scripts/install_backup.sh             # install + load
#   ./scripts/install_backup.sh --uninstall

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.hikari.backup.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
SERVICE_NAME="gui/$(id -u)/com.hikari.backup"
SCRIPT_PATH="$REPO_DIR/scripts/backup.sh"

if [ "${1:-}" = "--uninstall" ]; then
    echo "uninstalling $SERVICE_NAME ..."
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "removed $PLIST_PATH"
    exit 0
fi

if [ ! -x "$SCRIPT_PATH" ]; then
    chmod +x "$SCRIPT_PATH"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hikari.backup</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$SCRIPT_PATH</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/hikari-backup.log</string>

    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/hikari-backup.err</string>

    <key>RunAtLoad</key>
    <false/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "wrote $PLIST_PATH"

# Bootout if already loaded, then bootstrap fresh.
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "$SERVICE_NAME"

echo
echo "installed. next backup fires at 03:00 local time."
echo "  manual run:  $SCRIPT_PATH"
echo "  next fires:  launchctl print $SERVICE_NAME | grep -i next"
echo "  log:         tail -f ~/Library/Logs/hikari-backup.log"
echo
echo "to uninstall: $0 --uninstall"
