import json
import os
import threading
from flask import jsonify, request, Response
from core import _council_listeners, _lock, _split_listeners, app, load_meta
from tmuxlib import _tmux_has_session, capture_session_pane, paste_into_session, spawn_claude_tmux
from council import is_git_repo, load_council, run_council
from split import SPLIT_DEFAULT_BRANCHES, SPLIT_MAX_BRANCHES, ask_branch_to_rebase, cleanup_split, launch_split, load_split, merge_split, plan_split, save_split, split_lock, split_preflight, split_session_name


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
