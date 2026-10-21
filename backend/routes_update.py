"""Profile tab: update Claude Code and Agent Capital itself from the UI."""
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from flask import jsonify
from core import CLAUDE_BIN, all_projects, app, init_project_dir, save_meta

REPO = Path(__file__).resolve().parent.parent
_busy = threading.Lock()


def _run(cmd, cwd=None, timeout=300):
    """Returns (ok, combined output)."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _git(*args, timeout=30):
    return _run(["git", *args], cwd=REPO, timeout=timeout)


@app.route("/api/update/status")
def update_status():
    _, claude = _run([CLAUDE_BIN, "--version"], timeout=10)
    _git("fetch", "--quiet")
    ok, behind = _git("rev-list", "--count", "HEAD..@{u}")
    _, head = _git("log", "-1", "--format=%h %s")
    _, dirty = _git("status", "--porcelain")
    return jsonify({
        "claude_version": claude,
        "app_head": head,
        "app_behind": int(behind) if ok and behind.isdigit() else None,
        "app_dirty": bool(dirty),
    })


@app.route("/api/update/claude", methods=["POST"])
def update_claude():
    if not _busy.acquire(blocking=False):
        return jsonify({"error": "An update is already running"}), 409
    try:
        ok, out = _run([CLAUDE_BIN, "update"], timeout=300)
        return jsonify({"ok": ok, "output": out}), (200 if ok else 500)
    finally:
        _busy.release()


@app.route("/api/update/app", methods=["POST"])
def update_app():
    if not _busy.acquire(blocking=False):
        return jsonify({"error": "An update is already running"}), 409
    try:
        log = []
        steps = [
            ("git pull", ["git", "pull", "--ff-only"], REPO),
            ("pip install", [sys.executable, "-m", "pip", "install", "-q", "-r", "backend/requirements.txt"], REPO),
        ]
        if shutil.which("npm"):
            steps += [("npm install", ["npm", "install", "--no-audit", "--no-fund"], REPO / "frontend"),
                      ("npm run build", ["npm", "run", "build"], REPO / "frontend")]
        for name, cmd, cwd in steps:
            ok, out = _run(cmd, cwd=cwd, timeout=600)
            log.append(f"$ {name}\n{out}")
            if not ok:
                return jsonify({"ok": False, "output": "\n\n".join(log)}), 500
        # Exit without the shutdown handler (it would close every Claude session);
        # launchd (KeepAlive) / systemd (Restart=always) bring the server back up.
        # ponytail: relies on a supervisor; run by hand and it just stops.
        threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()
        return jsonify({"ok": True, "restarting": True, "output": "\n\n".join(log)})
    finally:
        _busy.release()


@app.route("/api/update/self-project", methods=["POST"])
def self_project():
    """Find (or create) the project whose folder is this repo, so Agent Capital
    can be edited by a normal Claude session in its own UI."""
    for p in all_projects():
        if Path(p.get("path", "")).resolve() == REPO:
            return jsonify(p)
    pid = uuid.uuid4().hex[:8]
    meta = {
        "id": pid, "name": "Agent Capital", "emoji": "",
        "description": "This app's own code. Changes go live via Profile -> Update Agent Capital.",
        "path": str(REPO), "preview_url": "", "server_cmd": "",
        "created": datetime.now().isoformat(), "status": "stopped",
    }
    init_project_dir(pid)
    save_meta(pid, meta)
    return jsonify(meta)
