import json
import subprocess
import threading
from flask import jsonify, request, Response
from core import TMUX, _lock, _sse_listeners, app
from tmuxlib import ANSI_RE, cursor_position, paste_into_session, session_name, session_running


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
    """Stream live tmux pane content — only sends when content changes.
    ?slow=1: the xterm.js view draws the screen itself, so this feed only
    feeds select-prompt detection and can poll gently."""
    interval = 0.5 if request.args.get("slow") else 0.05

    def gen():
        prev = ""
        while True:
            try:
                # One tmux spawn per tick while idle: a failed capture-pane
                # already means "no session", so no separate has-session probe.
                # (Each spawn competes with the keystroke send-keys calls.)
                session = session_name(pid)
                r = subprocess.run(
                    [TMUX, "capture-pane", "-t", session, "-p", "-S", "-500"],
                    capture_output=True, text=True
                )
                if r.returncode == 0:
                    current = ANSI_RE.sub('', r.stdout)
                    if current != prev:
                        prev = current
                        payload = {"content": current}
                        cursor = cursor_position(session)
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
            threading.Event().wait(interval)

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
