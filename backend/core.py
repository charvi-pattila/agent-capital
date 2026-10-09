import json
import os
import re
import secrets
import shutil
import sys
import threading
from datetime import timedelta
from pathlib import Path
from flask import Flask
from flask_cors import CORS


app = Flask(__name__)
CORS(app)

# ── Platform ─────────────────────────────────────────────────────────────────
# The backend runs on macOS (the original home) or Linux — in practice Ubuntu
# under WSL2 on a Windows box acting as an always-on server (docs/WINDOWS-SERVER.md).
# Everything that only exists on a Mac (Terminal.app windows, notification
# center, Bonjour names) is gated on IS_MACOS and silently skipped elsewhere.
IS_MACOS = sys.platform == "darwin"


def _detect_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


IS_WSL = (not IS_MACOS) and _detect_wsl()


def _find_bin(env_var, name, *fallbacks):
    """Locate an executable: explicit env override, then PATH, then known
    install locations. Returns the last fallback (unexpanded) if nothing is
    found so error messages name the path that was expected."""
    override = os.environ.get(env_var)
    if override:
        return os.path.expanduser(override)
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        candidate = os.path.expanduser(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return os.path.expanduser(fallbacks[-1]) if fallbacks else name


# CLAUDE_BIN / TMUX_BIN env vars override detection (TMUX itself is reserved:
# tmux sets it inside every session). The native installer puts claude in
# ~/.local/bin on both platforms; tmux is Homebrew on the Mac, apt on Ubuntu.
CLAUDE_BIN = _find_bin("CLAUDE_BIN", "claude", "~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
TMUX = _find_bin("TMUX_BIN", "tmux", "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux")
DATA_DIR = Path(__file__).parent.parent / "data" / "projects"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Auth ─────────────────────────────────────────────────────────────────────
# Single shared password (this is a personal, single-operator dashboard, not a
# multi-user app). No password configured (no .auth_hash yet) => the login gate
# is skipped entirely, so a fresh checkout still runs — see backend/set_password.py.
AUTH_HASH_PATH = DATA_DIR.parent / ".auth_hash"
SECRET_KEY_PATH = DATA_DIR.parent / ".secret_key"
if SECRET_KEY_PATH.exists():
    app.secret_key = SECRET_KEY_PATH.read_text().strip()
else:
    app.secret_key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(app.secret_key)
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Stay logged in for 30 days. Without this the cookie is a browser-session
# cookie, which iOS drops whenever the home-screen app is closed - the phone
# then silently gets 401s on every API call while the cached app shell
# still renders, so it looks like the app "does not open".
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# No SESSION_COOKIE_SECURE: this is served over plain HTTP on the LAN (see
# serving-setup notes) — fine for a trusted home network, not for the open internet.

# Default home for new project codebases. Relative paths (or a blank path)
# entered when creating a project resolve here; absolute or ~-paths override it.
PROJECTS_BASE_DIR = Path.home() / "code" / "my-claude"
PROJECTS_BASE_DIR.mkdir(parents=True, exist_ok=True)



def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def resolve_project_path(raw_path, name):
    raw_path = (raw_path or "").strip()
    if not raw_path:
        base_slug = slugify(name)
        slug = base_slug
        n = 2
        while (PROJECTS_BASE_DIR / slug).exists():
            slug = f"{base_slug}-{n}"
            n += 1
        return str(PROJECTS_BASE_DIR / slug)

    p = Path(os.path.expanduser(raw_path))
    if p.is_absolute():
        return str(p)
    return str(PROJECTS_BASE_DIR / p)

# ── SSE broadcast ─────────────────────────────────────────────────────────────

_sse_listeners: dict = {}  # project_id -> [queue, ...]
_running_listeners: list = []
_council_listeners: dict = {}  # project_id -> [queue, ...]
_split_listeners: dict = {}  # project_id -> [queue, ...]
_lock = threading.Lock()


def broadcast(project_id, data):
    with _lock:
        dead = []
        for q in _sse_listeners.get(project_id, []):
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_listeners[project_id].remove(q)


def broadcast_council(project_id, data):
    with _lock:
        dead = []
        for q in _council_listeners.get(project_id, []):
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            _council_listeners[project_id].remove(q)


def broadcast_split(project_id, data):
    with _lock:
        dead = []
        for q in _split_listeners.get(project_id, []):
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            _split_listeners[project_id].remove(q)


def broadcast_running(data):
    with _lock:
        dead = []
        for q in _running_listeners:
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            _running_listeners.remove(q)


# ── Project storage ───────────────────────────────────────────────────────────

def project_dir(pid):
    return DATA_DIR / pid


def meta_path(pid):
    return project_dir(pid) / "meta.json"


def load_meta(pid):
    p = meta_path(pid)
    if p.exists():
        return json.loads(p.read_text())
    return None


def save_meta(pid, data):
    meta_path(pid).write_text(json.dumps(data, indent=2))


def all_projects():
    projects = []
    for d in sorted(DATA_DIR.iterdir()):
        if d.is_dir() and (d / "meta.json").exists():
            projects.append(json.loads((d / "meta.json").read_text()))
    return projects


def init_project_dir(pid):
    base = project_dir(pid)
    (base / "memory").mkdir(parents=True, exist_ok=True)
    (base / "memory" / "components").mkdir(exist_ok=True)
    (base / "tests").mkdir(exist_ok=True)
    for f in ["last_session.md", "errors.md", "skills.md"]:
        p = base / "memory" / f
        if not p.exists():
            p.write_text("")


_pane_watch = {}  # pid -> {"body": str, "changed_at": float, "pushed": bool}
