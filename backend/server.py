import atexit
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request, Response, send_file, session, redirect
from flask_cors import CORS
from werkzeug.security import check_password_hash

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


def capture_session_pane(session):
    r = subprocess.run(
        [TMUX, "capture-pane", "-t", session, "-p", "-S", "-500"],
        capture_output=True, text=True
    )
    ansi = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi.sub('', r.stdout)


def capture_pane(pid):
    return capture_session_pane(session_name(pid))


def cursor_position(session):
    """Cursor cell (0-indexed, pane-relative) and visibility, so the terminal
    mirror can draw a caret instead of just dumping static text — capture-pane
    has no way to mark where the cursor actually is."""
    r = subprocess.run(
        [TMUX, "display-message", "-p", "-t", session, "-F", "#{cursor_x},#{cursor_y},#{cursor_flag}"],
        capture_output=True, text=True
    )
    try:
        x, y, flag = r.stdout.strip().split(",")
        return {"x": int(x), "y": int(y), "visible": flag == "1"}
    except (ValueError, AttributeError):
        return None


def paste_into_session(session, message, delay=0.3, submit=True):
    """Type a message into a Claude Code tmux session and (by default) submit it.
    Used for the project's own session and for each split agent's session alike,
    and for injecting arbitrary pasted text into the terminal (submit=False).

    Two subtleties, both verified against a live tmux server:
    - Paste buffers are GLOBAL, not per-session ("set-buffer -t" targets a client,
      not a session). With several senders in flight at once — chat, the split
      briefing thread, six agent input boxes — an unnamed buffer lets one
      message get pasted into another agent's session. A uniquely named buffer
      per call makes delivery race-free; -d drops it once pasted.
    - "--" terminates the option list, so a message starting with "-" (a markdown
      bullet, a pasted diff's "--- a/file") isn't parsed as a flag. Without it
      set-buffer fails and paste-buffer silently re-sends the PREVIOUS message.

    Uses tmux's bracketed-paste path (paste-buffer) rather than literal send-keys
    so embedded newlines land as text in Claude Code's Ink input instead of each
    one submitting early like a real Enter keypress would.
    """
    buf = f"p2c-{uuid.uuid4().hex[:8]}"
    subprocess.run([TMUX, "set-buffer", "-b", buf, "--", message])
    subprocess.run([TMUX, "paste-buffer", "-d", "-b", buf, "-t", session])
    if submit:
        threading.Event().wait(delay)
        subprocess.run([TMUX, "send-keys", "-t", session, "", "Enter"])


# Claude Code's TUI is ready for input once it has painted its prompt box. The
# footer is the surest marker (we always launch with --dangerously-skip-permissions,
# so the bypass line is always there); the bare "❯" is a fallback.
CLAUDE_READY_RE = re.compile(r"bypass permissions|Try \"|^❯", re.M)


def wait_for_claude_ready(session, timeout=60):
    """Block until a freshly-spawned Claude Code session can actually accept a
    message. Pasting into a TUI that's still booting silently drops the text —
    which is easy to hit on a cold tmux server, where startup is slowest."""
    deadline = datetime.now().timestamp() + timeout
    while datetime.now().timestamp() < deadline:
        if CLAUDE_READY_RE.search(capture_session_pane(session)):
            threading.Event().wait(1.0)  # let the input box settle before typing
            return True
        threading.Event().wait(0.5)
    return False


def spawn_claude_tmux(session, cwd):
    """Launch a detached Claude Code session in `cwd`. No-ops if it already exists."""
    if _tmux_has_session(session):
        return False

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


def start_session(pid, project_path=None):
    cwd = os.path.expanduser(project_path) if project_path else os.path.expanduser("~")
    return spawn_claude_tmux(session_name(pid), cwd)


def open_terminal_window(pid):
    """Pop a native Terminal.app window attached to the session (macOS only).
    On Linux the web mirror is the only UI; attach by hand with
    `tmux attach -t <session>` if you are at the server's shell."""
    if not IS_MACOS:
        return
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
    if not IS_MACOS:
        marker.unlink()
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
    """Desktop notification. macOS: Notification Center via osascript. Linux:
    there is usually no desktop (WSL server), so it goes to the log instead —
    `journalctl -u agent-capitol` shows them — unless notify-send exists."""
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

    paste_into_session(session_name(pid), message)
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
    return text


# ── Context-bloat monitor ─────────────────────────────────────────────────────
# A long-running session accumulates tool output, dead ends and stale file
# contents until Claude Code's own auto-compaction fires blind (nothing gets
# saved first) or the model just gets slow and forgetful. This watches each
# running session's live context size and, once it's bloated, has the agent
# checkpoint what matters into project memory and then /compact with explicit
# instructions about what to keep.
#
# Where the numbers come from: Claude Code writes every session's transcript to
# ~/.claude/projects/<cwd with non-alphanumerics → "-">/<session-id>.jsonl, and
# each assistant entry carries the API usage for that turn. input + cache
# creation + cache read tokens is exactly the context that was sent on the
# most recent turn — i.e. the live context size. Verified against a real
# transcript on this Mac (2026-09-06).
#
# Replaced the usage-limit watchdog (2026-09-06): the upgraded plan doesn't hit
# limits, and bloat, not quota, is what actually degrades long sessions.

CONTEXT_CHECK_INTERVAL = 60  # seconds
CONTEXT_TRIM_TOKENS = int(os.environ.get("CONTEXT_TRIM_TOKENS", "120000"))
CONTEXT_MAX_HOURS = float(os.environ.get("CONTEXT_MAX_HOURS", "8"))
CONTEXT_TRIM_COOLDOWN = 30 * 60  # seconds before the same session is trimmed again
CONTEXT_COMPACT_TIMEOUT = 240    # seconds to wait for /compact to finish

CONTEXT_CHECKPOINT_PROMPT = (
    "This session's context has grown large, so it's about to be compacted. First write a "
    "handoff checkpoint: what we've accomplished, what is in progress right now (which files "
    "are being edited and why, decisions made and their reasons), what to do next, and any "
    "gotchas discovered. Plain text, max 300 words. Future-you will work from this."
)
CONTEXT_COMPACT_INSTRUCTIONS = (
    "Keep: the current task and its exact state, decisions and their reasons, files being "
    "edited and what remains in each, next steps, and gotchas. Drop: exploration that led "
    "nowhere, raw tool output, file contents already applied, and resolved questions."
)


def transcript_dir(path):
    encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(Path(path).resolve()))
    return Path.home() / ".claude" / "projects" / encoded


def _first_timestamp(f):
    try:
        with open(f) as fh:
            for line in fh:
                try:
                    ts = json.loads(line).get("timestamp")
                except ValueError:
                    continue
                if ts:
                    return ts
    except OSError:
        pass
    return None


def _parse_iso(ts):
    """ISO string → aware UTC datetime (transcripts use a trailing Z; our own
    fields are written with an explicit offset). None if unparseable."""
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def session_transcript(pid, path):
    """The transcript belonging to this project's tmux session: the most
    recently modified .jsonl for the cwd that started no earlier than the tmux
    session did — the same directory can hold transcripts from the user's own
    terminal sessions, and picking the newest blindly would read one of those."""
    d = transcript_dir(path)
    if not d.is_dir():
        return None
    r = subprocess.run([TMUX, "display-message", "-p", "-t", session_name(pid), "#{session_created}"],
                       capture_output=True, text=True)
    try:
        created = datetime.fromtimestamp(int(r.stdout.strip()) - 60, tz=timezone.utc)
    except ValueError:
        created = None
    candidates = []
    for f in sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]:
        first = _parse_iso(_first_timestamp(f))
        if created and first and first < created:
            continue
        candidates.append((f.stat().st_mtime, f, first))
    if not candidates:
        return None
    _, f, first = max(candidates)
    return f, first


def _last_context_tokens(f):
    """Context size on the most recent assistant turn, read from the tail of
    the transcript (they grow to megabytes; the last turn is all we need)."""
    try:
        size = f.stat().st_size
        with open(f, "rb") as fh:
            fh.seek(max(0, size - 512 * 1024))
            tail = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if '"assistant"' not in line and "compact_boundary" not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        # A compaction (ours via /compact, or Claude Code's own auto-compact)
        # logs {"type":"system","subtype":"compact_boundary","compactMetadata":
        # {"preTokens":..,"postTokens":..}}. If that's the latest event, the
        # post-compaction size is the truth — the assistant entry before it
        # still carries the old, bloated count.
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            post = (e.get("compactMetadata") or {}).get("postTokens")
            if isinstance(post, int):
                return post
            continue
        u = ((e.get("message") or {}).get("usage")) if e.get("type") == "assistant" else None
        if u:
            return (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0))
    return None


_trim_state = {}  # pid -> "checkpoint" | "compacting" while a trim is in progress
_trim_lock = threading.Lock()


def context_status(pid, meta=None):
    """What the UI shows and the monitor decides on: live token count, hours
    since the session started (or since the last trim), and whether that
    crosses the bloat line."""
    meta = meta if meta is not None else (load_meta(pid) or {})
    out = {"tokens": None, "hours": None, "bloated": False,
           "threshold": CONTEXT_TRIM_TOKENS, "max_hours": CONTEXT_MAX_HOURS,
           "last_trim": meta.get("context_trimmed_at"), "trims": meta.get("context_trims", 0),
           "trim_state": _trim_state.get(pid)}
    path = meta.get("path")
    if not path or not session_running(pid):
        return out
    found = session_transcript(pid, path)
    if not found:
        return out
    f, first = found
    out["tokens"] = _last_context_tokens(f)
    since = _parse_iso(meta.get("context_trimmed_at")) or first
    if since:
        out["hours"] = round((datetime.now(timezone.utc) - since).total_seconds() / 3600, 1)
    out["bloated"] = _is_bloated(out["tokens"], out["hours"])
    return out


def _is_bloated(tokens, hours):
    if not tokens:
        return False
    if tokens >= CONTEXT_TRIM_TOKENS:
        return True
    # Long-running *and* well on its way: don't let an all-day session coast
    # toward the hard threshold and then compact mid-thought.
    return hours is not None and hours >= CONTEXT_MAX_HOURS and tokens >= CONTEXT_TRIM_TOKENS // 2


def session_idle(pid):
    """Prompt painted and nothing generating — safe to type into."""
    pane = capture_pane(pid)
    return bool(CLAUDE_READY_RE.search(pane)) and "esc to interrupt" not in pane


def trim_context(pid, reason="auto"):
    """Checkpoint, then compact. Serialised per project; returns True once the
    compaction has visibly completed."""
    with _trim_lock:
        if pid in _trim_state:
            return False
        _trim_state[pid] = "checkpoint"
    try:
        meta = load_meta(pid) or {}
        name = meta.get("name", pid)
        if not session_running(pid):
            return False
        before = context_status(pid, meta).get("tokens")

        # 1. Have the agent write down what matters while it still has the
        #    full picture. Lands in the project's last_session memory, which is
        #    injected on the next session start too.
        saved = _save_session_summary(pid, notify=False, prompt=CONTEXT_CHECKPOINT_PROMPT)

        # 2. Compact with instructions, and wait for the TUI to finish: the
        #    footer keeps showing "esc to interrupt" / a "Compacting…" line
        #    until the summary is in place.
        _trim_state[pid] = "compacting"
        session = session_name(pid)
        paste_into_session(session, "/compact " + CONTEXT_COMPACT_INSTRUCTIONS)
        threading.Event().wait(4)
        deadline = datetime.now().timestamp() + CONTEXT_COMPACT_TIMEOUT
        while datetime.now().timestamp() < deadline:
            pane = capture_session_pane(session)
            if "ompacting" not in pane and "esc to interrupt" not in pane and CLAUDE_READY_RE.search(pane):
                break
            threading.Event().wait(2)

        meta = load_meta(pid) or {}
        meta["context_trimmed_at"] = datetime.now(timezone.utc).isoformat()
        meta["context_trims"] = meta.get("context_trims", 0) + 1
        meta["context_last_trim"] = {"at": meta["context_trimmed_at"], "reason": reason,
                                     "tokens_before": before, "checkpoint_saved": bool(saved)}
        save_meta(pid, meta)
        before_k = f"{before // 1000}k tokens" if before else "context"
        notify_macos(name, f"🧹 Trimmed {before_k} — checkpoint saved to memory, context compacted")
        print(f"[context-monitor] {name}: trimmed ({reason}), before={before}, checkpoint={bool(saved)}")
        return True
    finally:
        with _trim_lock:
            _trim_state.pop(pid, None)


def _context_tick():
    now = datetime.now(timezone.utc)
    for p in all_projects():
        pid = p["id"]
        if not session_running(pid):
            continue
        meta = load_meta(pid) or {}
        st = context_status(pid, meta)
        if not st["bloated"]:
            continue
        last = _parse_iso(meta.get("context_trimmed_at"))
        if last and (now - last).total_seconds() < CONTEXT_TRIM_COOLDOWN:
            continue
        # Never type into a session something else is driving right now.
        if pid in _trim_state or pid in _review_inflight or not session_idle(pid):
            continue
        print(f"[context-monitor] {meta.get('name', pid)}: {st['tokens']} tokens, {st['hours']}h — trimming")
        threading.Thread(target=trim_context, args=(pid, "auto"), daemon=True).start()


def context_monitor():
    while True:
        threading.Event().wait(CONTEXT_CHECK_INTERVAL)
        try:
            _context_tick()
        except Exception as e:
            print(f"[context-monitor] error: {e}")


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
    p.parent.mkdir(parents=True, exist_ok=True)
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
            entry["context"] = context_status(p["id"], p)
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


# ── Review loop: one reviewer, the author decides when it's done ──────────────
# After each chat turn, if the working tree changed, a single one-shot
# `claude -p` reviewer (independent of the interactive session, no shared
# context) reads the diff and either APPROVEs or reports concrete problems.
# Problems go back into the live session, whose agent fixes what it agrees
# with and then decides: "REVIEW: DONE" (it judges the change complete — the
# loop ends) or "REVIEW: AGAIN" (re-review the new diff). The author's
# decision is what terminates the loop; REVIEW_MAX_ROUNDS is only a backstop
# so a stuck exchange can't cycle forever.
#
# This replaced a 5-tester unanimous-vote council (2026-09-05). With LLM
# reviewers, five parallel opinions never converge — each one always finds
# *something* — so it produced noise rather than signal, and the agent that
# actually wrote the change had no say in when it was good enough.

REVIEW_MAX_ROUNDS = 3
_review_inflight = set()
_review_lock = threading.Lock()
REVIEW_DECISION_RE = re.compile(r"REVIEW\s*[:\-]?\s*(DONE|AGAIN)\b", re.I)

REVIEWER_PROMPT = """You are reviewing a code change just made to this project. Don't edit any files.

Read the diff below and the surrounding code it touches. Look only for real defects: incorrect behavior, broken edge cases, regressions, security problems. If the project has a test/build/lint command, you may run it. Do NOT report style preferences, hypothetical refactors, missing tests, or anything you can't point to concretely. A sound change should get APPROVE, and approving is the expected outcome most of the time — don't invent findings to justify a review.

Diff of what changed:
```
{diff}
```

Reply with your verdict as the FIRST line, exactly "APPROVE" or "REJECT". If REJECT, follow with at most 3 findings, most severe first, one short paragraph each: what's wrong, where (file/function), and how you know."""

FIX_PROMPT = """A reviewer looked at your last change and reported the findings below. Fix whatever is genuinely a problem. If a finding is wrong or not worth changing, say why instead of changing code — you have the final call.

{findings}

When you're finished, end your reply with exactly one of these lines:
REVIEW: DONE  — the change is complete and needs no further review
REVIEW: AGAIN — you changed enough that the reviewer should look once more"""


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


def _run_reviewer(path, diff_text):
    """One independent `claude -p` pass over the diff. Returned in the same
    shape the old per-tester records used (name/verdict/reason) so existing
    council.json history and the Council tab keep rendering."""
    prompt = REVIEWER_PROMPT.format(diff=diff_text[:12000])
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=path, capture_output=True, text=True, timeout=240,
        )
        out = result.stdout.strip()
    except Exception as e:
        return {"name": "Reviewer", "verdict": "ERROR", "reason": str(e)}
    lines = out.splitlines()
    first = lines[0].strip().upper() if lines else ""
    verdict = "APPROVE" if first.startswith("APPROVE") else "REJECT"
    reason = "\n".join(lines[1:]).strip() or out
    return {"name": "Reviewer", "verdict": verdict, "reason": reason[:4000]}


def run_council(pid):
    """Entry point called after a chat turn completes. No-ops unless the
    project is a git repo with a real, not-yet-reviewed change, and skips if
    a review is already in flight for this project."""
    meta = load_meta(pid)
    if not meta:
        return
    path = meta.get("path")
    if not path or not os.path.isdir(path) or not is_git_repo(path):
        return

    # In-flight tracking lives in memory, not in council.json: the file's
    # "running_council"/"fixing" state survives a server restart mid-run, and
    # trusting it left projects stuck in "reviewing…" forever with every later
    # review silently skipped (seen on a real project, stale since 2026-07-31).
    with _review_lock:
        if pid in _review_inflight:
            return
        _review_inflight.add(pid)
    try:
        council = load_council(pid)
        for run in council.get("runs", []):
            if not run.get("finished"):
                run["finished"] = datetime.now().isoformat()
                run["aborted"] = True  # orphaned by a restart; shown as "interrupted"

        diff_text, signature = _compute_diff_signature(path)
        if not diff_text.strip() or signature == council.get("last_signature"):
            if council.get("state") in ("running_council", "fixing"):
                council["state"] = "idle"
                save_council(pid, council)
            return

        council["last_signature"] = signature
        _review_loop(pid, path, diff_text, council, attempt=1)
    finally:
        with _review_lock:
            _review_inflight.discard(pid)


def _finish(pid, council, state, name, message):
    council["state"] = state
    save_council(pid, council)
    notify_macos(name, message)


def _review_loop(pid, path, diff_text, council, attempt):
    name = (load_meta(pid) or {}).get("name", pid)
    run = {
        "id": uuid.uuid4().hex[:8],
        "attempt": attempt,
        "started": datetime.now().isoformat(),
        "finished": None,
        "diff_preview": diff_text[:4000],
        "testers": [],
        "approved": None,
        "author": None,  # {"decision": "DONE"|"AGAIN"|"NONE", "reply": str} once the author has answered
    }
    council["state"] = "running_council"
    council.setdefault("runs", []).append(run)
    save_council(pid, council)

    reviewer = _run_reviewer(path, diff_text)
    run["testers"] = [reviewer]
    run["approved"] = reviewer["verdict"] == "APPROVE"
    run["finished"] = datetime.now().isoformat()

    if run["approved"]:
        _finish(pid, council, "approved", name, "✅ Reviewer approved the latest change")
        return
    if reviewer["verdict"] == "ERROR" or not session_running(pid):
        _finish(pid, council, "gave_up", name, "⚠️ Review couldn't complete — see Council tab")
        return

    # Hand the findings to the author. It fixes what it agrees with and then
    # decides whether the loop continues.
    council["state"] = "fixing"
    save_council(pid, council)

    fix_prompt = FIX_PROMPT.format(findings=reviewer["reason"])
    msgs = load_messages(pid)
    next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
    reply_id = next_id + 1
    append_message(pid, {"id": next_id, "role": "user", "text": "[Review feedback]\n" + fix_prompt, "status": "done", "timestamp": datetime.now().isoformat()})
    append_message(pid, {"id": reply_id, "role": "assistant", "text": "Addressing review feedback...", "status": "thinking", "timestamp": datetime.now().isoformat()})
    reply = send_to_session(pid, fix_prompt, reply_id, name, notify=False) or ""

    m = REVIEW_DECISION_RE.search(reply)
    decision = m.group(1).upper() if m else "NONE"
    run["author"] = {"decision": decision, "reply": reply[-2000:]}

    # Whatever the author did counts as reviewed: record the new tree state so
    # the next chat turn doesn't re-review the fix itself.
    new_diff, council["last_signature"] = _compute_diff_signature(path)

    if decision != "AGAIN":
        # DONE, or no explicit decision at all — either way the author didn't
        # ask for another pass, and defaulting to "stop" is what keeps this
        # loop finite when the reply is malformed.
        suffix = "" if decision == "DONE" else " (no explicit decision in its reply)"
        _finish(pid, council, "done", name, f"✔ Author addressed the review and closed it{suffix}")
        return
    if attempt >= REVIEW_MAX_ROUNDS:
        _finish(pid, council, "gave_up", name, f"⚠️ Review still open after {attempt} rounds — see Council tab")
        return
    if not new_diff.strip():
        _finish(pid, council, "done", name, "✔ Review closed — nothing left to re-review")
        return
    _review_loop(pid, path, new_diff, council, attempt + 1)


# ── Split runs (parallel mini-agents, one git worktree each) ─────────────────
# The normal chat drives ONE session through a task sequentially. For a big task
# that decomposes cleanly, a split instead fans it out: a planner reads the repo
# and proposes independent pieces, the user edits/approves them, and each piece
# gets its own git worktree + branch + Claude Code tmux session. Every agent is a
# full interactive session — same terminal mirror and input box as Chat — so the
# user can correct any one of them mid-flight without touching the others. When
# they're done the branches are merged back into the base branch together.

SPLIT_ROOT = Path.home() / ".agent-capitol-splits"
SPLIT_MAX_BRANCHES = 6
SPLIT_DEFAULT_BRANCHES = 3
SPLIT_CHECK_INTERVAL = 8    # seconds between agent-status sweeps
SPLIT_IDLE_THRESHOLD = 45   # seconds of unchanged pane before an agent reads as idle

# Statuses the status sweep is allowed to overwrite. Merge outcomes
# (merged/conflict/empty) are decisions, not observations — they stick until the
# next merge attempt re-decides them.
SPLIT_LIVE_STATUSES = ("starting", "working", "idle", "stopped")

SPLIT_PLAN_PROMPT = """You are planning how to split one large task across independent Claude Code agents that will work in PARALLEL — each in its own git worktree on its own branch of this project — whose branches are all merged back together at the end.

The task:
---
{prompt}
---

First read enough of this project to understand how it's actually structured. Then split the task into independent pieces. Rules:
- Each piece must be workable on its own, right now, without waiting on another piece.
- Minimize file overlap. Two agents editing the same file means a merge conflict. If some shared file (a router, an index, a schema, a config) has to change, assign it to exactly ONE piece and tell the others not to touch it.
- Aim for {count} pieces, but use fewer if the task doesn't honestly divide that far. Never more than {max}.
- Each "task" is the ONLY thing its agent will be told, besides the repo itself. Write it as complete standalone instructions: what to build, where it goes, and how it meets the other pieces (names of functions/props/endpoints it should expose or assume).

Reply with ONLY a JSON array and nothing else — no prose, no markdown fence:
[{{"name": "short-kebab-name", "task": "full standalone instructions for this agent", "files": ["likely/path.js"]}}]"""

SPLIT_AGENT_PROMPT = """You are agent {n} of {total} working in PARALLEL with other Claude Code agents on one larger task. You're in your own git worktree on branch `{branch}`; the others are in their own worktrees on their own branches, and all the branches get merged together once everyone is done.

The overall goal, for context only:
{overall}

YOUR piece — the only piece you should implement:
{task}
{files_line}
Rules:
- Stay inside your piece. Do not implement the other agents' pieces, and avoid editing files outside yours: {others}
- The other agents' work will NOT appear in your worktree. Don't wait for it. If your piece needs something they're building, code against the interface described above and move on.
- Commit your work on this branch when it's done: `git add -A && git commit -m "..."` with a short message.
- Finish by printing one line starting with "DONE:" and a one-sentence summary."""


def split_path(pid):
    return project_dir(pid) / "split.json"


# Unlike the other state files, split.json has genuinely concurrent writers: the
# status sweep, the agent-launch thread, and whatever the user just clicked. So
# writes go through a temp file + atomic rename (a reader can never catch a
# half-written file), and every read-modify-write holds the project's lock so one
# update can't silently clobber another's.
_split_locks = {}
_split_locks_guard = threading.Lock()


def split_lock(pid):
    with _split_locks_guard:
        return _split_locks.setdefault(pid, threading.RLock())


def load_split(pid):
    p = split_path(pid)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"state": "idle", "run": None, "history": []}


def save_split(pid, data):
    p = split_path(pid)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)
    broadcast_split(pid, data)


def split_session_name(pid, bid):
    return f"claude_split_{pid}_{bid}"


def split_run_dir(pid, run_id):
    meta = load_meta(pid) or {}
    return SPLIT_ROOT / f"{slugify(meta.get('name', pid))}-{pid}" / run_id


def _split_error(pid, message):
    with split_lock(pid):
        data = load_split(pid)
        data["state"] = "idle" if not data.get("run") else data["state"]
        data["error"] = message
        save_split(pid, data)
        return data


def split_preflight(pid):
    """(path, base_branch, base_sha, error). A split needs a git repo with at
    least one commit — worktrees branch off HEAD."""
    meta = load_meta(pid) or {}
    path = meta.get("path")
    if not path or not os.path.isdir(path):
        return None, None, None, "Project folder not found."
    if not is_git_repo(path):
        return None, None, None, "This project isn't a git repo. Run `git init` in the project folder to use splits."
    head = _git(path, "rev-parse", "HEAD")
    if head.returncode != 0:
        return None, None, None, "This repo has no commits yet. Make one commit, then split."
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    return path, branch, head.stdout.strip(), None


def _parse_plan_json(out):
    """Pull the JSON array out of a planner reply, tolerating a stray fence or
    a sentence of preamble."""
    text = out.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    branches = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        task = str(item.get("task") or "").strip()
        if not name or not task:
            continue
        files = [str(f) for f in (item.get("files") or []) if isinstance(f, (str, int))][:12]
        branches.append({"name": name, "task": task, "files": files})
    return branches[:SPLIT_MAX_BRANCHES]


def plan_split(pid, prompt, count):
    path, base_branch, base_sha, err = split_preflight(pid)
    if err:
        return _split_error(pid, err)

    with split_lock(pid):
        data = load_split(pid)
        data.update({"state": "planning", "error": "", "prompt": prompt, "run": None})
        save_split(pid, data)

    planner = SPLIT_PLAN_PROMPT.format(prompt=prompt, count=count, max=SPLIT_MAX_BRANCHES)
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", planner, "--dangerously-skip-permissions"],
            cwd=path, capture_output=True, text=True, timeout=420,
        )
        branches = _parse_plan_json(result.stdout)
    except Exception as e:
        return _split_error(pid, f"Planner failed: {e}")

    if not branches:
        return _split_error(pid, "The planner didn't return a usable split. Try rewording the task.")

    with split_lock(pid):
        data = load_split(pid)
        data.update({
            "state": "planned",
            "error": "",
            "prompt": prompt,
            "proposed": branches,
            "base_branch": base_branch,
            "base_sha": base_sha,
        })
        save_split(pid, data)
        return data


def _unique_slugs(names):
    slugs, seen = [], set()
    for name in names:
        base = slugify(name)[:32] or "branch"
        slug, n = base, 2
        while slug in seen:
            slug = f"{base}-{n}"
            n += 1
        seen.add(slug)
        slugs.append(slug)
    return slugs


def launch_split(pid, prompt, branches):
    path, base_branch, base_sha, err = split_preflight(pid)
    if err:
        return _split_error(pid, err)

    # One run at a time. Overwriting a live run would strand its worktrees and
    # tmux sessions with nothing left pointing at them to clean up.
    if (load_split(pid) or {}).get("run"):
        return _split_error(pid, "A split run is already open — close it before starting another.")

    branches = [b for b in branches if (b.get("name") or "").strip() and (b.get("task") or "").strip()]
    if len(branches) < 2:
        return _split_error(pid, "A split needs at least 2 branches.")
    branches = branches[:SPLIT_MAX_BRANCHES]

    run_id = uuid.uuid4().hex[:6]
    run_dir = split_run_dir(pid, run_id)
    slugs = _unique_slugs([b["name"] for b in branches])

    entries = []
    for i, (b, slug) in enumerate(zip(branches, slugs), start=1):
        entries.append({
            "id": f"{run_id}{i}",
            "n": i,
            "name": b["name"].strip(),
            "slug": slug,
            "task": b["task"].strip(),
            "files": b.get("files") or [],
            "branch": f"split/{run_id}/{slug}",
            "worktree": str(run_dir / slug),
            "status": "starting",
            "changed_files": [],
            "note": "",
        })

    run = {
        "id": run_id,
        "prompt": prompt,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "started": datetime.now().isoformat(),
        "merged": None,
        "branches": entries,
    }
    with split_lock(pid):
        data = load_split(pid)
        data.update({"state": "running", "error": "", "prompt": prompt, "run": run, "proposed": [], "needs_base_commit": []})
        save_split(pid, data)

        run_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            r = _git(path, "worktree", "add", "-b", entry["branch"], entry["worktree"], base_sha)
            if r.returncode != 0:
                entry["status"] = "stopped"
                entry["note"] = f"Couldn't create worktree: {r.stderr.strip()[:300]}"
        save_split(pid, data)

    threading.Thread(target=_start_split_agents, args=(pid, run["id"]), daemon=True).start()
    return data


def _brief_landed(session, marker, timeout=12):
    """Confirm a submitted message actually reached the TUI — it echoes the text
    into its transcript, so the branch name showing up means it took."""
    deadline = datetime.now().timestamp() + timeout
    while datetime.now().timestamp() < deadline:
        if marker in capture_session_pane(session):
            return True
        threading.Event().wait(0.5)
    return False


def _start_split_agents(pid, run_id):
    """Boot each agent's session and hand it its brief.

    Sessions are created first, in one pass, so they all boot concurrently — then
    each is briefed once its TUI is actually up. (Creation stays sequential and
    blocking: tmux session creation racing itself on a cold server is the failure
    start_session()'s blocking run() exists to avoid.)"""
    data = load_split(pid)
    run = data.get("run") or {}
    if run.get("id") != run_id:
        return
    entries = [e for e in run["branches"] if e["status"] != "stopped"]
    total = len(entries)

    for entry in entries:
        spawn_claude_tmux(split_session_name(pid, entry["id"]), entry["worktree"])

    for entry in entries:
        names = [e["name"] for e in entries if e["id"] != entry["id"]]
        files_line = ("\nFiles you'll most likely be working in: " + ", ".join(entry["files"]) + "\n") if entry["files"] else ""
        brief = SPLIT_AGENT_PROMPT.format(
            n=entry["n"], total=total, branch=entry["branch"], overall=run["prompt"],
            task=entry["task"], files_line=files_line,
            others=", ".join(names) if names else "(none)",
        )
        session = split_session_name(pid, entry["id"])
        # Waiting and pasting are slow (seconds each) — deliberately outside the
        # lock, so a merge or a correction the user fires meanwhile isn't blocked.
        wait_for_claude_ready(session)
        paste_into_session(session, brief)
        if not _brief_landed(session, entry["branch"]):
            # Belt and braces: an agent that silently never received its brief
            # just sits at an empty prompt looking "idle", which is the one
            # failure the user can't tell apart from "finished already".
            paste_into_session(session, brief)
            if not _brief_landed(session, entry["branch"]):
                entry["note"] = "Couldn't confirm this agent received its brief — check its terminal."
        entry["status"] = "working"

        with split_lock(pid):
            data = load_split(pid)
            if (data.get("run") or {}).get("id") != run_id:
                return  # run was cleaned up under us
            for e in data["run"]["branches"]:
                if e["id"] == entry["id"]:
                    e["status"] = "working"
                    e["note"] = entry.get("note", "")
            save_split(pid, data)


def split_branch_files(entry, base_sha):
    """Everything this agent has touched — committed on its branch plus whatever
    is still uncommitted in its worktree."""
    wt = entry["worktree"]
    if not os.path.isdir(wt):
        return []
    files = set()
    committed = _git(wt, "diff", "--name-only", base_sha)
    if committed.returncode == 0:
        files.update(f for f in committed.stdout.splitlines() if f.strip())
    status = _git(wt, "status", "--porcelain")
    if status.returncode == 0:
        files.update(line[3:].strip() for line in status.stdout.splitlines() if line[3:].strip())
    return sorted(files)[:40]


_split_pane_watch = {}  # "pid:bid" -> {"body": str, "changed_at": float}


def _split_tick():
    """Keep each agent's live status (working / idle / stopped) and touched-file
    list current, using the same stable-pane diffing as the blocked watchdog.

    Observations (pane captures, git calls) are gathered WITHOUT the project lock
    since they're slow, then applied to a freshly-reloaded copy under it — so a
    merge or cleanup landing mid-sweep wins instead of being overwritten by
    readings taken before it happened."""
    now = datetime.now().timestamp()
    seen = set()
    for meta in all_projects():
        pid = meta["id"]
        data = load_split(pid)
        run = data.get("run")
        if not run or data.get("state") not in ("running", "merging", "merged", "conflict"):
            continue

        observed = {}
        for entry in run["branches"]:
            key = f"{pid}:{entry['id']}"
            seen.add(key)
            files = split_branch_files(entry, run["base_sha"])
            status = None

            if entry["status"] in SPLIT_LIVE_STATUSES:
                session = split_session_name(pid, entry["id"])
                if not _tmux_has_session(session):
                    status = "stopped"
                else:
                    body = pane_body(capture_session_pane(session))
                    with _lock:
                        prev = _split_pane_watch.get(key)
                        if prev is None or prev["body"] != body:
                            _split_pane_watch[key] = {"body": body, "changed_at": now}
                            idle_for = 0
                        else:
                            idle_for = now - prev["changed_at"]
                    status = "idle" if idle_for >= SPLIT_IDLE_THRESHOLD else "working"
            observed[entry["id"]] = (status, files)

        with split_lock(pid):
            fresh = load_split(pid)
            fresh_run = fresh.get("run")
            if not fresh_run or fresh_run["id"] != run["id"]:
                continue  # run was replaced or cleaned up while we looked
            dirty = False
            for entry in fresh_run["branches"]:
                status, files = observed.get(entry["id"], (None, None))
                if files is not None and files != entry.get("changed_files"):
                    entry["changed_files"] = files
                    dirty = True
                # Re-check against the fresh copy: a merge may have moved this
                # branch to a terminal status since the reading was taken.
                if status and entry["status"] in SPLIT_LIVE_STATUSES and status != entry["status"]:
                    entry["status"] = status
                    dirty = True
            if dirty:
                save_split(pid, fresh)

    with _lock:
        for key in list(_split_pane_watch):
            if key not in seen:
                del _split_pane_watch[key]


def split_monitor():
    while True:
        try:
            _split_tick()
        except Exception as e:
            print(f"[split-monitor] error: {e}")
        threading.Event().wait(SPLIT_CHECK_INTERVAL)


def merge_split(pid, commit_base=False):
    """Commit each agent's leftover work, then merge the branches into the base
    branch one at a time. Stops at the first conflict with the conflicted files
    named, so the user can send that one agent a fix instead of untangling a
    half-merged tree."""
    with split_lock(pid):
        return _merge_split_locked(pid, commit_base)


def _merge_split_locked(pid, commit_base):
    data = load_split(pid)
    run = data.get("run")
    if not run:
        return _split_error(pid, "No split run to merge.")

    path = (load_meta(pid) or {}).get("path")
    if not path or not is_git_repo(path):
        return _split_error(pid, "Project folder is no longer a git repo.")

    # git merge lands on whatever HEAD currently is. If the working tree moved to
    # another branch since launch, merging would dump every agent's work onto the
    # wrong branch — so require the base branch back before touching anything.
    current = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != run["base_branch"]:
        return _split_error(
            pid,
            f"This split branched off `{run['base_branch']}`, but the project is now on "
            f"`{current}`. Switch back to `{run['base_branch']}` before merging.",
        )

    # A merge into a dirty base would mix the user's own uncommitted work into
    # the merge commit (or just be refused by git), so it's opt-in.
    status = _git(path, "status", "--porcelain")
    base_dirty = [line[3:].strip() for line in status.stdout.splitlines() if line[3:].strip()]
    if base_dirty:
        if not commit_base:
            data["error"] = ""
            data["needs_base_commit"] = base_dirty[:40]
            save_split(pid, data)
            return data
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "Snapshot before merging split branches")
    data["needs_base_commit"] = []

    data["state"] = "merging"
    data["error"] = ""
    save_split(pid, data)

    for entry in run["branches"]:
        if entry["status"] == "merged":
            continue
        wt = entry["worktree"]
        if not os.path.isdir(wt):
            entry["status"] = "empty"
            entry["note"] = "Worktree is gone — nothing to merge."
            continue

        if _git(wt, "status", "--porcelain").stdout.strip():
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", f"split({entry['slug']}): agent work")

        ahead = _git(path, "rev-list", "--count", f"{run['base_branch']}..{entry['branch']}")
        if ahead.stdout.strip() in ("", "0"):
            entry["status"] = "empty"
            entry["note"] = "This agent didn't commit any changes."
            continue

        r = _git(path, "merge", "--no-ff", entry["branch"], "-m",
                 f"Merge split branch {entry['slug']} ({entry['name']})")
        if r.returncode == 0:
            entry["status"] = "merged"
            entry["note"] = ""
            continue

        conflicts = _git(path, "diff", "--name-only", "--diff-filter=U").stdout.split()
        _git(path, "merge", "--abort")
        entry["status"] = "conflict"
        entry["conflicts"] = conflicts[:40]
        entry["note"] = (r.stdout + r.stderr).strip()[:400]
        data["state"] = "conflict"
        save_split(pid, data)
        return data

    run["merged"] = datetime.now().isoformat()
    data["state"] = "merged"
    save_split(pid, data)
    return data


REBASE_PROMPT = """Your branch `{branch}` conflicts with the base branch `{base}` — the other agents' work landed there first, so your changes no longer apply cleanly.

Conflicting files:
{files}

Please: `git fetch` isn't needed (same repo). Run `git merge {base}` here in your worktree, resolve the conflicts by hand — keeping BOTH your work and what's already on {base} — then commit the merge. Tell me "DONE:" when the worktree is clean and your work still does what it should."""


def ask_branch_to_rebase(pid, bid):
    with split_lock(pid):
        data = load_split(pid)
        run = data.get("run") or {}
        entry = next((e for e in run.get("branches", []) if e["id"] == bid), None)
        if not entry:
            return None
        session = split_session_name(pid, bid)
        if not _tmux_has_session(session):
            return None
        files = "\n".join(f"- {f}" for f in entry.get("conflicts", [])) or "(see git status)"
        paste_into_session(session, REBASE_PROMPT.format(
            branch=entry["branch"], base=run.get("base_branch", "main"), files=files))
        entry["status"] = "working"
        if data.get("state") == "conflict":
            data["state"] = "running"
        save_split(pid, data)
        return data


def cleanup_split(pid, delete_branches=False):
    """Tear down the run: kill the agent sessions, remove the worktrees, and file
    the run under history. Branches are kept by default — merged work lives on
    the base branch, but an unmerged branch is the only copy of that agent's
    work, so deleting it is opt-in."""
    with split_lock(pid):
        return _cleanup_split_locked(pid, delete_branches)


def _cleanup_split_locked(pid, delete_branches):
    data = load_split(pid)
    run = data.get("run")
    if not run:
        data.update({"state": "idle", "proposed": [], "error": ""})
        save_split(pid, data)
        return data
    path = (load_meta(pid) or {}).get("path")

    for entry in run["branches"]:
        session = split_session_name(pid, entry["id"])
        if _tmux_has_session(session):
            subprocess.run([TMUX, "kill-session", "-t", session])
        if path and is_git_repo(path):
            _git(path, "worktree", "remove", "--force", entry["worktree"])
            if delete_branches:
                _git(path, "branch", "-D", entry["branch"])
    if path and is_git_repo(path):
        _git(path, "worktree", "prune")

    # Tidy the now-empty run/project scratch dirs. rmdir (not rmtree) on purpose:
    # it fails harmlessly if anything is left, so a worktree that didn't come off
    # cleanly keeps its files rather than being silently deleted.
    if run["branches"]:
        run_dir = Path(run["branches"][0]["worktree"]).parent
        for d in (run_dir, run_dir.parent):
            try:
                d.rmdir()
            except OSError:
                break

    run["closed"] = datetime.now().isoformat()
    run["final_state"] = data.get("state")
    data.setdefault("history", []).insert(0, run)
    data["history"] = data["history"][:10]
    data.update({"state": "idle", "run": None, "proposed": [], "error": "", "needs_base_commit": []})
    save_split(pid, data)
    return data


def kill_split_sessions(pid):
    """Stop a project's agent sessions without tearing the worktrees down — used
    by Close All and server shutdown, where the run should survive to be resumed."""
    run = (load_split(pid) or {}).get("run")
    for entry in (run or {}).get("branches", []):
        session = split_session_name(pid, entry["id"])
        if _tmux_has_session(session):
            subprocess.run([TMUX, "kill-session", "-t", session])


# ── Daily report ─────────────────────────────────────────────────────────────
# Close All already asks every live session for a handoff summary before killing
# it. That's the raw material for a day's report: gather each project touched
# today (the ones just closed, plus any paused earlier or auto-paused by the
# context monitor), render it to a PDF via the Chrome that's already used for
# screenshot tests, and email it as an attachment.

REPORTS_DIR = DATA_DIR.parent / "reports"
EMAIL_CONFIG_PATH = DATA_DIR.parent / ".email_config"


def load_email_config():
    """{smtp_user, smtp_pass, to, smtp_host?, smtp_port?} or None if unconfigured.
    Absent config is a normal state, not an error — the report is still built and
    saved to disk, just not mailed."""
    if not EMAIL_CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(EMAIL_CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return None
    if not cfg.get("smtp_user") or not cfg.get("smtp_pass"):
        return None
    cfg.setdefault("to", cfg["smtp_user"])
    cfg.setdefault("smtp_host", "smtp.gmail.com")
    cfg.setdefault("smtp_port", 465)
    return cfg


def _is_today(iso, today=None):
    if not iso:
        return False
    today = today or datetime.now().date()
    try:
        return datetime.fromisoformat(iso).date() == today
    except (ValueError, TypeError):
        return False


def collect_day_activity(pid, today=None):
    """What this project did today, or None if it did nothing.

    Activity is judged from recorded timestamps only — meta's `last_activity` and
    the message log — never from file mtimes. Every path that writes a session
    summary (Close All, the context-bloat monitor) goes through send_to_session, which
    stamps last_activity, so mtime adds nothing but false positives: restoring a
    backup or touching a file would otherwise resurrect a project into the
    report. Close All additionally passes the sessions it just closed explicitly."""
    today = today or datetime.now().date()
    meta = load_meta(pid)
    if not meta:
        return None

    msgs = load_messages(pid)
    today_msgs = [m for m in msgs if _is_today(m.get("timestamp"), today)]
    touched = bool(today_msgs) or _is_today(meta.get("last_activity"), today)

    if not touched:
        return None

    mem = get_memory(pid)["memory"]
    summary = (mem.get("last_session") or "").strip()

    times = sorted(m["timestamp"] for m in today_msgs if m.get("timestamp"))
    council = load_council(pid)
    council_runs = [r for r in council.get("runs", []) if _is_today(r.get("started"), today)]
    split = load_split(pid)
    splits = [r for r in ([split["run"]] if split.get("run") else []) + split.get("history", [])
              if _is_today(r.get("started"), today)]

    return {
        "id": pid,
        "name": meta.get("name", pid),
        "emoji": meta.get("emoji", "📁"),
        "description": meta.get("description", ""),
        "summary": summary,
        "errors": (mem.get("errors") or "").strip(),
        "messages": len(today_msgs),
        "first": times[0] if times else None,
        "last": times[-1] if times else None,
        "council": {
            "runs": len(council_runs),
            "approved": sum(1 for r in council_runs if r.get("approved")),
        } if council_runs else None,
        "splits": [{
            "branches": [b["name"] for b in s.get("branches", [])],
            "merged": bool(s.get("merged")),
        } for s in splits],
    }


def _fmt_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%-I:%M %p")
    except (ValueError, TypeError):
        return ""


def build_report_html(projects, day=None):
    day = day or datetime.now()
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    total_msgs = sum(p["messages"] for p in projects)

    cards = []
    for p in projects:
        meta_bits = []
        if p["first"] and p["last"]:
            meta_bits.append(f"{_fmt_time(p['first'])} – {_fmt_time(p['last'])}")
        if p["messages"]:
            meta_bits.append(f"{p['messages']} message{'s' if p['messages'] != 1 else ''}")

        extras = ""
        if p["council"]:
            c = p["council"]
            extras += (f"<div class='extra'><b>Council:</b> {c['approved']}/{c['runs']} "
                       f"review{'s' if c['runs'] != 1 else ''} approved</div>")
        for s in p["splits"]:
            extras += (f"<div class='extra'><b>Split:</b> {', '.join(esc(b) for b in s['branches'])}"
                       f" — {'merged' if s['merged'] else 'not merged'}</div>")
        if p["errors"]:
            extras += f"<div class='extra errors'><b>Open problems:</b> {esc(p['errors'][:600])}</div>"

        summary = esc(p["summary"]) or "<i>No summary was saved for this session.</i>"
        cards.append(f"""
      <section class="project">
        <h2><span class="emoji">{esc(p['emoji'])}</span> {esc(p['name'])}</h2>
        <div class="meta">{esc(' · '.join(meta_bits)) or 'No message activity recorded'}</div>
        <div class="summary">{summary}</div>
        {extras}
      </section>""")

    body = "".join(cards) or "<section class='project'><div class='meta'>No project activity recorded today.</div></section>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Daily report</title><style>
  @page {{ size: letter; margin: 16mm 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
         color: #1a1a24; font-size: 11pt; line-height: 1.55; margin: 0; }}
  header {{ border-bottom: 3px solid #7c5cfc; padding-bottom: 12px; margin-bottom: 22px; }}
  h1 {{ font-size: 21pt; margin: 0 0 4px; letter-spacing: -0.4px; }}
  .day {{ color: #6b6b80; font-size: 10.5pt; }}
  .totals {{ margin-top: 10px; font-size: 10.5pt; color: #4a4a5c; }}
  .totals b {{ color: #7c5cfc; }}
  .project {{ page-break-inside: avoid; border: 1px solid #e2e2ec; border-radius: 10px;
              padding: 14px 16px; margin-bottom: 14px; }}
  h2 {{ font-size: 13pt; margin: 0 0 3px; }}
  .emoji {{ margin-right: 5px; }}
  .meta {{ color: #6b6b80; font-size: 9.5pt; margin-bottom: 9px; }}
  .summary {{ white-space: pre-wrap; }}
  .extra {{ margin-top: 9px; padding-top: 8px; border-top: 1px solid #eeeef4;
            font-size: 10pt; color: #4a4a5c; }}
  .extra.errors {{ color: #a13030; }}
  footer {{ margin-top: 20px; color: #9b9bb2; font-size: 9pt;
            border-top: 1px solid #e2e2ec; padding-top: 10px; }}
</style></head><body>
  <header>
    <h1>Daily report</h1>
    <div class="day">{day.strftime('%A, %B %-d, %Y')}</div>
    <div class="totals">
      <b>{len(projects)}</b> project{'s' if len(projects) != 1 else ''} worked on
      · <b>{total_msgs}</b> message{'s' if total_msgs != 1 else ''} exchanged
    </div>
  </header>
  {body}
  <footer>Generated by Claude Manager at {day.strftime('%-I:%M %p')} — summaries are each session's own handoff notes.</footer>
</body></html>"""


def render_pdf(html, out_path):
    """HTML → PDF through the same Chrome the screenshot tests already shell out
    to, so this adds no new dependency."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = out_path.with_suffix(".html")
    src.write_text(html)
    try:
        subprocess.run(
            [CHROME_BIN, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={out_path}", src.as_uri()],
            capture_output=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def send_report_email(pdf_path, projects, day=None):
    """(sent, detail). Never raises — a failed send must not take down Close All."""
    cfg = load_email_config()
    if not cfg:
        return False, f"No email configured ({EMAIL_CONFIG_PATH.name} missing) — report saved to disk only."

    import smtplib
    from email.message import EmailMessage

    day = day or datetime.now()
    names = ", ".join(p["name"] for p in projects) or "no active projects"
    msg = EmailMessage()
    msg["Subject"] = f"Your day in code — {day.strftime('%a %b %-d')}"
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["to"]
    lines = [f"Here's what you got done on {day.strftime('%A, %B %-d')}.", "", f"Projects: {names}", ""]
    for p in projects:
        lines.append(f"— {p['emoji']} {p['name']}")
        if p["summary"]:
            lines.append(f"   {' '.join(p['summary'].split())[:300]}")
        lines.append("")
    lines.append("Full report attached as a PDF.")
    msg.set_content("\n".join(lines))

    try:
        msg.add_attachment(Path(pdf_path).read_bytes(), maintype="application",
                           subtype="pdf", filename=Path(pdf_path).name)
        with smtplib.SMTP_SSL(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=45) as s:
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)
        return True, f"Emailed to {cfg['to']}"
    except Exception as e:
        return False, f"Email failed: {type(e).__name__}: {e}"


def build_and_send_daily_report(extra_pids=None):
    """Gather today's work, render the PDF, mail it. `extra_pids` forces projects
    in even if the activity check misses them — Close All passes the sessions it
    just closed, which are the whole point of the report."""
    today = datetime.now().date()
    forced = set(extra_pids or [])
    projects = []
    for meta in all_projects():
        entry = collect_day_activity(meta["id"], today)
        if entry is None and meta["id"] in forced:
            entry = {**{
                "id": meta["id"], "name": meta.get("name", meta["id"]),
                "emoji": meta.get("emoji", "📁"), "description": meta.get("description", ""),
                "summary": "", "errors": "", "messages": 0, "first": None, "last": None,
                "council": None, "splits": [],
            }}
        if entry:
            projects.append(entry)
    projects.sort(key=lambda p: (p["last"] or ""), reverse=True)

    now = datetime.now()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = REPORTS_DIR / f"daily-report-{now:%Y-%m-%d}.pdf"
    html = build_report_html(projects, now)

    if not projects:
        # Nothing happened today — an empty report in the inbox is just noise.
        detail = "No project activity today — no report sent."
        print(f"[daily-report] {detail}")
        return {"ok": True, "emailed": False, "detail": detail, "projects": 0, "pdf": ""}

    if not render_pdf(html, pdf_path):
        result = {"ok": False, "detail": "Couldn't render the PDF.", "projects": len(projects)}
        print(f"[daily-report] {result['detail']}")
        return result

    sent, detail = send_report_email(pdf_path, projects, now)
    print(f"[daily-report] {len(projects)} project(s) — {detail}")
    return {"ok": True, "emailed": sent, "detail": detail,
            "projects": len(projects), "pdf": str(pdf_path)}


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
    meta["context"] = context_status(pid, meta)
    return jsonify(meta)


@app.route("/api/projects/<pid>/context")
def get_context(pid):
    return jsonify(context_status(pid))


@app.route("/api/projects/<pid>/context/trim", methods=["POST"])
def trim_context_now(pid):
    """Manual trim from the UI: same checkpoint-then-compact as the monitor."""
    if not session_running(pid):
        return jsonify({"started": False, "error": "Session not running"}), 400
    if pid in _trim_state:
        return jsonify({"started": False, "error": "Already trimming"}), 409
    if not session_idle(pid):
        return jsonify({"started": False, "error": "Claude is busy — wait for it to finish"}), 409
    threading.Thread(target=trim_context, args=(pid, "manual"), daemon=True).start()
    return jsonify({"started": True})


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
    # Split agents get no handoff summary — their brief and their branch survive
    # in split.json/git, so a Restart in the Split tab picks the work back up.
    for p in all_projects():
        kill_split_sessions(p["id"])
    return running


@app.route("/api/close-all", methods=["POST"])
def close_all():
    saved = _close_all_sessions()
    # Runs inline rather than in a thread: the summaries it reads were only just
    # written by _close_all_sessions, and the modal wants to show the outcome.
    report = build_and_send_daily_report(extra_pids=[p["id"] for p in saved])
    return jsonify({"ok": True, "saved": [p["id"] for p in saved], "report": report})


@app.route("/api/daily-report", methods=["POST"])
def daily_report():
    """Send today's report without closing anything — for a re-send, or a look at
    the day so far."""
    return jsonify(build_and_send_daily_report())


@app.route("/api/daily-report/preview")
def daily_report_preview():
    """The report as HTML, for checking how it reads before it goes out."""
    today = datetime.now().date()
    projects = [e for e in (collect_day_activity(m["id"], today) for m in all_projects()) if e]
    projects.sort(key=lambda p: (p["last"] or ""), reverse=True)
    return Response(build_report_html(projects), mimetype="text/html")


@app.route("/api/email-config")
def email_config_status():
    cfg = load_email_config()
    return jsonify({"configured": bool(cfg), "to": cfg["to"] if cfg else "",
                    "path": str(EMAIL_CONFIG_PATH)})


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

    paste_into_session(session_name(pid), text, delay=0.1)

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


@app.route("/api/projects/<pid>/split")
def get_split(pid):
    data = load_split(pid)
    path, base_branch, _, err = split_preflight(pid)
    data["can_split"] = err is None
    data["blocker"] = err or ""
    data["base_branch"] = base_branch or data.get("base_branch", "")
    return jsonify(data)


@app.route("/api/projects/<pid>/split/plan", methods=["POST"])
def plan_split_route(pid):
    body = request.get_json() or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Describe the task to split first."}), 400
    count = max(2, min(int(body.get("count") or SPLIT_DEFAULT_BRANCHES), SPLIT_MAX_BRANCHES))
    threading.Thread(target=plan_split, args=(pid, prompt, count), daemon=True).start()
    return jsonify({"status": "planning"})


@app.route("/api/projects/<pid>/split/launch", methods=["POST"])
def launch_split_route(pid):
    body = request.get_json() or {}
    branches = body.get("branches") or []
    prompt = (body.get("prompt") or load_split(pid).get("prompt") or "").strip()
    return jsonify(launch_split(pid, prompt, branches))


@app.route("/api/projects/<pid>/split/merge", methods=["POST"])
def merge_split_route(pid):
    body = request.get_json() or {}
    return jsonify(merge_split(pid, commit_base=bool(body.get("commit_base"))))


@app.route("/api/projects/<pid>/split/cleanup", methods=["POST"])
def cleanup_split_route(pid):
    body = request.get_json() or {}
    return jsonify(cleanup_split(pid, delete_branches=bool(body.get("delete_branches"))))


def _split_branch(pid, bid):
    run = (load_split(pid) or {}).get("run") or {}
    return next((e for e in run.get("branches", []) if e["id"] == bid), None)


@app.route("/api/projects/<pid>/split/branches/<bid>/message", methods=["POST"])
def split_branch_message(pid, bid):
    entry = _split_branch(pid, bid)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    text = ((request.get_json() or {}).get("message") or "").strip()
    if not text:
        return jsonify({"error": "No message"}), 400
    session = split_session_name(pid, bid)
    if not _tmux_has_session(session):
        return jsonify({"error": "This agent's session isn't running"}), 400
    paste_into_session(session, text, delay=0.1)
    return jsonify({"status": "sent"})


@app.route("/api/projects/<pid>/split/branches/<bid>/rebase", methods=["POST"])
def split_branch_rebase(pid, bid):
    data = ask_branch_to_rebase(pid, bid)
    if data is None:
        return jsonify({"error": "Agent session isn't running"}), 400
    return jsonify(data)


@app.route("/api/projects/<pid>/split/branches/<bid>/restart", methods=["POST"])
def split_branch_restart(pid, bid):
    """Bring a stopped agent back up in its existing worktree — its branch still
    holds whatever it got done, so it picks up where it left off."""
    entry = _split_branch(pid, bid)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    if not os.path.isdir(entry["worktree"]):
        return jsonify({"error": "Worktree is gone"}), 400
    spawn_claude_tmux(split_session_name(pid, bid), entry["worktree"])
    with split_lock(pid):
        data = load_split(pid)
        for e in (data.get("run") or {}).get("branches", []):
            if e["id"] == bid:
                e["status"] = "idle"
        save_split(pid, data)
    return jsonify(data)


@app.route("/api/projects/<pid>/split/branches/<bid>/terminal")
def split_branch_terminal(pid, bid):
    """Same live pane mirror as the main Chat tab, pointed at one agent."""
    session = split_session_name(pid, bid)

    def gen():
        prev = ""
        while True:
            try:
                if _tmux_has_session(session):
                    current = capture_session_pane(session)
                    if current != prev:
                        prev = current
                        yield f"data: {json.dumps({'content': current})}\n\n"
                else:
                    yield f"data: {json.dumps({'content': '', 'offline': True})}\n\n"
            except Exception:
                pass
            threading.Event().wait(0.3)

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/projects/<pid>/split/stream")
def split_stream(pid):
    import queue as q_mod
    queue = q_mod.Queue()
    with _lock:
        _split_listeners.setdefault(pid, []).append(queue)

    def gen():
        try:
            yield f"data: {json.dumps(load_split(pid))}\n\n"
            while True:
                try:
                    data = queue.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            with _lock:
                try:
                    _split_listeners[pid].remove(queue)
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
    allowed = {"Up", "Down", "Left", "Right", "Enter", "Tab", "BTab", "Escape", "Space", "BSpace"}
    keys = [k for k in data.get("keys", []) if k in allowed][:30]
    if not keys:
        return jsonify({"ok": False, "error": "no valid keys"}), 400
    session = session_name(pid)
    for i, key in enumerate(keys):
        if i > 0:
            threading.Event().wait(0.06)
        subprocess.run([TMUX, "send-keys", "-t", session, key])
    return jsonify({"ok": True})


@app.route("/api/projects/<pid>/type", methods=["POST"])
def type_text(pid):
    """Forward literal text into the session as it's typed — the raw-terminal
    counterpart to send-keys, used for printable characters and for pastes
    instead of a compose-then-submit box, so the terminal mirror echoes
    keystrokes the same way a real terminal (or `ssh` session) would.

    A single character goes straight through send-keys -l (fast, no buffer
    setup — matters for per-keystroke latency while typing live). Anything
    longer (a paste, possibly multi-line) goes through paste_into_session's
    bracketed-paste path with submit=False so embedded newlines land as text
    instead of each one acting like an early Enter."""
    if not session_running(pid):
        return jsonify({"ok": False}), 400
    text = (request.get_json() or {}).get("text", "")
    if not text:
        return jsonify({"ok": False, "error": "no text"}), 400
    session = session_name(pid)
    if len(text) == 1 and text != "\n":
        subprocess.run([TMUX, "send-keys", "-t", session, "-l", "--", text])
    else:
        paste_into_session(session, text, submit=False)
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
                        payload = {"content": current}
                        cursor = cursor_position(session_name(pid))
                        if cursor:
                            payload["cursor"] = cursor
                        yield f"data: {json.dumps(payload)}\n\n"
                else:
                    yield f"data: {json.dumps({'content': '', 'offline': True})}\n\n"
            except Exception:
                pass
            # Fast poll (was 0.3s) — this interval directly caps how quickly a
            # typed keystroke reflects back in the terminal mirror; at 0.3s,
            # live typing felt laggy even though the keystroke itself landed
            # in tmux almost immediately.
            threading.Event().wait(0.05)

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
    splits = [p for p in all_projects() if (load_split(p["id"]).get("run") or {}).get("branches")]
    if not running and not splits:
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
    threading.Thread(target=context_monitor, daemon=True).start()
    threading.Thread(target=blocked_monitor, daemon=True).start()
    threading.Thread(target=split_monitor, daemon=True).start()
    _host = os.environ.get("HOST", "0.0.0.0")
    _port = int(os.environ.get("PORT", "8888"))
    _cert, _key = os.environ.get("SSL_CERT"), os.environ.get("SSL_KEY")
    _ssl = (_cert, _key) if _cert and _key and os.path.isfile(_cert) and os.path.isfile(_key) else None
    print(f"Claude Manager backend running on {'https' if _ssl else 'http'}://{_host}:{_port}", flush=True)
    app.run(host=_host, port=_port, debug=False, threaded=True, ssl_context=_ssl)
