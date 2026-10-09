import json
import threading
import uuid
from flask import jsonify, request, Response, send_file
from core import _lock, _running_listeners, app
from context import get_running_sessions
from memory import get_global_memory, get_memory, set_global_memory, set_memory
from testrunner import load_tests, run_tests_for_project, save_tests, screenshot_test_dir


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
