#!/bin/bash
# Install (or reinstall) the Agent Capitol backend as a macOS launchd
# LaunchAgent so it starts at login and is restarted if it dies.
#
# Usage:
#   scripts/install-service.sh [--port N] [--env-file PATH] [--label NAME]
#
#   --port N        Listen on port N (default: PORT from backend/.env, else 8888)
#   --env-file P    KEY=VALUE file merged into the service environment
#                   (default: backend/.env if it exists)
#   --label NAME    launchd label (default: com.agent-capitol.server).
#                   Only useful for running a second, test copy.
#
# The job is bootstrapped into the per-user *gui* domain (not a system daemon)
# because the app posts desktop notifications via osascript, which only
# works from a logged-in GUI session.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$REPO_ROOT/venv/bin/python"
SERVER="$REPO_ROOT/backend/server.py"
TEMPLATE="$REPO_ROOT/launchd/com.agent-capitol.server.plist"
LABEL="com.agent-capitol.server"
ENV_FILE="$REPO_ROOT/backend/.env"
PORT_OVERRIDE=""
LOG_DIR="$HOME/Library/Logs/agent-capitol"
UID_NUM="$(id -u)"

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port)     [ $# -ge 2 ] || die "--port needs a value"; PORT_OVERRIDE="$2"; shift 2 ;;
        --port=*)   PORT_OVERRIDE="${1#--port=}"; shift ;;
        --env-file) [ $# -ge 2 ] || die "--env-file needs a value"; ENV_FILE="$2"; shift 2 ;;
        --label)    [ $# -ge 2 ] || die "--label needs a value"; LABEL="$2"; shift 2 ;;
        -h|--help)  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          die "unknown argument: $1 (see --help)" ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS launchd only"
[ -x "$PYTHON" ]  || die "venv python not found at $PYTHON (create it: python3 -m venv venv && ./venv/bin/pip install -r backend/requirements.txt)"
[ -f "$SERVER" ]  || die "server not found at $SERVER"
[ -f "$TEMPLATE" ] || die "plist template not found at $TEMPLATE"
if [ -n "$PORT_OVERRIDE" ]; then
    [[ "$PORT_OVERRIDE" =~ ^[0-9]+$ ]] && [ "$PORT_OVERRIDE" -ge 1 ] && [ "$PORT_OVERRIDE" -le 65535 ] \
        || die "--port must be an integer 1-65535, got '$PORT_OVERRIDE'"
fi

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
SERVICE="gui/$UID_NUM/$LABEL"
mkdir -p "$PLIST_DIR" "$LOG_DIR"

# ---------------------------------------------------------------------------
# 1. Render the plist: fill placeholders, merge env file, apply --port.
#    Done in Python (plistlib) so values are XML-escaped correctly.
# ---------------------------------------------------------------------------
RENDERED="$(mktemp -t agent-capitol-plist)"
trap 'rm -f "$RENDERED"' EXIT

LABEL="$LABEL" PYTHON="$PYTHON" SERVER="$SERVER" REPO_ROOT="$REPO_ROOT" LOG_DIR="$LOG_DIR" \
"$PYTHON" - "$TEMPLATE" "$RENDERED" "$ENV_FILE" "$PORT_OVERRIDE" <<'PY'
import os, plistlib, re, sys
template, out, env_file, port_override = sys.argv[1:5]
subs = {
    "__LABEL__": os.environ["LABEL"],
    "__PYTHON__": os.environ["PYTHON"],
    "__SERVER__": os.environ["SERVER"],
    "__REPO_ROOT__": os.environ["REPO_ROOT"],
    "__LOG_DIR__": os.environ["LOG_DIR"],
    "__HOME__": os.environ["HOME"],
}
with open(template, "rb") as f:
    data = plistlib.load(f)

def fill(v):
    if isinstance(v, str):
        for k, s in subs.items():
            v = v.replace(k, s)
        return v
    if isinstance(v, list):
        return [fill(x) for x in v]
    if isinstance(v, dict):
        return {k: fill(x) for k, x in v.items()}
    return v

data = fill(data)
env = data.setdefault("EnvironmentVariables", {})

# Merge KEY=VALUE lines from the env file (comments, blanks, `export ` allowed;
# surrounding single/double quotes stripped).
line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
if env_file and os.path.isfile(env_file):
    with open(env_file) as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = line_re.match(line)
            if not m:
                sys.stderr.write(f"warning: {env_file}:{n}: ignoring unparseable line\n")
                continue
            key, val = m.groups()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key == "PATH":
                sys.stderr.write("warning: PATH from env file ignored (set in template)\n")
                continue
            env[key] = val
            print(f"  env {key}=<from {os.path.basename(env_file)}>")

if port_override:
    env["PORT"] = port_override

for k in ("SSL_CERT", "SSL_KEY"):
    if k in env and not os.path.isfile(env[k]):
        sys.stderr.write(f"warning: {k}={env[k]} does not exist; server will fall back to HTTP\n")

with open(out, "wb") as f:
    plistlib.dump(data, f, sort_keys=False)
PY

# Effective port/scheme as the service will see them (for checks and messages).
read -r PORT SCHEME < <("$PYTHON" -c '
import plistlib, sys, os
e = plistlib.load(open(sys.argv[1], "rb")).get("EnvironmentVariables", {})
tls = e.get("SSL_CERT") and e.get("SSL_KEY") and os.path.isfile(e["SSL_CERT"]) and os.path.isfile(e["SSL_KEY"])
print(e.get("PORT", "8888"), "https" if tls else "http")' "$RENDERED")
plutil -lint -s "$RENDERED" >/dev/null || die "rendered plist failed plutil -lint"

# ---------------------------------------------------------------------------
# 2. Refuse if the port is held by something that is not this service
#    (typically a foreground `python backend/server.py`).
# ---------------------------------------------------------------------------
service_pid() {
    launchctl print "$SERVICE" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}'
}
SVC_PID="$(service_pid || true)"
LISTENERS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
for pid in $LISTENERS; do
    if [ "$pid" != "$SVC_PID" ]; then
        cmd="$(ps -o command= -p "$pid" 2>/dev/null || echo '?')"
        cat >&2 <<EOF
error: port $PORT is already in use by PID $pid, which is not the $LABEL service:
         $cmd
       Stop that process first (if it's your foreground 'python backend/server.py',
       press Ctrl-C in that terminal), then re-run this installer.
       Or pick another port: $0 --port <N>
EOF
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# 3. Install: bootout any existing copy (idempotent), write plist, bootstrap.
# ---------------------------------------------------------------------------
if launchctl print "$SERVICE" >/dev/null 2>&1; then
    echo "Stopping existing $LABEL ..."
    launchctl bootout "$SERVICE" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    # bootout is asynchronous; wait for the old process to go away.
    for _ in $(seq 1 50); do launchctl print "$SERVICE" >/dev/null 2>&1 || break; sleep 0.2; done
fi

install -m 0644 "$RENDERED" "$PLIST"
echo "Wrote $PLIST"

if ! launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>"$RENDERED.err"; then
    echo "launchctl bootstrap failed ($(tr -d '\n' <"$RENDERED.err")); falling back to launchctl load" >&2
    rm -f "$RENDERED.err"
    launchctl load -w "$PLIST" || die "launchctl load failed"
fi
rm -f "$RENDERED.err"

# ---------------------------------------------------------------------------
# 4. Verify it came up.
# ---------------------------------------------------------------------------
echo -n "Waiting for $SCHEME://localhost:$PORT "
for _ in $(seq 1 30); do
    if curl -sk -o /dev/null --max-time 2 "$SCHEME://127.0.0.1:$PORT/api/projects"; then
        echo
        echo "OK: $LABEL is running (pid $(service_pid)) on $SCHEME://localhost:$PORT"
        echo "Logs:    $LOG_DIR/server.out.log, $LOG_DIR/server.err.log"
        echo "Restart: launchctl kickstart -k $SERVICE"
        echo "Remove:  $SCRIPT_DIR/uninstall-service.sh"
        exit 0
    fi
    echo -n .
    sleep 0.5
done
echo
echo "warning: service loaded but $SCHEME://localhost:$PORT did not answer within 15s." >&2
echo "         Check: launchctl print $SERVICE ; tail -50 $LOG_DIR/server.err.log" >&2
exit 2
