#!/usr/bin/env bash
# tailscale-https.sh — give Agent Capitol a stable https:// URL over Tailscale.
#
# What it does (idempotent; safe to re-run any time):
#   1. Finds the Tailscale CLI (Mac app bundle first, then PATH).
#   2. Checks that this Mac is logged in to a tailnet; if not, prints the exact
#      manual steps and exits without touching anything.
#   3. Reads this machine's MagicDNS name (Self.DNSName).
#   4. Fetches/renews a Let's Encrypt certificate for that name with
#      `tailscale cert` into ~/.agent-capitol/certs/.
#   5. Writes SSL_CERT= / SSL_KEY= into backend/.env (creates the file if
#      missing, replaces existing values, never duplicates keys).
#   6. Prints the final URL and the restart command.
#
# Renewal: Tailscale certs are valid ~90 days. Re-running this script (with or
# without --renew) refreshes the files from `tailscale cert`, which renews the
# certificate automatically when it is inside the renewal window.
#
# Overrides (environment variables):
#   AGENT_CAPITOL_ENV_FILE   path of the env file to update  (default: <repo>/backend/.env)
#   AGENT_CAPITOL_CERT_DIR   where to put the cert/key       (default: ~/.agent-capitol/certs)
#   TAILSCALE_BIN            explicit path to the tailscale CLI
#   PORT                     port used in the printed URL    (default: PORT= from env file, else 8888)
#
# Exit codes: 0 done, 1 prerequisite missing (message explains), 2 tailscale cert failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${AGENT_CAPITOL_ENV_FILE:-$REPO_ROOT/backend/.env}"
CERT_DIR="${AGENT_CAPITOL_CERT_DIR:-$HOME/.agent-capitol/certs}"
SERVICE_LABEL="com.agent-capitol.server"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \{0,1\}//'
}

RENEW=0
for arg in "$@"; do
  case "$arg" in
    --renew) RENEW=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit "${2:-1}"; }

# ---------------------------------------------------------------- 1. find CLI
step "Locating the Tailscale CLI"
TS=""
if [ -n "${TAILSCALE_BIN:-}" ] && [ -x "$TAILSCALE_BIN" ]; then
  TS="$TAILSCALE_BIN"
elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
elif command -v tailscale >/dev/null 2>&1; then
  TS="$(command -v tailscale)"
fi

if [ -z "$TS" ]; then
  cat <<MSG
Tailscale is not installed on this Mac.

  Install it with Homebrew (asks for your macOS password because the
  installer package runs as root):

      brew install --cask tailscale

  or download it from https://tailscale.com/download/mac

Then open Tailscale.app, sign in, and re-run this script.
MSG
  exit 1
fi
say "using $TS ($("$TS" --version 2>/dev/null | head -n1 || echo 'version unknown'))"

# ------------------------------------------------------------ 2. login check
step "Checking Tailscale login state"
STATUS_JSON="$("$TS" status --json 2>/dev/null || true)"

# Tiny JSON reader: python3 ships with macOS; fall back to grep if it is absent.
json_get() {  # json_get '<expr over obj d>'  (prints "" on any failure)
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$STATUS_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    v = $1
    print(v if v is not None else '')
except Exception:
    print('')
" 2>/dev/null
  else
    printf '%s' "$STATUS_JSON" | grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n1 | sed 's/.*:[[:space:]]*"\(.*\)"/\1/'
  fi
}

BACKEND_STATE="$(json_get "d.get('BackendState')" BackendState)"
if [ "$BACKEND_STATE" != "Running" ]; then
  cat <<MSG
This Mac is not connected to a tailnet (state: ${BACKEND_STATE:-unknown / app not running}).

Do these once, then re-run this script:

  1. Open Tailscale.app (Spotlight -> "Tailscale") and click "Log in".
     Sign in with the SAME account you will use on the iPhone.
  2. In the admin console, https://login.tailscale.com/admin/dns :
       - MagicDNS            -> Enable
       - HTTPS Certificates  -> Enable
  3. Re-run:  $0
MSG
  exit 1
fi

# --------------------------------------------------------------- 3. DNS name
step "Reading this machine's MagicDNS name"
DNS_NAME="$(json_get "(d.get('Self') or {}).get('DNSName')" DNSName)"
DNS_NAME="${DNS_NAME%.}"   # strip the trailing dot
if [ -z "$DNS_NAME" ] || [ "$DNS_NAME" = "${DNS_NAME%%.*}" ]; then
  cat <<MSG
Could not read a full MagicDNS name for this machine (got: '${DNS_NAME:-<empty>}').

Enable MagicDNS in the admin console (https://login.tailscale.com/admin/dns),
wait a few seconds, then re-run this script.
MSG
  exit 1
fi
say "machine name: $DNS_NAME"

# ------------------------------------------------------------ 4. certificate
step "Fetching / renewing the HTTPS certificate"
mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"
CERT_FILE="$CERT_DIR/$DNS_NAME.crt"
KEY_FILE="$CERT_DIR/$DNS_NAME.key"
[ "$RENEW" = 1 ] && say "(--renew: forcing a refresh of the cert files)"
if ! "$TS" cert --cert-file "$CERT_FILE" --key-file "$KEY_FILE" "$DNS_NAME"; then
  cat <<MSG
'tailscale cert' failed. The usual cause is that HTTPS certificates are not
enabled for your tailnet: https://login.tailscale.com/admin/dns -> "HTTPS
Certificates" -> Enable. Then re-run this script.
MSG
  exit 2
fi
chmod 600 "$KEY_FILE"
if command -v openssl >/dev/null 2>&1; then
  EXPIRES="$(openssl x509 -in "$CERT_FILE" -noout -enddate 2>/dev/null | sed 's/notAfter=//')"
  say "certificate: $CERT_FILE (expires: ${EXPIRES:-unknown})"
else
  say "certificate: $CERT_FILE"
fi
say "private key: $KEY_FILE"

# ------------------------------------------------------------- 5. update .env
step "Updating $ENV_FILE"
# upsert_env FILE KEY VALUE — replace the first KEY= (or 'export KEY=') line,
# drop any later duplicates, append if absent. Everything else is untouched.
upsert_env() {
  local file="$1" key="$2" val="$3" tmp
  mkdir -p "$(dirname "$file")"
  [ -f "$file" ] || : > "$file"
  tmp="$(mktemp "$file.XXXXXX")"
  K="$key" V="$val" awk '
    BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0 }
    $0 ~ ("^(export[ \t]+)?" k "=") {
      if (!done) { print k "=" v; done = 1 }
      next
    }
    { print }
    END { if (!done) print k "=" v }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}
upsert_env "$ENV_FILE" SSL_CERT "$CERT_FILE"
upsert_env "$ENV_FILE" SSL_KEY  "$KEY_FILE"
say "SSL_CERT=$CERT_FILE"
say "SSL_KEY=$KEY_FILE"

# ------------------------------------------------------------------- 6. done
ENV_PORT="$(grep -E '^(export[[:space:]]+)?PORT=' "$ENV_FILE" 2>/dev/null | tail -n1 | sed -E 's/^(export[[:space:]]+)?PORT=//' || true)"
URL_PORT="${PORT:-${ENV_PORT:-8888}}"

cat <<MSG

Done. Agent Capitol URL (from any device on your tailnet):

    https://$DNS_NAME:$URL_PORT

The server only picks up SSL_CERT/SSL_KEY when it starts, so restart it:

  - launchd service:  scripts/install-service.sh   (re-merges backend/.env)
                      then, for later restarts:
                      launchctl kickstart -k gui/\$(id -u)/$SERVICE_LABEL
  - foreground:       stop it (Ctrl-C) and start it again with
                      SSL_CERT and SSL_KEY set, e.g.
                      set -a; source backend/.env; set +a; ./venv/bin/python backend/server.py

Renewal: the certificate expires in ~90 days. Re-run this script
(optionally with --renew) to refresh it, then restart the server.
MSG
