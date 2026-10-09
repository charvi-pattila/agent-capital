import re
import threading
from datetime import datetime
from core import _lock, _pane_watch, all_projects
from tmuxlib import capture_pane, pane_body, session_running
from push import notify_phone


BLOCKED_CHECK_INTERVAL = 15  # seconds
IDLE_THRESHOLD = 90          # seconds unchanged before flagging as blocked
PROMPT_PUSH_THRESHOLD = 20   # a y/n or menu prompt sitting this long is waiting on the user: push now

# Common Claude Code CLI confirmation/choice prompts — matching one gives a more
# specific reason than the generic idle fallback.
BLOCKED_PROMPT_RE = re.compile(
    r"do you want to (?:proceed|continue)|\(y/n\)|❯\s*1\.|press enter to continue",
    re.I,
)


def _prompt_line(body, match):
    needle = match.group(0).lower()
    return next((l.strip() for l in body.splitlines() if needle in l.lower()), match.group(0))


def _blocked_tick():
    """Track each running pane; also the phone-alert trigger. Every distinct
    pane state gets at most one push: a recognised prompt after
    PROMPT_PUSH_THRESHOLD, anything else idle after IDLE_THRESHOLD. Replies
    sent through the chat are announced by notify_needs_response() the moment
    they finish, which marks the state pushed so it isn't repeated here."""
    now = datetime.now().timestamp()
    seen = set()
    due = []  # (pid, name, detail) — pushed outside the lock
    for meta in all_projects():
        pid = meta["id"]
        if not session_running(pid):
            continue
        seen.add(pid)
        body = pane_body(capture_pane(pid))
        with _lock:
            prev = _pane_watch.get(pid)
            if prev is None or prev["body"] != body:
                _pane_watch[pid] = {"body": body, "changed_at": now, "pushed": False}
                continue
            # unchanged — leave changed_at as-is, it keeps aging
            if prev.get("pushed"):
                continue
            idle_for = now - prev["changed_at"]
            prompt = BLOCKED_PROMPT_RE.search(body)
            if prompt and idle_for >= PROMPT_PUSH_THRESHOLD:
                prev["pushed"] = True
                due.append((pid, meta.get("name", pid), _prompt_line(body, prompt)))
            elif idle_for >= IDLE_THRESHOLD:
                prev["pushed"] = True
                due.append((pid, meta.get("name", pid), "idle, waiting for your next instruction"))

    with _lock:
        for pid in list(_pane_watch):
            if pid not in seen:
                del _pane_watch[pid]  # session no longer running — clear stale state

    for pid, name, detail in due:
        notify_phone(name, detail, pid)


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
        return "blocked", f"Waiting on a prompt: {_prompt_line(watch['body'], m)[:120]}"
    return "blocked", "Idle — may be waiting for input"


def blocked_monitor():
    while True:
        try:
            _blocked_tick()
        except Exception as e:
            print(f"[blocked-monitor] error: {e}")
        threading.Event().wait(BLOCKED_CHECK_INTERVAL)


# ── Memory helpers ────────────────────────────────────────────────────────────
