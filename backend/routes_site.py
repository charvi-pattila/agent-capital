import json
import os
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from flask import jsonify, request, send_file, session, redirect
from werkzeug.security import check_password_hash
from core import AUTH_HASH_PATH, IS_MACOS, IS_WSL, app
from push import PUSH_AVAILABLE, _load_push_subs, _push_lock, _push_send, _save_push_subs, load_push_settings, push_greeting_name, push_payload, push_public_key, save_push_settings


LOGIN_PAGE = """<!doctype html>
<html><head><title>Agent Capital — Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ background:#0d0d0d; color:#e8e8e8; font-family:-apple-system,BlinkMacSystemFont,sans-serif;
    display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  form {{ background:#1c1c1c; padding:32px; border-radius:12px; border:1px solid #2a2a2a; width:280px; }}
  h1 {{ font-size:16px; margin:0 0 20px; }}
  input {{ width:100%; box-sizing:border-box; padding:10px; border-radius:8px; border:1px solid #333;
    background:#111; color:#e8e8e8; margin-bottom:14px; font-size:14px; }}
  button {{ width:100%; padding:10px; border-radius:8px; border:none; background:#7c5cfc;
    color:#fff; font-weight:600; font-size:14px; cursor:pointer; }}
  .err {{ color:#f87171; font-size:13px; margin-bottom:14px; }}
</style></head>
<body>
  <form method="POST" action="/login">
    <h1>Agent Capital</h1>
    {error}
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Log in</button>
  </form>
</body></html>"""


# ── Phone notification routes ─────────────────────────────────────────────────

@app.route("/api/push/state")
def push_state():
    subs = _load_push_subs()
    return jsonify({
        "available": PUSH_AVAILABLE,
        "public_key": push_public_key(),
        "name": push_greeting_name(),
        "devices": [{"endpoint": s.get("endpoint"), "label": s.get("label", ""), "added": s.get("added")} for s in subs],
    })


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    if not PUSH_AVAILABLE:
        return jsonify({"error": "pywebpush is not installed on the server (pip install -r backend/requirements.txt)"}), 503
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription") or {}
    keys = sub.get("keys") or {}
    if not sub.get("endpoint") or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"error": "Invalid subscription"}), 400
    if "name" in data:
        save_push_settings({**load_push_settings(), "name": (data.get("name") or "").strip()})
    with _push_lock:
        subs = [s for s in _load_push_subs() if s.get("endpoint") != sub["endpoint"]]
        subs.append({
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
            "label": (data.get("label") or "")[:80],
            "added": datetime.now().isoformat(timespec="seconds"),
        })
        _save_push_subs(subs)
    return jsonify({"ok": True, "devices": len(subs), "name": push_greeting_name()})


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    with _push_lock:
        subs = [s for s in _load_push_subs() if s.get("endpoint") != endpoint]
        _save_push_subs(subs)
    return jsonify({"ok": True, "devices": len(subs)})


@app.route("/api/push/settings", methods=["POST"])
def push_settings():
    data = request.get_json(silent=True) or {}
    save_push_settings({**load_push_settings(), "name": (data.get("name") or "").strip()})
    return jsonify({"ok": True, "name": push_greeting_name()})


@app.route("/api/push/test", methods=["POST"])
def push_test():
    """Synchronous so the modal can show why a delivery failed."""
    if not PUSH_AVAILABLE:
        return jsonify({"error": "pywebpush is not installed on the server"}), 503
    data = request.get_json(silent=True) or {}
    payload = push_payload("A test project", "this is what an alert looks like. Tap to open the dashboard.")
    payload["url"] = "/running"
    sent, errors = _push_send(payload, only_endpoint=data.get("endpoint"))
    if sent == 0:
        return jsonify({"error": errors[0] if errors else "No device is subscribed"}), 400
    return jsonify({"ok": True, "sent": sent, "errors": errors})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        stored = AUTH_HASH_PATH.read_text().strip() if AUTH_HASH_PATH.exists() else None
        if stored and check_password_hash(stored, request.form.get("password", "")):
            session["authed"] = True
            session.permanent = True
            return redirect("/")
        return LOGIN_PAGE.format(error='<div class="err">Wrong password.</div>'), 401
    return LOGIN_PAGE.format(error="")


@app.route("/logout")
def logout():
    session.pop("authed", None)
    return redirect("/login")


_PUBLIC_FILES = frozenset({"/sw.js", "/registerSW.js", "/manifest.webmanifest", "/favicon.png",
                           "/apple-touch-icon.png", "/vite.svg"})


@app.before_request
def require_login():
    # AUTH_HASH_PATH is checked fresh on every request (not cached at startup), so
    # running set_password.py turns the gate on immediately — no backend restart needed.
    if not AUTH_HASH_PATH.exists():
        return
    if request.path in ("/login", "/logout") or session.get("authed"):
        return
    # The built frontend's static files are public code, not data. Gating them
    # broke the installed PWA: with the cookie gone, the service worker's asset
    # fetches were redirected to the login *HTML* and cached as if they were
    # the JS/CSS, leaving a blank app even after logging back in.
    if request.path.startswith(("/assets/", "/icons/")) or request.path in _PUBLIC_FILES:
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


# ── Frontend (built app) ──────────────────────────────────────────────────────
# Serves frontend/dist so the whole app lives on one port (8888) and is
# reachable from the phone. Rebuild with `npm run build` after frontend changes.

FRONTEND_DIST = (Path(__file__).parent.parent / "frontend" / "dist").resolve()


def _run_quiet(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _tailscale_url():
    """https://<machine>.<tailnet>.ts.net if this machine is on a tailnet and
    the CLI is reachable. Under WSL the Windows tailscale.exe is used (WSL runs
    Windows executables directly), since Tailscale is installed on the Windows
    side there, and its `tailscale serve` is what fronts this backend."""
    candidates = [
        shutil.which("tailscale"),
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/mnt/c/Program Files/Tailscale/tailscale.exe",
    ]
    for bin_ in candidates:
        if not bin_ or not os.path.isfile(bin_):
            continue
        out = _run_quiet([bin_, "status", "--json"], timeout=5)
        if not out:
            continue
        try:
            dns_name = (json.loads(out).get("Self") or {}).get("DNSName") or ""
        except ValueError:
            continue
        dns_name = dns_name.rstrip(".")
        if dns_name:
            return f"https://{dns_name}"
    return ""


@app.route("/api/hostname")
def hostname_info():
    """Addresses for the 'Install as an app' card in the About tab.

    public_url   — the one to bookmark from anywhere: PUBLIC_URL from the
                   environment (backend/.env) if set, else the Tailscale
                   MagicDNS https URL if this machine is on a tailnet.
    local_hostname — the LAN name with port. On a Mac that's the .local
                   (Bonjour) name, which survives DHCP renumbering. On Linux it
                   is the plain hostname (Bonjour needs avahi, which WSL lacks),
                   so it's only useful with a public_url in front of it.
    lan_ip       — hint only; goes stale."""
    port = int(os.environ.get("PORT", "8888"))
    if IS_MACOS:
        name = _run_quiet(["scutil", "--get", "LocalHostName"]) or socket.gethostname().split(".")[0]
        local_hostname = f"{name}.local"
        lan_ip = _run_quiet(["ipconfig", "getifaddr", "en0"]) or None
    else:
        local_hostname = socket.gethostname().split(".")[0]
        lan_ip = (_run_quiet(["hostname", "-I"]).split() or [None])[0]
    public_url = (os.environ.get("PUBLIC_URL") or "").strip().rstrip("/") or _tailscale_url() or None
    return jsonify({
        "public_url": public_url,
        "local_hostname": local_hostname,
        "port": port,
        "lan_ip": lan_ip,
        "platform": "macos" if IS_MACOS else ("wsl" if IS_WSL else "linux"),
    })


@app.route("/")
@app.route("/<path:path>")
def serve_frontend(path=""):
    if path.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    if path:
        target = (FRONTEND_DIST / path).resolve()
        if target.is_file() and (target == FRONTEND_DIST or FRONTEND_DIST in target.parents):
            return send_file(target)
    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        return "Frontend not built — run `npm run build` in frontend/", 503
    return send_file(index)
