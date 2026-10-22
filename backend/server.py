import atexit
import os
import signal
import sys
import threading
from core import all_projects, app
from context import context_monitor, get_running_sessions
from blocked import blocked_monitor
from split import load_split, split_monitor
from routes_projects import _close_all_sessions
from tmuxlib import dev_server_keeper

# Importing the route modules registers their @app.route handlers.
import routes_agents, routes_data, routes_terminal, ptyws  # noqa: F401,E401
import routes_update  # noqa: F401
import routes_site  # noqa: F401  (last: owns the catch-all frontend route)


_shutdown_done = False


def _shutdown_once():
    """Kill every running Claude tmux session (apps keep running) when the app
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
    threading.Thread(target=dev_server_keeper, daemon=True).start()
    _host = os.environ.get("HOST", "0.0.0.0")
    _port = int(os.environ.get("PORT", "8888"))
    _cert, _key = os.environ.get("SSL_CERT"), os.environ.get("SSL_KEY")
    _ssl = (_cert, _key) if _cert and _key and os.path.isfile(_cert) and os.path.isfile(_key) else None
    print(f"Agent Capital backend running on {'https' if _ssl else 'http'}://{_host}:{_port}", flush=True)
    app.run(host=_host, port=_port, debug=False, threaded=True, ssl_context=_ssl)
