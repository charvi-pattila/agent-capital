#!/bin/bash
# Stop and remove the Agent Capital launchd LaunchAgent.
#
# Usage: scripts/uninstall-service.sh [--label NAME]
#
# Logs in ~/Library/Logs/agent-capital/ are left in place.
set -euo pipefail

LABEL="com.agent-capital.server"
while [ $# -gt 0 ]; do
    case "$1" in
        --label) [ $# -ge 2 ] || { echo "error: --label needs a value" >&2; exit 1; }; LABEL="$2"; shift 2 ;;
        -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "error: unknown argument: $1" >&2; exit 1 ;;
    esac
done

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVICE="gui/$(id -u)/$LABEL"

if launchctl print "$SERVICE" >/dev/null 2>&1; then
    echo "Stopping $LABEL ..."
    launchctl bootout "$SERVICE" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    for _ in $(seq 1 50); do launchctl print "$SERVICE" >/dev/null 2>&1 || break; sleep 0.2; done
    if launchctl print "$SERVICE" >/dev/null 2>&1; then
        echo "warning: $SERVICE still registered with launchd" >&2
    fi
else
    echo "$LABEL is not loaded."
fi

if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Removed $PLIST"
else
    echo "No plist at $PLIST"
fi
echo "Done. Logs kept in $HOME/Library/Logs/agent-capital/"
