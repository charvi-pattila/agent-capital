import json
import os
import shutil
import subprocess
import threading
from datetime import datetime
from core import DATA_DIR, IS_MACOS, _lock, _pane_watch
from tmuxlib import capture_pane, pane_body


def notify_macos(title, message):
    """Desktop notification. macOS: Notification Center via osascript. Linux:
    there is usually no desktop (WSL server), so it goes to the log instead —
    `journalctl -u agent-capital` shows them — unless notify-send exists."""
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    clean = " ".join((message or "").split())[:200]
    if IS_MACOS:
        script = f'display notification "{esc(clean)}" with title "{esc(title)}"'
        subprocess.run(["osascript", "-e", script])
        return
    print(f"[notify] {title}: {clean}", flush=True)
    if shutil.which("notify-send") and os.environ.get("DISPLAY"):
        subprocess.run(["notify-send", title, clean], capture_output=True)


# ── Phone notifications (Web Push) ───────────────────────────────────────────
# notify_macos() only reaches the machine the backend runs on — which is now a
# headless Windows/WSL box, so nothing reached the phone. Web Push goes through
# the browser's own push service (Apple's for the iPhone home-screen app,
# Google's for Chrome) to every device that turned alerts on in the dashboard
# (bell button, AlertsModal.jsx). The message is always the same shape:
# "Hey <name>, Agent Capital" / "<project> needs your response: <detail>".
#
# iOS only allows push for an installed (Add to Home Screen) web app, and only
# from a user gesture — the modal handles both. Keys + subscriptions live in
# data/push/ (git-ignored): the VAPID key pair identifies this server to the
# push services and is generated on first use; losing it invalidates every
# subscription, so back it up along with data/ when moving hosts.
try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid, b64urlencode
    from cryptography.hazmat.primitives import serialization
    PUSH_AVAILABLE = True
except ImportError:  # pip install -r requirements.txt (pywebpush)
    PUSH_AVAILABLE = False

PUSH_DIR = DATA_DIR.parent / "push"
VAPID_KEY_PATH = PUSH_DIR / "vapid_private.pem"
PUSH_SUBS_PATH = PUSH_DIR / "subscriptions.json"
PUSH_SETTINGS_PATH = PUSH_DIR / "settings.json"
# VAPID "sub" claim — a contact address the push services may use about abuse.
PUSH_CONTACT = os.environ.get("PUSH_CONTACT", "mailto:agent-capital@localhost")
_push_lock = threading.Lock()
_vapid = None


def _vapid_keys():
    global _vapid
    if not PUSH_AVAILABLE:
        return None
    with _push_lock:
        if _vapid is None:
            PUSH_DIR.mkdir(parents=True, exist_ok=True)
            if VAPID_KEY_PATH.exists():
                _vapid = Vapid.from_file(str(VAPID_KEY_PATH))
            else:
                _vapid = Vapid()
                _vapid.generate_keys()
                _vapid.save_key(str(VAPID_KEY_PATH))
        return _vapid


def push_public_key():
    """The applicationServerKey the browser needs to subscribe (base64url,
    uncompressed P-256 point)."""
    v = _vapid_keys()
    if v is None:
        return None
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


def _load_push_subs():
    try:
        subs = json.loads(PUSH_SUBS_PATH.read_text())
        return subs if isinstance(subs, list) else []
    except (OSError, ValueError):
        return []


def _save_push_subs(subs):
    PUSH_DIR.mkdir(parents=True, exist_ok=True)
    PUSH_SUBS_PATH.write_text(json.dumps(subs, indent=2))


def load_push_settings():
    try:
        return json.loads(PUSH_SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_push_settings(settings):
    PUSH_DIR.mkdir(parents=True, exist_ok=True)
    PUSH_SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def push_greeting_name():
    """Who the notification greets: set in the alerts modal, or NOTIFY_NAME in
    backend/.env as a fallback."""
    return (load_push_settings().get("name") or os.environ.get("NOTIFY_NAME") or "").strip()


def push_payload(project_name, detail, pid=None):
    name = push_greeting_name()
    body = f"{project_name} needs your response"
    clean = " ".join((detail or "").split())[:160]
    if clean:
        body += f": {clean}"
    return {
        "title": f"Hey {name}, Agent Capital" if name else "Hey, Agent Capital",
        "body": body,
        "tag": f"project-{pid}" if pid else "agent-capital",
        "url": f"/project/{pid}" if pid else "/running",
    }


def _push_send(payload, only_endpoint=None):
    """Deliver one payload to every subscription (or just one). Returns
    (sent, errors). Subscriptions the push service reports gone (404/410 —
    the user revoked permission or deleted the app) are dropped."""
    subs = _load_push_subs()
    if only_endpoint:
        subs = [s for s in subs if s.get("endpoint") == only_endpoint]
    sent, errors, dead = 0, [], []
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=json.dumps(payload),
                vapid_private_key=_vapid_keys(),
                vapid_claims={"sub": PUSH_CONTACT},  # fresh dict: pywebpush adds aud/exp to it
                ttl=3600,
                headers={"Urgency": "high"},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                dead.append(sub["endpoint"])
            errors.append(f"{status or 'error'}: {str(e)[:200]}")
            print(f"[push] failed ({status}) for {sub.get('label') or sub['endpoint'][:40]}: {e}", flush=True)
        except Exception as e:  # DNS down, timeout, bad key…
            errors.append(str(e)[:200])
            print(f"[push] error: {e}", flush=True)
    if dead:
        with _push_lock:
            _save_push_subs([s for s in _load_push_subs() if s.get("endpoint") not in dead])
    return sent, errors


def notify_phone(project_name, detail, pid=None):
    """Push "<project> needs your response" to every subscribed device.
    Fire-and-forget: push services can take seconds and this is called from
    the watchdog thread and reply watchers."""
    if not PUSH_AVAILABLE or not _load_push_subs():
        return
    payload = push_payload(project_name, detail, pid)
    threading.Thread(target=_push_send, args=(payload,), daemon=True).start()


def notify_needs_response(pid, project_name, detail):
    """Desktop + phone, and remember that this pane state has been announced so
    the blocked watchdog doesn't repeat it 90s later (see _blocked_tick)."""
    notify_macos(project_name, detail)
    notify_phone(project_name, detail, pid)
    body = pane_body(capture_pane(pid))
    with _lock:
        watch = _pane_watch.get(pid)
        if watch and watch["body"] == body:
            watch["pushed"] = True
        else:
            _pane_watch[pid] = {"body": body, "changed_at": datetime.now().timestamp(), "pushed": True}
