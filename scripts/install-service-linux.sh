#!/bin/bash
# Install (or reinstall) the Agent Capitol backend as a systemd service on
# Linux — written for Ubuntu inside WSL2 (see docs/WINDOWS-SERVER.md), works on
# any systemd distro. Starts at boot, restarts if it dies.
#
# Usage:
#   scripts/install-service-linux.sh [--port N] [--env-file PATH]
#
#   --port N        Listen on port N (default: PORT from backend/.env, else 8888)
#   --env-file P    KEY=VALUE file loaded into the service environment
#                   (default: backend/.env; missing file is fine)
#
# Needs sudo (writes /etc/systemd/system). The service runs as *you*, not root,
# so it finds your claude login, your tmux socket and your project repos.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$REPO_ROOT/venv/bin/python"
SERVER="$REPO_ROOT/backend/server.py"
TEMPLATE="$REPO_ROOT/systemd/agent-capitol.service"
UNIT_NAME="agent-capitol"
UNIT="/etc/systemd/system/$UNIT_NAME.service"
ENV_FILE="$REPO_ROOT/backend/.env"
PORT_OVERRIDE=""

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port)     [ $# -ge 2 ] || die "--port needs a value"; PORT_OVERRIDE="$2"; shift 2 ;;
        --port=*)   PORT_OVERRIDE="${1#--port=}"; shift ;;
        --env-file) [ $# -ge 2 ] || die "--env-file needs a value"; ENV_FILE="$2"; shift 2 ;;
        -h|--help)  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          die "unknown argument: $1 (see --help)" ;;
    esac
done

[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux systemd only (macOS: scripts/install-service.sh)"
command -v systemctl >/dev/null || die "systemctl not found"
if [ ! -d /run/systemd/system ]; then
    if grep -qi microsoft /proc/version 2>/dev/null; then
        die "systemd is not running inside this WSL distro. scripts/setup-linux.sh enables it; then run 'wsl --shutdown' from PowerShell, reopen Ubuntu and re-run this."
    fi
    die "systemd is not running on this system"
fi
[ -x "$PYTHON" ] || die "venv python not found at $PYTHON (create it: python3 -m venv venv && ./venv/bin/pip install -r backend/requirements.txt)"
[ -f "$SERVER" ] || die "server not found at $SERVER"
[ -f "$TEMPLATE" ] || die "unit template not found at $TEMPLATE"
[ -f "$REPO_ROOT/frontend/dist/index.html" ] || echo "warning: frontend/dist not built yet (cd frontend && npm run build) — the service serves it" >&2
if [ -n "$PORT_OVERRIDE" ]; then
    [[ "$PORT_OVERRIDE" =~ ^[0-9]+$ ]] && [ "$PORT_OVERRIDE" -ge 1 ] && [ "$PORT_OVERRIDE" -le 65535 ] \
        || die "--port must be an integer 1-65535, got '$PORT_OVERRIDE'"
fi

# Effective port: --port, else PORT= in the env file, else 8888. --port is
# persisted into the env file so the unit's EnvironmentFile carries it.
PORT="8888"
if [ -f "$ENV_FILE" ]; then
    v="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?PORT[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -1 | tr -d '"'"'" )"
    [ -n "$v" ] && PORT="$v"
fi
if [ -n "$PORT_OVERRIDE" ]; then
    PORT="$PORT_OVERRIDE"
    touch "$ENV_FILE"
    if grep -q '^[[:space:]]*\(export[[:space:]]\+\)\?PORT[[:space:]]*=' "$ENV_FILE"; then
        sed -i "s|^[[:space:]]*\(export[[:space:]]\+\)\?PORT[[:space:]]*=.*|PORT=$PORT|" "$ENV_FILE"
    else
        printf 'PORT=%s\n' "$PORT" >>"$ENV_FILE"
    fi
fi
SCHEME=http
if [ -f "$ENV_FILE" ] && grep -q '^SSL_CERT=' "$ENV_FILE" && grep -q '^SSL_KEY=' "$ENV_FILE"; then
    SCHEME=https
fi

# Refuse if something that is not this service already holds the port
# (typically a foreground `python backend/server.py`).
service_pid() { systemctl show -p MainPID --value "$UNIT_NAME" 2>/dev/null || true; }
holder="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1 || true)"
if [ -n "$holder" ] && [ "$holder" != "$(service_pid)" ]; then
    die "port $PORT is already in use by pid $holder ($(ps -o comm= -p "$holder" 2>/dev/null || echo '?')). Stop it (Ctrl-C in its terminal) or pick another port: $0 --port <N>"
fi

RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
sed -e "s|__USER__|$(id -un)|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__SERVER__|$SERVER|g" \
    -e "s|__ENV_FILE__|$ENV_FILE|g" \
    -e "s|__PORT__|$PORT|g" \
    "$TEMPLATE" >"$RENDERED"
grep -q '__[A-Z_]*__' "$RENDERED" && die "unfilled placeholder in rendered unit"

echo "Installing $UNIT (needs sudo) ..."
sudo install -m 0644 "$RENDERED" "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT_NAME" >/dev/null
sudo systemctl restart "$UNIT_NAME"

echo -n "Waiting for $SCHEME://localhost:$PORT "
for _ in $(seq 1 30); do
    if curl -sk -o /dev/null --max-time 2 "$SCHEME://127.0.0.1:$PORT/api/projects"; then
        echo
        echo "OK: $UNIT_NAME is running (pid $(service_pid)) on $SCHEME://localhost:$PORT"
        echo "Logs:    journalctl -u $UNIT_NAME -f"
        echo "Restart: sudo systemctl restart $UNIT_NAME"
        echo "Status:  systemctl status $UNIT_NAME"
        echo "Remove:  $SCRIPT_DIR/uninstall-service-linux.sh"
        exit 0
    fi
    echo -n .
    sleep 0.5
done
echo
echo "warning: service started but $SCHEME://localhost:$PORT did not answer within 15s." >&2
echo "         Check: systemctl status $UNIT_NAME ; journalctl -u $UNIT_NAME -n 50" >&2
exit 2
