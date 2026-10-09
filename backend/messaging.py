import json
import re
import threading
from datetime import datetime
from core import broadcast, load_meta, project_dir, save_meta
from tmuxlib import capture_pane, pane_body, paste_into_session, session_name, session_running
from push import notify_needs_response


def extract_reply(content, message):
    lines = content.splitlines()
    msg_short = message[:40].lower()
    msg_idx = -1
    for i, line in enumerate(lines):
        if msg_short in line.lower():
            msg_idx = i

    if msg_idx == -1:
        return ""

    response_lines = []
    for line in lines[msg_idx + 1:]:
        stripped = line.strip()
        if stripped in ("❯", ">", "❯ "):
            break
        if set(stripped) <= set("─—-="):
            continue
        cleaned = re.sub(r'^[⏺⎿⏵\s]+', '', stripped).strip()
        if cleaned:
            response_lines.append(cleaned)

    text = "\n".join(response_lines).strip()
    return re.sub(r'\n{3,}', '\n\n', text)


# ── Chat helpers ──────────────────────────────────────────────────────────────

def messages_path(pid):
    return project_dir(pid) / "messages.json"


def load_messages(pid):
    p = messages_path(pid)
    return json.loads(p.read_text()) if p.exists() else []


def save_messages(pid, msgs):
    messages_path(pid).write_text(json.dumps(msgs, indent=2))


def append_message(pid, msg):
    msgs = load_messages(pid)
    msgs.append(msg)
    save_messages(pid, msgs)
    broadcast(pid, msg)
    return msg


def update_message(pid, msg_id, updates):
    msgs = load_messages(pid)
    for m in msgs:
        if m["id"] == msg_id:
            m.update(updates)
    save_messages(pid, msgs)
    updated = next((m for m in msgs if m["id"] == msg_id), None)
    if updated:
        broadcast(pid, updated)
    return updated


def mark_unread(pid):
    meta = load_meta(pid)
    if meta:
        meta["unread"] = meta.get("unread", 0) + 1
        save_meta(pid, meta)


def clear_unread(pid):
    meta = load_meta(pid)
    if meta and meta.get("unread"):
        meta["unread"] = 0
        save_meta(pid, meta)


def wait_for_stable_reply(pid, message):
    prev = ""
    stable = 0
    current = ""
    for _ in range(240):
        threading.Event().wait(0.5)
        current = capture_pane(pid)
        body = pane_body(current)
        if body == prev:
            stable += 1
            if stable >= 4:
                break
        else:
            stable = 0
        prev = body

    reply = extract_reply(current, message)
    return reply or "(Response visible in terminal)"


def send_to_session(pid, message, reply_id, project_name, notify=True):
    if not session_running(pid):
        update_message(pid, reply_id, {"text": "Session not running. Start it first.", "status": "done"})
        return

    paste_into_session(session_name(pid), message)
    threading.Event().wait(2)

    text = wait_for_stable_reply(pid, message)
    update_message(pid, reply_id, {"text": text, "status": "done"})
    if notify:
        notify_needs_response(pid, project_name, text)
        mark_unread(pid)

    meta = load_meta(pid)
    if meta:
        meta["last_activity"] = datetime.now().isoformat()
        save_meta(pid, meta)
    return text


# ── Context-bloat monitor ─────────────────────────────────────────────────────
# A long-running session accumulates tool output, dead ends and stale file
# contents until Claude Code's own auto-compaction fires blind (nothing gets
# saved first) or the model just gets slow and forgetful. This watches each
# running session's live context size and, once it's bloated, has the agent
# checkpoint what matters into project memory and then /compact with explicit
# instructions about what to keep.
#
# Where the numbers come from: Claude Code writes every session's transcript to
# ~/.claude/projects/<cwd with non-alphanumerics → "-">/<session-id>.jsonl, and
# each assistant entry carries the API usage for that turn. input + cache
# creation + cache read tokens is exactly the context that was sent on the
# most recent turn — i.e. the live context size. Verified against a real
# transcript on this Mac (2026-09-06).
#
# Replaced the usage-limit watchdog (2026-09-06): the upgraded plan doesn't hit
# limits, and bloat, not quota, is what actually degrades long sessions.

def _do_send_message(pid, text, notify=True):
    msgs = load_messages(pid)
    next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
    meta = load_meta(pid) or {}

    user_msg = {"id": next_id, "role": "user", "text": text, "status": "done", "timestamp": datetime.now().isoformat()}
    reply_msg = {"id": next_id + 1, "role": "assistant", "text": "Thinking...", "status": "thinking", "timestamp": datetime.now().isoformat()}

    append_message(pid, user_msg)
    append_message(pid, reply_msg)

    threading.Thread(target=send_to_session, args=(pid, text, reply_msg["id"], meta.get("name", pid), notify), daemon=True).start()
    return user_msg
