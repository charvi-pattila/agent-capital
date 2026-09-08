#!/bin/bash
# One-shot bootstrap of Agent Capitol on a fresh Ubuntu machine — written for
# Ubuntu inside WSL2 on a Windows PC used as an always-on server
# (docs/WINDOWS-SERVER.md), works on a bare Ubuntu/Debian box too.
#
# Idempotent: re-run it any time (after `git pull`, to change the URL, ...).
#
# Usage (from the repo root, as your normal user — it sudo's where needed):
#   scripts/setup-linux.sh [--public-url https://pc.tailnet.ts.net] [--port N] [--no-service]
#
#   --public-url U  The https address the phone will use (Tailscale MagicDNS
#                   name). Stored as PUBLIC_URL in backend/.env; shown with a QR
#                   code in the About tab. Can be added later.
#   --port N        Backend port (default 8888).
#   --no-service    Do everything except install/start the systemd service.
#
# What it does, in order:
#   1. apt: tmux, git, curl, python3 + venv
#   2. Node.js 22 (NodeSource) if node < 18 or missing — needed to build the UI
#   3. Claude Code CLI (native installer -> ~/.local/bin/claude)
#   4. Python venv + backend requirements
#   5. Frontend build (frontend/dist, served by the backend)
#   6. Login password for the dashboard (prompts, once)
#   7. backend/.env (PORT / PUBLIC_URL)
#   8. WSL only: enables systemd in /etc/wsl.conf if it isn't already
#   9. systemd service (scripts/install-service-linux.sh)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/backend/.env"
PUBLIC_URL=""
PORT=""
INSTALL_SERVICE=1

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --public-url)   [ $# -ge 2 ] || die "--public-url needs a value"; PUBLIC_URL="$2"; shift 2 ;;
        --public-url=*) PUBLIC_URL="${1#--public-url=}"; shift ;;
        --port)         [ $# -ge 2 ] || die "--port needs a value"; PORT="$2"; shift 2 ;;
        --port=*)       PORT="${1#--port=}"; shift ;;
        --no-service)   INSTALL_SERVICE=0; shift ;;
        -h|--help)      sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              die "unknown argument: $1 (see --help)" ;;
    esac
done

[ "$(uname -s)" = "Linux" ] || die "this script is for Linux (macOS: see README 'Run as a background service')"
[ "$(id -u)" != "0" ] || die "run as your normal user, not root (the service must run as you so it finds your claude login)"
command -v apt-get >/dev/null || die "apt-get not found — this script targets Ubuntu/Debian"
IS_WSL=0; grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1

# Persist KEY=VALUE into backend/.env (replace if present, append if not).
set_env() {
    local key="$1" val="$2"
    touch "$ENV_FILE"
    if grep -q "^[[:space:]]*\(export[[:space:]]\+\)\?$key[[:space:]]*=" "$ENV_FILE"; then
        sed -i "s|^[[:space:]]*\(export[[:space:]]\+\)\?$key[[:space:]]*=.*|$key=$val|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$val" >>"$ENV_FILE"
    fi
}

step "1/9 System packages (sudo)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmux git curl ca-certificates python3 python3-venv python3-pip >/dev/null
echo "tmux $(tmux -V | awk '{print $2}'), $(python3 --version)"

step "2/9 Node.js"
need_node=1
if command -v node >/dev/null; then
    major="$(node -v | sed 's/^v\([0-9]*\).*/\1/')"
    [ "$major" -ge 18 ] && need_node=0
fi
if [ "$need_node" = 1 ]; then
    echo "Installing Node.js 22 from NodeSource ..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
    sudo apt-get install -y -qq nodejs >/dev/null
fi
echo "node $(node -v), npm $(npm -v)"

step "3/9 Claude Code CLI"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude >/dev/null; then
    echo "Installing claude (native installer) ..."
    curl -fsSL https://claude.ai/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
fi
command -v claude >/dev/null || die "claude still not on PATH after install (expected ~/.local/bin/claude)"
echo "claude $(claude --version 2>/dev/null | head -1)"
if ! grep -q 'HOME/.local/bin' "$HOME/.bashrc" 2>/dev/null; then
    printf '\n# claude CLI\nexport PATH="$HOME/.local/bin:$PATH"\n' >>"$HOME/.bashrc"
fi
CLAUDE_LOGGED_IN=0
if [ -f "$HOME/.claude/.credentials.json" ] || grep -q '"oauthAccount"' "$HOME/.claude.json" 2>/dev/null; then
    CLAUDE_LOGGED_IN=1
fi

step "4/9 Python venv"
[ -x "$REPO_ROOT/venv/bin/python" ] || python3 -m venv "$REPO_ROOT/venv"
"$REPO_ROOT/venv/bin/pip" install -q -r "$REPO_ROOT/backend/requirements.txt"
echo "venv ok"

step "5/9 Frontend build"
( cd "$REPO_ROOT/frontend" && { [ -f package-lock.json ] && npm ci --silent || npm install --silent; } && npm run build --silent )
[ -f "$REPO_ROOT/frontend/dist/index.html" ] || die "frontend build did not produce frontend/dist/index.html"
echo "frontend/dist built"

step "6/9 Dashboard password"
if [ -f "$REPO_ROOT/data/.auth_hash" ]; then
    echo "already set (re-run backend/set_password.py to change it)"
elif [ -t 0 ]; then
    echo "The dashboard will be reachable from your phone over Tailscale; set a password for it."
    "$REPO_ROOT/venv/bin/python" "$REPO_ROOT/backend/set_password.py"
else
    echo "warning: no TTY, skipping. Run: venv/bin/python backend/set_password.py" >&2
fi

step "7/9 backend/.env"
[ -n "$PORT" ] && set_env PORT "$PORT"
if [ -n "$PUBLIC_URL" ]; then
    set_env PUBLIC_URL "${PUBLIC_URL%/}"
fi
# The backend must listen on all interfaces inside WSL so Windows' localhost
# forwarding (and Tailscale on the Windows side) can reach it.
grep -q '^HOST=' "$ENV_FILE" 2>/dev/null || set_env HOST 0.0.0.0
echo "--- $ENV_FILE"; cat "$ENV_FILE"

if [ "$IS_WSL" = 1 ]; then
    step "8/9 WSL: systemd"
    if [ -d /run/systemd/system ]; then
        echo "systemd is running"
    else
        if ! grep -q '^systemd[[:space:]]*=[[:space:]]*true' /etc/wsl.conf 2>/dev/null; then
            echo "Enabling systemd in /etc/wsl.conf (sudo) ..."
            if grep -q '^\[boot\]' /etc/wsl.conf 2>/dev/null; then
                sudo sed -i 's/^\[boot\]/[boot]\nsystemd=true/' /etc/wsl.conf
            else
                printf '\n[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
            fi
        fi
        cat <<MSG

systemd is enabled but WSL must be restarted for it to start. In PowerShell:

    wsl --shutdown

then reopen Ubuntu and run this script again — every step above is already
done, it will skip straight to installing the service.
MSG
        exit 0
    fi
else
    step "8/9 (not WSL, nothing to do)"
fi

if [ "$INSTALL_SERVICE" = 1 ]; then
    step "9/9 systemd service"
    "$SCRIPT_DIR/install-service-linux.sh"
else
    step "9/9 service skipped (--no-service). Run it by hand: set -a; . backend/.env; set +a; venv/bin/python backend/server.py"
fi

cat <<MSG

== Done on the Linux side.
MSG
if [ "$CLAUDE_LOGGED_IN" = 0 ]; then
    cat <<MSG
!! Claude Code is not logged in yet on this machine. Run:

       claude

   type /login, open the printed URL on any device, sign in, paste the code
   back. Sessions started from the dashboard fail until this is done.

MSG
fi
if [ "$IS_WSL" = 1 ]; then
    cat <<MSG
Next, on the Windows side (docs/WINDOWS-SERVER.md has the details):
  1. Power: never sleep;  powercfg /h off
  2. Task Scheduler job at logon: wsl.exe -d Ubuntu --exec /bin/true  (boots this distro, and with it the service)
  3. Auto sign-in so that job fires after an unattended reboot
  4. Tailscale for Windows, signed in;  tailscale serve --bg ${PORT:-8888}
     -> phone opens https://<pc-name>.<tailnet>.ts.net
  5. Re-run:  scripts/setup-linux.sh --public-url https://<pc-name>.<tailnet>.ts.net
     so the About tab shows that address with a QR code.
MSG
fi
