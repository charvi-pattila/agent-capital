import atexit
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, Response, send_file, session, redirect
from flask_cors import CORS
from werkzeug.security import check_password_hash

app = Flask(__name__)
CORS(app)

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
TMUX = "/opt/homebrew/bin/tmux"
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
# No SESSION_COOKIE_SECURE: this is served over plain HTTP on the LAN (see
# serving-setup notes) — fine for a trusted home network, not for the open internet.

# Default home for new project codebases. Relative paths (or a blank path)
# entered when creating a project resolve here; absolute or ~-paths override it.
PROJECTS_BASE_DIR = Path.home() / "code" / "my-claude"
PROJECTS_BASE_DIR.mkdir(parents=True, exist_ok=True)

# iMessage/SMS target for limit-resume alerts (E.164). Override with NOTIFY_PHONE env var.
NOTIFY_PHONE = os.environ.get("NOTIFY_PHONE", "+15555550100")


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


# ── tmux helpers ──────────────────────────────────────────────────────────────

def session_name(pid):
    return f"claude_{pid}"


def server_session_name(pid):
    return f"claude_server_{pid}"


def _tmux_has_session(name):
    r = subprocess.run([TMUX, "has-session", "-t", name], capture_output=True)
    return r.returncode == 0


def session_running(pid):
    return _tmux_has_session(session_name(pid))


def dev_server_running(pid):
    return _tmux_has_session(server_session_name(pid))


def start_dev_server(pid, project_path, cmd):
    """Run the project's own dev/app server (e.g. `npm run dev`) in its own tmux
    session, detached from the Claude session, so the Preview tab's URL is already
    live by the time a Start finishes. No-ops if no command is configured or one's
    already running."""
    cmd = (cmd or "").strip()
    if not cmd:
        return False
    session = server_session_name(pid)
    if _tmux_has_session(session):
        return False
    cwd = os.path.expanduser(project_path) if project_path else os.path.expanduser("~")
    subprocess.run([TMUX, "new-session", "-d", "-s", session, "-c", cwd, cmd])
    return True


def stop_dev_server(pid):
    if _tmux_has_session(server_session_name(pid)):
        subprocess.run([TMUX, "kill-session", "-t", server_session_name(pid)])


def capture_pane(pid):
    r = subprocess.run(
        [TMUX, "capture-pane", "-t", session_name(pid), "-p", "-S", "-500"],
        capture_output=True, text=True
    )
    ansi = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi.sub('', r.stdout)


def start_session(pid, project_path=None):
    session = session_name(pid)
    if session_running(pid):
        return False

    cwd = os.path.expanduser(project_path) if project_path else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        os.makedirs(cwd, exist_ok=True)
    cmd = f"cd {cwd} && {CLAUDE_BIN} --dangerously-skip-permissions"
    # `-d` detaches the pane's own command immediately, so this still returns as soon as
    # the session exists — it must be a blocking run(), not Popen(). Popen() only forks
    # tmux's client and returns before the client has even connected, so on a cold tmux
    # server (no session ever started before) the set-option/resize-window calls below
    # would race the server's startup and fail silently against a socket that doesn't
    # exist yet, leaving the window at whatever a later, smaller client resizes it to.
    subprocess.run([TMUX, "new-session", "-d", "-s", session, "-x", "120", "-y", "200", cmd])
    # Claude Code's TUI sizes its rendered history to the pane it's given, so a tall
    # pane means more scrollback fits in one capture — that's what lets the web mirror
    # scroll natively instead of relying on PageUp for everything. "manual" keeps this
    # size fixed even when the native Terminal.app window (a smaller client) attaches.
    subprocess.run([TMUX, "set-option", "-t", session, "-w", "window-size", "manual"])
    subprocess.run([TMUX, "resize-window", "-t", session, "-x", "120", "-y", "200"])
    threading.Event().wait(2)
    return True


def open_terminal_window(pid):
    session = session_name(pid)
    script = (
        'tell application "Terminal"\n'
        f'\tdo script "tmux attach -t {session}"\n'
        "\tactivate\n"
        "\treturn id of front window\n"
        "end tell"
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    win_id = result.stdout.strip()
    if win_id:
        Path(f"/tmp/phone_terminal_{pid}").write_text(win_id)


def close_terminal_window(pid):
    marker = Path(f"/tmp/phone_terminal_{pid}")
    if not marker.exists():
        return
    win_id = marker.read_text().strip()
    marker.unlink()
    if not win_id:
        return
    if subprocess.run(["pgrep", "-x", "Terminal"], capture_output=True).returncode != 0:
        return
    script = (
        'tell application "Terminal"\n'
        f"\tif (exists window id {win_id}) then close window id {win_id}\n"
        "end tell"
    )
    subprocess.run(["osascript", "-e", script])


def kill_session(pid):
    if session_running(pid):
        subprocess.run([TMUX, "kill-session", "-t", session_name(pid)])
    stop_dev_server(pid)
    close_terminal_window(pid)
    ready = Path(f"/tmp/phone_ready_{pid}")
    if ready.exists():
        ready.unlink()


def pane_body(text):
    lines = text.splitlines()
    return "\n".join(lines[:-3]) if len(lines) > 3 else text


def extract_reply(content, message):
    lines = content.splitlines()
    msg_short = message[:40].lower()
    msg_idx = -1
    for i, line in enumerate(lines):
        if msg_short in line.lower():
            msg_idx = i

    if msg_idx == -1:
        return ""

    response_lines = []
    for line in lines[msg_idx + 1:]:
        stripped = line.strip()
        if stripped in ("❯", ">", "❯ "):
            break
        if set(stripped) <= set("─—-="):
            continue
        cleaned = re.sub(r'^[⏺⎿⏵\s]+', '', stripped).strip()
        if cleaned:
            response_lines.append(cleaned)

    text = "\n".join(response_lines).strip()
    return re.sub(r'\n{3,}', '\n\n', text)


# ── Chat helpers ──────────────────────────────────────────────────────────────

def messages_path(pid):
    return project_dir(pid) / "messages.json"


def load_messages(pid):
    p = messages_path(pid)
    return json.loads(p.read_text()) if p.exists() else []


def save_messages(pid, msgs):
    messages_path(pid).write_text(json.dumps(msgs, indent=2))


def append_message(pid, msg):
    msgs = load_messages(pid)
    msgs.append(msg)
    save_messages(pid, msgs)
    broadcast(pid, msg)
    return msg


def update_message(pid, msg_id, updates):
    msgs = load_messages(pid)
    for m in msgs:
        if m["id"] == msg_id:
            m.update(updates)
    save_messages(pid, msgs)
    updated = next((m for m in msgs if m["id"] == msg_id), None)
    if updated:
        broadcast(pid, updated)
    return updated


def mark_unread(pid):
    meta = load_meta(pid)
    if meta:
        meta["unread"] = meta.get("unread", 0) + 1
        save_meta(pid, meta)


def clear_unread(pid):
    meta = load_meta(pid)
    if meta and meta.get("unread"):
        meta["unread"] = 0
        save_meta(pid, meta)


def notify_macos(title, message):
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    clean = " ".join((message or "").split())[:200]
    script = f'display notification "{esc(clean)}" with title "{esc(title)}"'
    subprocess.run(["osascript", "-e", script])


def wait_for_stable_reply(pid, message):
    prev = ""
    stable = 0
    current = ""
    for _ in range(240):
        threading.Event().wait(0.5)
        current = capture_pane(pid)
        body = pane_body(current)
        if body == prev:
            stable += 1
            if stable >= 4:
                break
        else:
            stable = 0
        prev = body

    reply = extract_reply(current, message)
    return reply or "(Response visible in terminal)"


def send_to_session(pid, message, reply_id, project_name, notify=True):
    if not session_running(pid):
        update_message(pid, reply_id, {"text": "Session not running. Start it first.", "status": "done"})
        return

    session = session_name(pid)
    subprocess.run([TMUX, "set-buffer", "-t", session, message])
    subprocess.run([TMUX, "paste-buffer", "-t", session])
    threading.Event().wait(0.3)
    subprocess.run([TMUX, "send-keys", "-t", session, "", "Enter"])
    threading.Event().wait(2)

    text = wait_for_stable_reply(pid, message)
    update_message(pid, reply_id, {"text": text, "status": "done"})
    if notify:
        notify_macos(project_name, text)
        mark_unread(pid)

    meta = load_meta(pid)
    if meta:
        meta["last_activity"] = datetime.now().isoformat()
        save_meta(pid, meta)


# ── Usage-limit watchdog ──────────────────────────────────────────────────────
# Two-stage: on Claude Code's "Approaching usage limit" warning (~95%), interrupt
# the session and spend the remaining budget on a proper handoff summary. If the
# hard "limit reached" lands anyway, snapshot the terminal instead (a limited
# session can't answer prompts). Either way the session is kept alive and nudged
# with "continue" only after the reset time, then the user gets a text.

LIMIT_CHECK_INTERVAL = 20  # seconds
LIMIT_RESUME_GRACE = 300   # ignore lingering limit text this long after a resume

# Both require a concrete reset time nearby ("resets at 4pm", "resets Jul 17"),
# so a session merely *talking about* limits doesn't trigger them.
_RESET_NEARBY = (
    r"resets?\b[^\n]{0,16}?"
    r"(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm)\b|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2})"
)
LIMIT_RE = re.compile(r"(?:limit reached|hit your [^\n]{0,40}limit)[\s\S]{0,200}?" + _RESET_NEARBY, re.I)
APPROACHING_RE = re.compile(r"approaching[^\n]{0,40}limit[^\n]{0,80}?" + _RESET_NEARBY, re.I)

RESET_AT_RE = re.compile(
    r"resets?(?:\s+at)?\s+"
    r"(?:(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(?P<day>\d{1,2})(?:\s+at)?[\s,]+)?"
    r"(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ap>am|pm)",
    re.I,
)

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_reset_time(text, now=None):
    """Best-effort parse of 'resets at 3pm' / 'resets Jul 17 at 10:30am'. None if absent."""
    matches = list(RESET_AT_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    now = now or datetime.now()
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("min") or 0)
    if m.group("mon"):
        t = now.replace(month=MONTHS[m.group("mon").lower()], day=int(m.group("day")),
                        hour=hour, minute=minute, second=0, microsecond=0)
        if t < now - timedelta(days=1):  # month rolled into next year
            t = t.replace(year=t.year + 1)
    else:
        t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
    return t


def _as_string(s):
    """Escape a Python string into an AppleScript string expression."""
    parts = s.replace("\\", "\\\\").replace('"', '\\"').split("\n")
    return '"' + '" & return & "'.join(parts) + '"'


def send_text(body, to=None):
    """Send an iMessage via Messages.app. Returns True if accepted for delivery."""
    to = to or NOTIFY_PHONE
    if not to:
        return False
    script = (
        'tell application "Messages"\n'
        '\tset targetService to 1st account whose service type = iMessage\n'
        f'\tset targetBuddy to participant "{to}" of targetService\n'
        f'\tsend {_as_string(body[:600])} to targetBuddy\n'
        "end tell"
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        masked = to[:3] + "***" + to[-4:]
        print(f"[limit-monitor] text to {masked} failed: {r.stderr.strip()}")
    return r.returncode == 0


def capture_visible(pid):
    """Visible pane only (no scrollback) — old limit messages scroll out of scope."""
    r = subprocess.run(
        [TMUX, "capture-pane", "-t", session_name(pid), "-p"],
        capture_output=True, text=True
    )
    ansi = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi.sub('', r.stdout)


def _save_terminal_tail(pid, reset_at):
    tail = "\n".join(capture_pane(pid).splitlines()[-60:]).strip()
    set_memory(pid, "last_session", (
        f"(Auto-saved {datetime.now():%b %d %I:%M %p} — Claude usage limit; "
        f"session paused, auto-resume at {reset_at:%b %d %I:%M %p}.)\n\n"
        f"Terminal output just before the limit:\n```\n{tail}\n```"
    ))


def _save_session_summary(pid, notify=True, prompt=None):
    """Ask the live session for a handoff summary and store it in last_session
    memory. Returns False if no usable reply came back (e.g. already limited)."""
    if not session_running(pid):
        return False
    prompt = prompt or ("Save a brief summary of what we accomplished this session "
                        "and what to do next time. Plain text, max 200 words.")
    name = (load_meta(pid) or {}).get("name", pid)
    msgs = load_messages(pid)
    next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
    reply_id = next_id + 1
    append_message(pid, {"id": next_id, "role": "user", "text": prompt, "status": "done", "timestamp": datetime.now().isoformat()})
    append_message(pid, {"id": reply_id, "role": "assistant", "text": "Saving summary...", "status": "thinking", "timestamp": datetime.now().isoformat()})

    send_to_session(pid, prompt, reply_id, name, notify=notify)

    msgs = load_messages(pid)
    last = next((m for m in msgs if m.get("id") == reply_id), None)
    text = (last or {}).get("text", "")
    if text and text != "(Response visible in terminal)":
        set_memory(pid, "last_session", text)
        return True
    return False


def _handle_limit_hit(pid, meta, reset_at):
    """Hard limit already hit — the session can't answer prompts, so snapshot
    the terminal instead of asking for a summary. Nothing is typed into the
    session until after the reset."""
    name = meta.get("name", pid)
    meta["limit_paused"] = True
    meta["limit_resume_at"] = reset_at.isoformat()
    save_meta(pid, meta)
    _save_terminal_tail(pid, reset_at)
    notify_macos(name, f"Usage limit hit — progress saved, auto-resuming at {reset_at:%-I:%M %p}")
    print(f"[limit-monitor] {name}: limit hit, resume at {reset_at}")


def _handle_limit_approaching(pid, meta, reset_at):
    """~95% of the limit — interrupt the current task now and spend the last
    bit of budget on a proper handoff summary while Claude can still answer."""
    name = meta.get("name", pid)
    meta["limit_paused"] = True
    meta["limit_resume_at"] = reset_at.isoformat()
    save_meta(pid, meta)

    def _stop():
        subprocess.run([TMUX, "send-keys", "-t", session_name(pid), "Escape"])
        threading.Event().wait(1.5)
        ok = _save_session_summary(pid, notify=False, prompt=(
            "We're close to the usage limit, so we're pausing here. Stop what you're doing "
            "and save a brief summary of what we accomplished and exactly where to pick up "
            "next time. Plain text, max 200 words."))
        if not ok:
            _save_terminal_tail(pid, reset_at)
        notify_macos(name, f"Approaching usage limit — paused early, auto-resuming at {reset_at:%-I:%M %p}")
        print(f"[limit-monitor] {name}: approaching limit, paused early; resume at {reset_at}")

    threading.Thread(target=_stop, daemon=True).start()


def _resume_project(pid, meta):
    name = meta.get("name", pid)
    if session_running(pid):
        # session survived — its full context is intact, just nudge it onward
        session = session_name(pid)
        msg = "The usage limit has reset. Continue where you left off."
        subprocess.run([TMUX, "set-buffer", "-t", session, msg])
        subprocess.run([TMUX, "paste-buffer", "-t", session])
        threading.Event().wait(0.3)
        subprocess.run([TMUX, "send-keys", "-t", session, "", "Enter"])
    else:
        if start_session(pid, meta.get("path")):
            open_terminal_window(pid)
        start_dev_server(pid, meta.get("path"), meta.get("server_cmd"))
        threading.Event().wait(3)
        context = build_context_prompt(pid)
        prompt = (context + "\n\n" if context else "") + \
            "We were interrupted by a usage limit. Continue where we left off."
        _do_send_message(pid, prompt, notify=False)
    notify_macos(name, "Usage limit reset — resuming work")


def _limit_tick():
    now = datetime.now()
    resumed = []
    for meta in all_projects():
        pid = meta["id"]

        if meta.get("limit_paused"):
            try:
                resume_at = datetime.fromisoformat(meta["limit_resume_at"])
            except (KeyError, ValueError):
                resume_at = now
            if now >= resume_at:
                _resume_project(pid, meta)
                meta["limit_paused"] = False
                meta["limit_resumed_at"] = now.isoformat()
                save_meta(pid, meta)
                resumed.append(meta.get("name", pid))
            continue

        if not session_running(pid):
            continue
        if meta.get("limit_resumed_at"):
            try:
                if (now - datetime.fromisoformat(meta["limit_resumed_at"])).total_seconds() < LIMIT_RESUME_GRACE:
                    continue
            except ValueError:
                pass
        pane = capture_visible(pid)
        if not pane:
            continue
        if LIMIT_RE.search(pane):
            reset_at = parse_reset_time(pane, now) or (now + timedelta(hours=5))
            _handle_limit_hit(pid, meta, reset_at)
        elif APPROACHING_RE.search(pane):
            reset_at = parse_reset_time(pane, now) or (now + timedelta(hours=5))
            _handle_limit_approaching(pid, meta, reset_at)

    if resumed:
        send_text(
            f"Claude limit reset — resumed: {', '.join(resumed)}. "
            "Picking up where we left off."
        )


def limit_monitor():
    while True:
        try:
            _limit_tick()
        except Exception as e:
            print(f"[limit-monitor] error: {e}")
        threading.Event().wait(LIMIT_CHECK_INTERVAL)


# ── Blocked-session watchdog ─────────────────────────────────────────────────
# Flags a running session as "blocked" in the Running tab once its pane has sat
# unchanged past IDLE_THRESHOLD — the same stable-pane diffing technique
# wait_for_stable_reply() already uses right after sending a message, just run
# continuously in the background instead of once. Purely live/derived state
# (never written to meta.json — it's a fact about the tmux pane, not the project).

BLOCKED_CHECK_INTERVAL = 15  # seconds
IDLE_THRESHOLD = 90          # seconds unchanged before flagging as blocked

# Common Claude Code CLI confirmation/choice prompts — matching one gives a more
# specific reason than the generic idle fallback.
BLOCKED_PROMPT_RE = re.compile(
    r"do you want to (?:proceed|continue)|\(y/n\)|❯\s*1\.|press enter to continue",
    re.I,
)

_pane_watch = {}  # pid -> {"body": str, "changed_at": float}


def _blocked_tick():
    now = datetime.now().timestamp()
    seen = set()
    for meta in all_projects():
        pid = meta["id"]
        if not session_running(pid):
            continue
        seen.add(pid)
        body = pane_body(capture_pane(pid))
        with _lock:
            prev = _pane_watch.get(pid)
            if prev is None or prev["body"] != body:
                _pane_watch[pid] = {"body": body, "changed_at": now}
            # else: unchanged — leave changed_at as-is, it keeps aging

    with _lock:
        for pid in list(_pane_watch):
            if pid not in seen:
                del _pane_watch[pid]  # session no longer running — clear stale state


def blocked_status(pid):
    """Returns (status, blocked_reason) for a running session, or (None, None)."""
    with _lock:
        watch = _pane_watch.get(pid)
    if not watch:
        return None, None
    idle_for = datetime.now().timestamp() - watch["changed_at"]
    if idle_for < IDLE_THRESHOLD:
        return None, None
    m = BLOCKED_PROMPT_RE.search(watch["body"])
    if m:
        line = next((l.strip() for l in watch["body"].splitlines() if m.group(0).lower() in l.lower()), m.group(0))
        return "blocked", f"Waiting on a prompt: {line[:120]}"
    return "blocked", "Idle — may be waiting for input"


def blocked_monitor():
    while True:
        try:
            _blocked_tick()
        except Exception as e:
            print(f"[blocked-monitor] error: {e}")
        threading.Event().wait(BLOCKED_CHECK_INTERVAL)


# ── Memory helpers ────────────────────────────────────────────────────────────

MEMORY_FILES = {
    "last_session": "memory/last_session.md",
    "errors": "memory/errors.md",
    "skills": "memory/skills.md",
}

# Global memory: one free-form note shared across every project (unlike per-project
# memory, "shared across all projects" doesn't need the last_session/errors/skills
# taxonomy — it's a single running note, e.g. "always use tabs not spaces").
GLOBAL_MEMORY_DIR = DATA_DIR.parent / "global_memory"
GLOBAL_MEMORY_PATH = GLOBAL_MEMORY_DIR / "notes.md"


def get_global_memory():
    GLOBAL_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return GLOBAL_MEMORY_PATH.read_text() if GLOBAL_MEMORY_PATH.exists() else ""


def set_global_memory(content):
    GLOBAL_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    GLOBAL_MEMORY_PATH.write_text(content)


def get_memory(pid):
    base = project_dir(pid)
    result = {}
    for key, rel in MEMORY_FILES.items():
        p = base / rel
        result[key] = p.read_text() if p.exists() else ""

    comp_dir = base / "memory" / "components"
    components = []
    if comp_dir.exists():
        for f in sorted(comp_dir.iterdir()):
            if f.suffix == ".md":
                components.append({"name": f.stem, "content": f.read_text()})

    return {"memory": result, "components": components}


def set_memory(pid, memory_type, content, component=None):
    base = project_dir(pid)
    if component:
        p = base / "memory" / "components" / f"{component}.md"
    elif memory_type in MEMORY_FILES:
        p = base / MEMORY_FILES[memory_type]
    else:
        return False
    p.write_text(content)
    return True


def build_context_prompt(pid):
    mem = get_memory(pid)
    lines = ["# Project Context - Read this to know where we left off\n"]

    global_note = get_global_memory()
    if global_note:
        lines.append(f"## Global Memory (shared across all projects)\n{global_note}\n")
    if mem["memory"]["last_session"]:
        lines.append(f"## Last Session\n{mem['memory']['last_session']}\n")
    if mem["memory"]["errors"]:
        lines.append(f"## Known Errors & Fixes\n{mem['memory']['errors']}\n")
    if mem["memory"]["skills"]:
        lines.append(f"## Skills & Patterns\n{mem['memory']['skills']}\n")
    for c in mem["components"]:
        if c["content"]:
            lines.append(f"## Component: {c['name']}\n{c['content']}\n")

    if len(lines) == 1:
        return None
    return "\n".join(lines)


# ── Running sessions ──────────────────────────────────────────────────────────

def get_running_sessions():
    result = []
    for p in all_projects():
        if session_running(p["id"]):
            status, blocked_reason = blocked_status(p["id"])
            entry = {**p, "status": status or "running"}
            if blocked_reason:
                entry["blocked_reason"] = blocked_reason
            result.append(entry)
    return result


# ── Tests ─────────────────────────────────────────────────────────────────────

def tests_path(pid):
    return project_dir(pid) / "tests" / "tests.json"


def load_tests(pid):
    p = tests_path(pid)
    return json.loads(p.read_text()) if p.exists() else []


def save_tests(pid, tests):
    tests_path(pid).parent.mkdir(parents=True, exist_ok=True)
    tests_path(pid).write_text(json.dumps(tests, indent=2))


# ── Screenshot comparison tests ──────────────────────────────────────────────
# Shells out to the Chrome already on the Mac (no Playwright/Selenium install) and
# diffs pixels with Pillow. First run on a test just records the baseline; every
# run after that compares against it and fails once the differing-pixel share
# crosses SCREENSHOT_DIFF_THRESHOLD.

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCREENSHOT_DIFF_THRESHOLD = 1.0  # percent of pixels allowed to differ before a fail


def screenshot_test_dir(pid, test_id):
    d = project_dir(pid) / "tests" / test_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture_screenshot(url, out_path):
    try:
        subprocess.run(
            [CHROME_BIN, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--window-size=1280,800", f"--screenshot={out_path}", url],
            capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return Path(out_path).exists()


def diff_screenshots(baseline_path, latest_path, diff_path):
    """Saves a diff image and returns the % of pixels that differ."""
    from PIL import Image, ImageChops
    a = Image.open(baseline_path).convert("RGB")
    b = Image.open(latest_path).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    diff.save(diff_path)
    # ignore very small per-channel deltas (anti-aliasing/font-rendering noise)
    mask = diff.convert("L").point(lambda p: 255 if p > 8 else 0)
    changed = mask.histogram()[255]
    total = a.size[0] * a.size[1]
    return round(100 * changed / total, 2) if total else 0.0


def run_screenshot_test(pid, test):
    if not test.get("target_url"):
        test["status"] = "failing"
        test["error"] = "No target URL set"
        return
    d = screenshot_test_dir(pid, test["id"])
    baseline, latest, diff = d / "baseline.png", d / "latest.png", d / "diff.png"
    if not capture_screenshot(test["target_url"], latest):
        test["status"] = "failing"
        test["error"] = "Could not capture screenshot (is the URL reachable?)"
        return
    if not baseline.exists():
        baseline.write_bytes(latest.read_bytes())
        test["status"] = "baseline_saved"
        test["error"] = ""
        test["diff_percent"] = 0.0
        return
    percent = diff_screenshots(baseline, latest, diff)
    test["diff_percent"] = percent
    test["status"] = "passing" if percent < SCREENSHOT_DIFF_THRESHOLD else "failing"
    test["error"] = "" if test["status"] == "passing" else f"{percent}% of pixels differ from baseline"


def run_tests_for_project(pid):
    tests = load_tests(pid)
    if not tests:
        return
    name = (load_meta(pid) or {}).get("name", pid)

    for i, test in enumerate(tests):
        tests[i]["status"] = "running"
        tests[i]["last_run"] = datetime.now().isoformat()
    save_tests(pid, tests)

    for i, test in enumerate(tests):
        if test.get("type") == "screenshot":
            run_screenshot_test(pid, tests[i])
            continue

        if not session_running(pid):
            tests[i]["status"] = "failing"
            tests[i]["error"] = "Session not running"
            continue

        prompt = f"Run this test and tell me if it passes. Reply with PASS or FAIL followed by a brief reason.\nTest: {test['description']}"
        if test.get("target_url"):
            prompt += f"\nURL to check: {test['target_url']}"

        msgs = load_messages(pid)
        next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
        reply_id = next_id + 1
        append_message(pid, {"id": next_id, "role": "user", "text": f"[Auto-test] {test['description']}", "status": "done", "timestamp": datetime.now().isoformat()})
        append_message(pid, {"id": reply_id, "role": "assistant", "text": "Running test...", "status": "thinking", "timestamp": datetime.now().isoformat()})

        send_to_session(pid, prompt, reply_id, name)
        msgs = load_messages(pid)
        reply = next((m for m in msgs if m["id"] == reply_id), None)
        reply_text = (reply or {}).get("text", "")

        tests[i]["status"] = "passing" if reply_text.strip().upper().startswith("PASS") else "failing"
        tests[i]["error"] = reply_text if tests[i]["status"] == "failing" else ""

    save_tests(pid, tests)


# ── Council: 5 independent Claude testers auto-review every change ─────────────
# After each chat turn, if the working tree changed, 5 one-shot `claude -p`
# instances (independent of the interactive session, no shared context) each
# read the diff, invent their own test cases, and vote APPROVE/REJECT. If it's
# not unanimous, the rejection reasons are fed back into the live session to
# fix, and the council re-runs on the new diff — up to COUNCIL_MAX_ATTEMPTS.

COUNCIL_TESTER_COUNT = 5
COUNCIL_MAX_ATTEMPTS = 3

COUNCIL_TESTER_PROMPT = """You are an independent QA tester reviewing a code change just made to this project, as tester #{n} of {total} on a review council — all {total} must approve before the change ships. Don't assume the other testers cover anything; test thoroughly on your own, and don't edit any files.

Read the code and the diff below, understand what changed, then think of realistic edge cases and failure modes and actually verify them however you can (read the surrounding code paths, run the project's existing test/build/lint commands if any exist, trace through logic by hand). Be skeptical — your job is to find real problems, not rubber-stamp the change.

Diff of what changed:
```
{diff}
```

Reply with your verdict as the FIRST line, exactly "APPROVE" or "REJECT", followed by a short explanation (2-5 sentences: what you tested and why)."""


def council_path(pid):
    return project_dir(pid) / "council.json"


def load_council(pid):
    p = council_path(pid)
    if p.exists():
        return json.loads(p.read_text())
    return {"state": "idle", "last_signature": None, "runs": []}


def save_council(pid, data):
    council_path(pid).write_text(json.dumps(data, indent=2))
    broadcast_council(pid, data)


def is_git_repo(path):
    return (Path(path) / ".git").exists()


def _git(path, *args):
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=20)


def _compute_diff_signature(path):
    """Everything currently different from HEAD — tracked-file diff plus a
    preview of untracked new files — so a run captures uncommitted work too,
    since Claude Code doesn't commit on its own."""
    tracked = _git(path, "diff", "HEAD").stdout
    status = _git(path, "status", "--porcelain").stdout
    untracked = [line[3:] for line in status.splitlines() if line.startswith("??")]
    preview = ""
    for f in untracked[:20]:
        fp = Path(path) / f
        try:
            if fp.is_file() and fp.stat().st_size < 20000:
                preview += f"\n--- new file: {f} ---\n{fp.read_text(errors='ignore')}\n"
        except Exception:
            pass
    full = tracked + preview
    signature = hashlib.sha256(full.encode()).hexdigest()
    return full, signature


def _run_council_tester(path, diff_text, n):
    prompt = COUNCIL_TESTER_PROMPT.format(n=n, total=COUNCIL_TESTER_COUNT, diff=diff_text[:12000])
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=path, capture_output=True, text=True, timeout=240,
        )
        out = result.stdout.strip()
    except Exception as e:
        return {"name": f"Tester {n}", "verdict": "ERROR", "reason": str(e)}
    lines = out.splitlines()
    first = lines[0].strip().upper() if lines else ""
    verdict = "APPROVE" if first.startswith("APPROVE") else "REJECT"
    reason = "\n".join(lines[1:]).strip() or out
    return {"name": f"Tester {n}", "verdict": verdict, "reason": reason[:2000]}


def run_council(pid):
    """Entry point called after a chat turn completes. No-ops unless the
    project is a git repo with a real, not-yet-reviewed change, and skips if
    a council run is already in flight for this project."""
    meta = load_meta(pid)
    if not meta:
        return
    path = meta.get("path")
    if not path or not os.path.isdir(path) or not is_git_repo(path):
        return

    council = load_council(pid)
    if council.get("state") in ("running_council", "fixing"):
        return

    diff_text, signature = _compute_diff_signature(path)
    if not diff_text.strip() or signature == council.get("last_signature"):
        return

    council["last_signature"] = signature
    _council_loop(pid, path, diff_text, council, attempt=1)


def _council_loop(pid, path, diff_text, council, attempt):
    name = (load_meta(pid) or {}).get("name", pid)
    run = {
        "id": uuid.uuid4().hex[:8],
        "attempt": attempt,
        "started": datetime.now().isoformat(),
        "finished": None,
        "diff_preview": diff_text[:4000],
        "testers": [],
        "approved": None,
    }
    council["state"] = "running_council"
    council.setdefault("runs", []).append(run)
    save_council(pid, council)

    with ThreadPoolExecutor(max_workers=COUNCIL_TESTER_COUNT) as ex:
        futures = [ex.submit(_run_council_tester, path, diff_text, n) for n in range(1, COUNCIL_TESTER_COUNT + 1)]
        testers = [f.result() for f in futures]

    run["testers"] = testers
    run["approved"] = all(t["verdict"] == "APPROVE" for t in testers)
    run["finished"] = datetime.now().isoformat()
    n_reject = sum(1 for t in testers if t["verdict"] != "APPROVE")

    if run["approved"] or attempt >= COUNCIL_MAX_ATTEMPTS or not session_running(pid):
        council["state"] = "approved" if run["approved"] else "gave_up"
        save_council(pid, council)
        if run["approved"]:
            notify_macos(name, "✅ Council approved the latest change (5/5)")
        else:
            notify_macos(name, f"⚠️ Council: {n_reject}/5 testers found issues after {attempt} attempt(s) — see Council tab")
        return

    # Not unanimous and attempts remain: hand the rejection reasons back to the
    # live session to fix, then re-run the council against the new diff.
    council["state"] = "fixing"
    save_council(pid, council)

    feedback = "\n\n".join(
        f"Tester {i + 1}: {t['reason']}" for i, t in enumerate(testers) if t["verdict"] != "APPROVE"
    )
    fix_prompt = (
        f"The council of independent testers reviewed your last change and {n_reject}/5 found problems. "
        f"Please fix the issues below, then let me know when you're done.\n\n{feedback}"
    )

    msgs = load_messages(pid)
    next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
    reply_id = next_id + 1
    append_message(pid, {"id": next_id, "role": "user", "text": "[Council feedback]\n" + fix_prompt, "status": "done", "timestamp": datetime.now().isoformat()})
    append_message(pid, {"id": reply_id, "role": "assistant", "text": "Addressing council feedback...", "status": "thinking", "timestamp": datetime.now().isoformat()})
    send_to_session(pid, fix_prompt, reply_id, name, notify=False)

    new_diff, new_sig = _compute_diff_signature(path)
    council["last_signature"] = new_sig
    if not new_diff.strip():
        council["state"] = "gave_up"
        save_council(pid, council)
        return
    _council_loop(pid, path, new_diff, council, attempt + 1)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/projects")
def list_projects():
    projects = all_projects()
    for p in projects:
        p["status"] = "running" if session_running(p["id"]) else "stopped"
        p["server_running"] = dev_server_running(p["id"])
        mem = get_memory(p["id"])
        p["last_session_summary"] = mem["memory"].get("last_session", "")[:80]
    return jsonify(projects)


@app.route("/api/projects", methods=["POST"])
def create_project():
    data = request.get_json()
    pid = uuid.uuid4().hex[:8]
    name = data.get("name", "New Project")
    meta = {
        "id": pid,
        "name": name,
        "description": data.get("description", ""),
        "emoji": data.get("emoji", "🚀"),
        "path": resolve_project_path(data.get("path", ""), name),
        "preview_url": data.get("preview_url", ""),
        "server_cmd": data.get("server_cmd", ""),
        "created": datetime.now().isoformat(),
        "status": "stopped",
    }
    init_project_dir(pid)
    save_meta(pid, meta)
    os.makedirs(meta["path"], exist_ok=True)

    first = data.get("first_message", "").strip()
    if first:
        threading.Thread(target=_auto_start_and_message, args=(pid, first, meta["path"], meta["server_cmd"]), daemon=True).start()
    elif data.get("auto_start"):
        def _start_only():
            if start_session(pid, meta["path"]):
                open_terminal_window(pid)
            start_dev_server(pid, meta["path"], meta["server_cmd"])
        threading.Thread(target=_start_only, daemon=True).start()

    return jsonify(meta)


def _auto_start_and_message(pid, message, path, server_cmd=""):
    if start_session(pid, path):
        open_terminal_window(pid)
    start_dev_server(pid, path, server_cmd)
    threading.Event().wait(3)
    _do_send_message(pid, message)


@app.route("/api/projects/<pid>")
def get_project(pid):
    meta = load_meta(pid)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    meta["status"] = "running" if session_running(pid) else "stopped"
    meta["server_running"] = dev_server_running(pid)
    return jsonify(meta)


@app.route("/api/projects/<pid>", methods=["PUT"])
def update_project(pid):
    meta = load_meta(pid)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    for field in ("preview_url", "preview_file", "server_cmd", "description"):
        if field in data:
            meta[field] = data[field]
    save_meta(pid, meta)
    return jsonify(meta)


@app.route("/api/projects/<pid>/generate-description", methods=["POST"])
def generate_description(pid):
    meta = load_meta(pid)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    path = meta.get("path")
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Project path not found"}), 400

    prompt = (
        "Read through this project's code and files (package.json, README, source "
        "files, etc.) and write a concise 1-2 sentence description of what this "
        "project does. Reply with only the description text itself, no preamble, "
        "no quotes, no markdown."
    )
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=path, capture_output=True, text=True, timeout=90,
        )
        desc = result.stdout.strip()
    except Exception as e:
        desc = ""
        print(f"generate_description failed for {pid}: {e}")

    if not desc:
        desc = guess_description(Path(path))
    if desc:
        meta["description"] = desc[:400]
        save_meta(pid, meta)
    return jsonify(meta)


@app.route("/api/projects/<pid>", methods=["DELETE"])
def delete_project(pid):
    kill_session(pid)
    import shutil
    shutil.rmtree(project_dir(pid), ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/start", methods=["POST"])
def start_project(pid):
    meta = load_meta(pid)
    if not meta:
        return jsonify({"error": "Not found"}), 404

    def _start():
        if start_session(pid, meta.get("path")):
            open_terminal_window(pid)
        start_dev_server(pid, meta.get("path"), meta.get("server_cmd"))
        context = build_context_prompt(pid)
        if context:
            threading.Event().wait(3)
            prompt = context + "\n\nBriefly summarize where we left off and what you'll focus on first."
            _do_send_message(pid, prompt)

    threading.Thread(target=_start, daemon=True).start()
    return jsonify({"status": "starting"})


def _pause_project_and_save(pid, notify=True):
    if not session_running(pid):
        return False
    _save_session_summary(pid, notify=notify)
    kill_session(pid)
    return True


@app.route("/api/projects/<pid>/pause", methods=["POST"])
def pause_project(pid):
    _pause_project_and_save(pid)
    return jsonify({"status": "stopped"})


def _close_all_sessions():
    """Gracefully stop every running session (save a handoff summary, then kill
    the tmux session) in parallel. Shared by the Close All button and the
    server's own shutdown hook."""
    running = get_running_sessions()
    threads = []
    for p in running:
        t = threading.Thread(target=_pause_project_and_save, args=(p["id"],), kwargs={"notify": False}, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=130)
    return running


@app.route("/api/close-all", methods=["POST"])
def close_all():
    saved = _close_all_sessions()
    return jsonify({"ok": True, "saved": [p["id"] for p in saved]})


@app.route("/api/projects/<pid>/upload", methods=["POST"])
def upload_file(pid):
    """Save a pasted/dropped image so Claude can read it by path."""
    meta = load_meta(pid)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400

    ext = Path(f.filename or "").suffix.lower()
    if not ext:
        ext = mimetypes.guess_extension(f.mimetype or "") or ".png"
    if ext == ".jpe":
        ext = ".jpg"

    uploads = project_dir(pid) / "uploads"
    uploads.mkdir(exist_ok=True)
    fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    dest = uploads / fname
    f.save(dest)
    return jsonify({"path": str(dest.resolve()), "name": fname})


# ── Project file preview ──────────────────────────────────────────────────────

SKIP_LIST_DIRS = {"node_modules", "__pycache__", "venv", ".venv"}


def resolve_in_project(pid, rel):
    """Resolve rel inside the project's codebase dir; (base, None) if it escapes."""
    meta = load_meta(pid)
    if not meta or not meta.get("path"):
        return None, None
    base = Path(os.path.expanduser(meta["path"])).resolve()
    target = (base / rel).resolve() if rel else base
    if target != base and base not in target.parents:
        return base, None
    return base, target


@app.route("/api/projects/<pid>/fs-list")
def project_fs_list(pid):
    rel = (request.args.get("path") or "").strip().strip("/")
    base, target = resolve_in_project(pid, rel)
    if base is None:
        return jsonify({"error": "Not found"}), 404
    if target is None or not target.is_dir():
        return jsonify({"error": "Not a folder"}), 400

    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return jsonify({"error": "Could not read folder"}), 400
    for e in children:
        if e.name.startswith(".") or (e.is_dir() and e.name in SKIP_LIST_DIRS):
            continue
        try:
            size = e.stat().st_size if e.is_file() else 0
        except OSError:
            size = 0
        entries.append({"name": e.name, "dir": e.is_dir(), "size": size})
    return jsonify({"path": rel, "entries": entries})


@app.route("/api/projects/<pid>/fs/<path:rel>")
def project_fs_file(pid, rel):
    """Serve a file from the project dir so the preview can embed it.

    Path-style route (not a query param) so relative asset links inside a
    previewed index.html resolve to sibling files automatically.
    """
    base, target = resolve_in_project(pid, rel)
    if base is None or target is None or not target.is_file():
        return jsonify({"error": "Not found"}), 404
    resp = send_file(target, conditional=True)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Import existing folders ───────────────────────────────────────────────────

SKIP_SCAN_DIRS = {"node_modules", "venv", ".git", "__pycache__", "dist", "build"}


def guess_description(folder: Path):
    pkg = folder / "package.json"
    if pkg.exists():
        try:
            desc = json.loads(pkg.read_text()).get("description", "")
            if desc:
                return desc[:120]
        except Exception:
            pass
    for readme in ("README.md", "readme.md", "README.txt"):
        p = folder / readme
        if p.exists():
            try:
                for line in p.read_text(errors="ignore").splitlines():
                    line = line.strip().lstrip("#").strip()
                    if line:
                        return line[:120]
            except Exception:
                pass
    return ""


def guess_emoji(name: str):
    n = name.lower()
    for words, emoji in [
        (("game", "snake", "chess", "puzzle"), "🎮"),
        (("todo", "to-do", "task", "list"), "✅"),
        (("mail", "email"), "📧"),
        (("budget", "buget", "money", "finance"), "💰"),
        (("plan", "calendar"), "📅"),
        (("react", "web", "site", "app"), "🌐"),
    ]:
        if any(w in n for w in words):
            return emoji
    return "📁"


@app.route("/api/import/candidates")
def import_candidates():
    """Folders under PROJECTS_BASE_DIR not yet tracked as projects."""
    manager_root = Path(__file__).parent.parent.resolve()
    tracked = set()
    for p in all_projects():
        try:
            tracked.add(str(Path(os.path.expanduser(p.get("path", ""))).resolve()))
        except Exception:
            pass

    out = []
    for d in sorted(PROJECTS_BASE_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.name.startswith(".") or d.name in SKIP_SCAN_DIRS:
            continue
        rp = str(d.resolve())
        if rp in tracked or rp == str(manager_root):
            continue
        out.append({
            "name": d.name,
            "path": rp,
            "description": guess_description(d),
            "emoji": guess_emoji(d.name),
        })
    return jsonify(out)


@app.route("/api/import", methods=["POST"])
def import_projects():
    data = request.get_json() or {}
    tracked = set()
    for p in all_projects():
        try:
            tracked.add(str(Path(os.path.expanduser(p.get("path", ""))).resolve()))
        except Exception:
            pass

    created = []
    for item in data.get("projects", []):
        raw = item.get("path") or ""
        if not raw.strip():
            continue
        # folder names can legitimately end in whitespace ("To-Do List "),
        # so only fall back to the stripped form if the raw path doesn't exist
        path = Path(os.path.expanduser(raw))
        if not path.is_dir():
            path = Path(os.path.expanduser(raw.strip()))
        if not path.is_dir() or str(path.resolve()) in tracked:
            continue
        pid = uuid.uuid4().hex[:8]
        meta = {
            "id": pid,
            "name": item.get("name") or path.name,
            "description": item.get("description", ""),
            "emoji": item.get("emoji", "📁"),
            "path": str(path.resolve()),
            "preview_url": "",
            "server_cmd": "",
            "created": datetime.now().isoformat(),
            "status": "stopped",
        }
        init_project_dir(pid)
        save_meta(pid, meta)
        tracked.add(str(path.resolve()))
        created.append(meta)
    return jsonify({"created": created})


@app.route("/api/projects/<pid>/seen", methods=["POST"])
def mark_seen(pid):
    clear_unread(pid)
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/messages")
def get_messages(pid):
    return jsonify(load_messages(pid))


@app.route("/api/projects/<pid>/message", methods=["POST"])
def send_message(pid):
    data = request.get_json() or {}
    text = data.get("message", "").strip()
    if not text:
        return jsonify({"error": "No message"}), 400

    if not session_running(pid):
        return jsonify({"error": "Session not running"}), 400

    session = session_name(pid)
    subprocess.run([TMUX, "set-buffer", "-t", session, text])
    subprocess.run([TMUX, "paste-buffer", "-t", session])
    threading.Event().wait(0.1)
    subprocess.run([TMUX, "send-keys", "-t", session, "", "Enter"])

    name = (load_meta(pid) or {}).get("name", pid)

    def _watch():
        threading.Event().wait(2)
        reply = wait_for_stable_reply(pid, text)
        notify_macos(name, reply)
        m = load_meta(pid)
        if m:
            m["last_activity"] = datetime.now().isoformat()
            m["unread"] = m.get("unread", 0) + 1
            save_meta(pid, m)
        run_council(pid)

    threading.Thread(target=_watch, daemon=True).start()
    return jsonify({"status": "sent"})


@app.route("/api/projects/<pid>/council")
def get_council(pid):
    data = load_council(pid)
    meta = load_meta(pid) or {}
    data["git_repo"] = bool(meta.get("path")) and is_git_repo(meta["path"])
    return jsonify(data)


@app.route("/api/projects/<pid>/council/run", methods=["POST"])
def trigger_council(pid):
    threading.Thread(target=run_council, args=(pid,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/council/stream")
def council_stream(pid):
    import queue as q_mod
    queue = q_mod.Queue()
    with _lock:
        _council_listeners.setdefault(pid, []).append(queue)

    def gen():
        try:
            yield f"data: {json.dumps(load_council(pid))}\n\n"
            while True:
                try:
                    data = queue.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            with _lock:
                try:
                    _council_listeners[pid].remove(queue)
                except ValueError:
                    pass

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _do_send_message(pid, text, notify=True):
    msgs = load_messages(pid)
    next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
    meta = load_meta(pid) or {}

    user_msg = {"id": next_id, "role": "user", "text": text, "status": "done", "timestamp": datetime.now().isoformat()}
    reply_msg = {"id": next_id + 1, "role": "assistant", "text": "Thinking...", "status": "thinking", "timestamp": datetime.now().isoformat()}

    append_message(pid, user_msg)
    append_message(pid, reply_msg)

    threading.Thread(target=send_to_session, args=(pid, text, reply_msg["id"], meta.get("name", pid), notify), daemon=True).start()
    return user_msg


@app.route("/api/projects/<pid>/scroll", methods=["POST"])
def scroll_terminal(pid):
    """Forward wheel-scroll as PageUp/PageDown into the tmux session, since Claude
    Code's TUI runs in the alternate screen buffer (no tmux scrollback of its own)
    and handles its own transcript scrolling via those keys."""
    if not session_running(pid):
        return jsonify({"ok": False}), 400
    data = request.get_json() or {}
    key = "PageUp" if data.get("direction") == "up" else "PageDown"
    count = max(1, min(int(data.get("count", 1)), 10))
    for _ in range(count):
        subprocess.run([TMUX, "send-keys", "-t", session_name(pid), key])
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/send-keys", methods=["POST"])
def send_keys(pid):
    """Forward literal tmux key names (arrow keys, Enter, ...) into the session —
    lets the UI drive Claude Code's own interactive select prompts (numbered
    option lists with a '❯' cursor) by clicking instead of arrowing manually.

    Keys are sent one at a time with a short gap between them, not bundled into
    a single tmux send-keys call — confirmed by direct testing against a real
    Claude Code select prompt that bundling them (zero delay) causes its Ink
    input handler to silently drop all but the last key, e.g. "Down Enter" in
    one call just re-selects the already-highlighted option instead of moving
    the cursor first."""
    if not session_running(pid):
        return jsonify({"ok": False}), 400
    data = request.get_json() or {}
    allowed = {"Up", "Down", "Left", "Right", "Enter", "Tab", "Escape", "Space"}
    keys = [k for k in data.get("keys", []) if k in allowed][:30]
    if not keys:
        return jsonify({"ok": False, "error": "no valid keys"}), 400
    session = session_name(pid)
    for i, key in enumerate(keys):
        if i > 0:
            threading.Event().wait(0.06)
        subprocess.run([TMUX, "send-keys", "-t", session, key])
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/terminal")
def terminal_stream(pid):
    """Stream live tmux pane content — only sends when content changes."""
    def gen():
        prev = ""
        while True:
            try:
                if session_running(pid):
                    current = capture_pane(pid)
                    if current != prev:
                        prev = current
                        yield f"data: {json.dumps({'content': current})}\n\n"
                else:
                    yield f"data: {json.dumps({'content': '', 'offline': True})}\n\n"
            except Exception:
                pass
            threading.Event().wait(0.3)

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/projects/<pid>/stream")
def stream_messages(pid):
    import queue as q_mod
    queue = q_mod.Queue()
    with _lock:
        _sse_listeners.setdefault(pid, []).append(queue)

    def gen():
        try:
            while True:
                try:
                    msg = queue.get(timeout=30)
                    yield f"data: {json.dumps(msg)}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            with _lock:
                try:
                    _sse_listeners[pid].remove(queue)
                except ValueError:
                    pass

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/projects/<pid>/memory")
def get_memory_route(pid):
    return jsonify(get_memory(pid))


@app.route("/api/projects/<pid>/memory", methods=["PUT"])
def set_memory_route(pid):
    data = request.get_json() or {}
    set_memory(pid, data.get("type"), data.get("content", ""), data.get("component"))
    return jsonify({"ok": True})


@app.route("/api/global-memory")
def get_global_memory_route():
    return jsonify({"content": get_global_memory()})


@app.route("/api/global-memory", methods=["PUT"])
def set_global_memory_route():
    data = request.get_json() or {}
    set_global_memory(data.get("content", ""))
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/tests")
def get_tests(pid):
    return jsonify(load_tests(pid))


@app.route("/api/projects/<pid>/tests", methods=["POST"])
def create_test(pid):
    data = request.get_json() or {}
    tests = load_tests(pid)
    tests.append({
        "id": uuid.uuid4().hex[:6],
        "type": data.get("type", "description"),
        "description": data.get("description", ""),
        "target_url": data.get("target_url", ""),
        "status": "pending",
        "last_run": None,
        "error": "",
    })
    save_tests(pid, tests)
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/tests/run", methods=["POST"])
def run_tests(pid):
    threading.Thread(target=run_tests_for_project, args=(pid,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/projects/<pid>/tests/<test_id>/image/<kind>")
def test_screenshot_image(pid, test_id, kind):
    if kind not in ("baseline", "latest", "diff"):
        return jsonify({"error": "Not found"}), 404
    if not any(t["id"] == test_id for t in load_tests(pid)):
        return jsonify({"error": "Not found"}), 404
    path = screenshot_test_dir(pid, test_id) / f"{kind}.png"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, mimetype="image/png")


@app.route("/api/projects/<pid>/tests/<test_id>/accept-baseline", methods=["POST"])
def accept_test_baseline(pid, test_id):
    tests = load_tests(pid)
    test = next((t for t in tests if t["id"] == test_id), None)
    if not test:
        return jsonify({"error": "Not found"}), 404
    d = screenshot_test_dir(pid, test_id)
    latest = d / "latest.png"
    if not latest.exists():
        return jsonify({"error": "No captured screenshot to accept yet"}), 400
    (d / "baseline.png").write_bytes(latest.read_bytes())
    test["status"] = "passing"
    test["error"] = ""
    test["diff_percent"] = 0.0
    save_tests(pid, tests)
    return jsonify({"ok": True})


@app.route("/api/running")
def running():
    return jsonify(get_running_sessions())


@app.route("/api/running/stream")
def running_stream():
    import queue as q_mod
    queue = q_mod.Queue()
    with _lock:
        _running_listeners.append(queue)

    def gen():
        try:
            while True:
                try:
                    data = queue.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            with _lock:
                try:
                    _running_listeners.remove(queue)
                except ValueError:
                    pass

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


LOGIN_PAGE = """<!doctype html>
<html><head><title>Claude Manager — Login</title>
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
    <h1>Claude Manager</h1>
    {error}
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Log in</button>
  </form>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        stored = AUTH_HASH_PATH.read_text().strip() if AUTH_HASH_PATH.exists() else None
        if stored and check_password_hash(stored, request.form.get("password", "")):
            session["authed"] = True
            return redirect("/")
        return LOGIN_PAGE.format(error='<div class="err">Wrong password.</div>'), 401
    return LOGIN_PAGE.format(error="")


@app.route("/logout")
def logout():
    session.pop("authed", None)
    return redirect("/login")


@app.before_request
def require_login():
    # AUTH_HASH_PATH is checked fresh on every request (not cached at startup), so
    # running set_password.py turns the gate on immediately — no backend restart needed.
    if not AUTH_HASH_PATH.exists():
        return
    if request.path in ("/login", "/logout") or session.get("authed"):
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


# ── Frontend (built app) ──────────────────────────────────────────────────────
# Serves frontend/dist so the whole app lives on one port (8888) and is
# reachable from the phone. Rebuild with `npm run build` after frontend changes.

FRONTEND_DIST = (Path(__file__).parent.parent / "frontend" / "dist").resolve()


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


_shutdown_done = False


def _shutdown_once():
    """Kill every running tmux session (Claude + dev server) when the app
    itself is closed, so nothing is left running orphaned in the background.
    Reuses the same graceful save-then-kill path as the Close All button.
    Guarded so both the signal handler and atexit firing don't double-run it."""
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    running = get_running_sessions()
    if not running:
        return
    print(f"[shutdown] closing {len(running)} running session(s)...")
    _close_all_sessions()
    print("[shutdown] done")


def _handle_shutdown_signal(signum, frame):
    _shutdown_once()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    atexit.register(_shutdown_once)
    threading.Thread(target=limit_monitor, daemon=True).start()
    threading.Thread(target=blocked_monitor, daemon=True).start()
    print("Claude Manager backend running on http://0.0.0.0:8888")
    app.run(host="0.0.0.0", port=8888, debug=False, threaded=True)
