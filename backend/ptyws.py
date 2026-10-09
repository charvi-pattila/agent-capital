import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import termios
import threading
from flask_sock import Sock
from core import TMUX, app
from tmuxlib import session_name, session_running

sock = Sock(app)

# Must match spawn_claude_tmux: the window rests at a fixed tall 120x200 so
# capture-pane sees plenty of history. While a browser is attached the window
# takes that browser's size (like any terminal; a tall pane would show as a wall
# of blank rows), and goes back when the last browser leaves. Capture-based
# features (reply extraction, blocked detection) see the smaller pane meanwhile.
PANE_COLS, PANE_ROWS = 120, 200
_clients = {}  # session -> open browser attachments
_clients_lock = threading.Lock()


def _winsize(master, cols, rows):
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _resize(session, master, proc, cols, rows):
    subprocess.run([TMUX, "resize-window", "-t", session, "-x", str(cols), "-y", str(rows)])
    _winsize(master, cols, rows)
    # The pty isn't the client's controlling terminal, so the kernel won't
    # send SIGWINCH itself; without it tmux keeps drawing for the old size.
    proc.send_signal(signal.SIGWINCH)


@sock.route("/api/projects/<pid>/pty")
def pty_socket(ws, pid):
    """Raw terminal for xterm.js: a pty running `tmux attach`, bytes both ways.
    Binary frames = keystrokes in / screen bytes out; text frames = JSON
    control ({"type": "resize", "cols": N, "rows": N})."""
    if not session_running(pid):
        ws.close()
        return
    session = session_name(pid)
    master, slave = os.openpty()
    _winsize(master, PANE_COLS, 40)  # before tmux starts, so it never sees 80x24
    proc = subprocess.Popen(
        [TMUX, "attach-session", "-t", session],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    with _clients_lock:
        _clients[session] = _clients.get(session, 0) + 1
    # The status line would take a row from the browser's screen; hide it
    # while attached (restored when the last browser leaves).
    subprocess.run([TMUX, "set-option", "-t", session, "status", "off"])
    _resize(session, master, proc, PANE_COLS, 40)

    def pump():
        try:
            while True:
                if not select.select([master], [], [], 1)[0]:
                    if proc.poll() is not None:
                        break
                    continue
                data = os.read(master, 65536)
                if not data:
                    break
                ws.send(data)
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass

    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            msg = ws.receive()
            if isinstance(msg, str):
                ctl = json.loads(msg)
                if ctl.get("type") == "resize":
                    _resize(session, master, proc, max(20, min(int(ctl["cols"]), 300)),
                            max(5, min(int(ctl["rows"]), 200)))
            elif msg:
                os.write(master, msg)
    except Exception:
        pass  # closed socket / dead pty: fall through to cleanup
    finally:
        proc.terminate()  # detaches this client only; the tmux session lives on
        try:
            os.close(master)
        except OSError:
            pass
        proc.wait()
        with _clients_lock:
            _clients[session] -= 1
            last = _clients[session] == 0
        if last and session_running(pid):
            subprocess.run([TMUX, "set-option", "-t", session, "-u", "status"])
            subprocess.run([TMUX, "resize-window", "-t", session, "-x", str(PANE_COLS), "-y", str(PANE_ROWS)])
