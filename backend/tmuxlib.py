import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from core import CLAUDE_BIN, IS_MACOS, TMUX


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


ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


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
        # Ink takes longer to absorb a long paste; an Enter that arrives before
        # it's done is swallowed and the text sits unsent in the input box.
        threading.Event().wait(max(delay, min(3.0, len(message) / 1500)))
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
    `tmux attach -t <session>` if you are at the server's shell.
    AGENT_CAPITAL_TERMINAL_WINDOWS=0 turns this off on a Mac used as a server."""
    if not IS_MACOS or os.environ.get("AGENT_CAPITAL_TERMINAL_WINDOWS") == "0":
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
