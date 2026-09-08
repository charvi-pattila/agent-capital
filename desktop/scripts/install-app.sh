#!/usr/bin/env bash
# Copy the built "Agent Capital.app" into /Applications (fallback: ~/Applications),
# replacing any previous copy. Run `npm run build` first.
set -euo pipefail
cd "$(dirname "$0")/.."

APP="$(find dist -maxdepth 3 -name 'Agent Capital.app' -type d | head -n1 || true)"
if [[ -z "$APP" ]]; then
  echo "Built app not found under desktop/dist — run 'npm run build' first." >&2
  exit 1
fi

DEST_DIR=/Applications
if [[ ! -w "$DEST_DIR" ]]; then
  DEST_DIR="$HOME/Applications"
  mkdir -p "$DEST_DIR"
fi
DEST="$DEST_DIR/Agent Capital.app"

if [[ -d "$DEST" ]]; then
  # Ask the app to quit if it's running from the destination, then replace it.
  osascript -e 'tell application "Agent Capital" to quit' >/dev/null 2>&1 || true
  rm -rf "$DEST"
fi
# ditto preserves the bundle structure, symlinks and extended attributes.
ditto "$APP" "$DEST"
echo "Installed: $DEST"
echo "Open it with: open \"$DEST\"   (or from Launchpad / Spotlight)"
