import json
import mimetypes
import os
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from flask import jsonify, request, Response, send_file
from core import CLAUDE_BIN, PROJECTS_BASE_DIR, all_projects, app, init_project_dir, load_meta, project_dir, resolve_project_path, save_meta
from tmuxlib import dev_server_running, kill_session, open_terminal_window, paste_into_session, session_name, session_running, start_dev_server, start_session, wait_for_claude_ready
from messaging import _do_send_message, clear_unread, load_messages, wait_for_stable_reply
from push import notify_needs_response
from context import _save_session_summary, _trim_state, context_status, get_running_sessions, session_idle, trim_context
from memory import build_context_prompt, get_memory
from council import run_council
from split import kill_split_sessions
from reports import EMAIL_CONFIG_PATH, build_and_send_daily_report, build_report_html, collect_day_activity, load_email_config


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
            # A fixed sleep lost the prompt whenever startup ran slow (cold tmux,
            # Claude Code self-updating): wait for the TUI to paint its input box.
            wait_for_claude_ready(session_name(pid))
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
        notify_needs_response(pid, name, reply)
        m = load_meta(pid)
        if m:
            m["last_activity"] = datetime.now().isoformat()
            m["unread"] = m.get("unread", 0) + 1
            save_meta(pid, m)
        run_council(pid)

    threading.Thread(target=_watch, daemon=True).start()
    return jsonify({"status": "sent"})
