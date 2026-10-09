import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from core import TMUX, all_projects, load_meta, save_meta
from tmuxlib import CLAUDE_READY_RE, capture_pane, capture_session_pane, paste_into_session, session_name, session_running
from messaging import append_message, load_messages, send_to_session
from push import notify_macos
from blocked import blocked_status
from memory import set_memory
from council import _review_inflight


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
