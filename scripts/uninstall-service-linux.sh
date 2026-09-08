#!/bin/bash
# Stop, disable and remove the Agent Capitol systemd service (Linux).
# Usage: scripts/uninstall-service-linux.sh
set -euo pipefail
UNIT_NAME="agent-capitol"
UNIT="/etc/systemd/system/$UNIT_NAME.service"
if [ -f "$UNIT" ] || systemctl list-unit-files "$UNIT_NAME.service" --no-legend 2>/dev/null | grep -q .; then
    echo "Stopping $UNIT_NAME (needs sudo) ..."
    sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    sudo rm -f "$UNIT"
    sudo systemctl daemon-reload
    echo "Removed $UNIT (journal logs are kept: journalctl -u $UNIT_NAME)"
else
    echo "$UNIT_NAME is not installed."
fi
