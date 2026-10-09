import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from core import CLAUDE_BIN, broadcast_council, load_meta, project_dir
from tmuxlib import session_running
from messaging import append_message, load_messages, send_to_session
from push import notify_macos


REVIEW_MAX_ROUNDS = 3
_review_inflight = set()
_review_lock = threading.Lock()
REVIEW_DECISION_RE = re.compile(r"REVIEW\s*[:\-]?\s*(DONE|AGAIN)\b", re.I)

REVIEWER_PROMPT = """You are reviewing a code change just made to this project. Don't edit any files.

Read the diff below and the surrounding code it touches. Look only for real defects: incorrect behavior, broken edge cases, regressions, security problems. If the project has a test/build/lint command, you may run it. Do NOT report style preferences, hypothetical refactors, missing tests, or anything you can't point to concretely. A sound change should get APPROVE, and approving is the expected outcome most of the time — don't invent findings to justify a review.

Diff of what changed:
```
{diff}
```

Reply with your verdict as the FIRST line, exactly "APPROVE" or "REJECT". If REJECT, follow with at most 3 findings, most severe first, one short paragraph each: what's wrong, where (file/function), and how you know."""

FIX_PROMPT = """A reviewer looked at your last change and reported the findings below. Fix whatever is genuinely a problem. If a finding is wrong or not worth changing, say why instead of changing code — you have the final call.

{findings}

When you're finished, end your reply with exactly one of these lines:
REVIEW: DONE  — the change is complete and needs no further review
REVIEW: AGAIN — you changed enough that the reviewer should look once more"""


def council_path(pid):
    return project_dir(pid) / "council.json"


def load_council(pid):
    p = council_path(pid)
    if p.exists():
        return json.loads(p.read_text())
    return {"state": "idle", "last_signature": None, "runs": []}


def save_council(pid, data):
    council_path(pid).write_text(json.dumps(data, indent=2))
    broadcast_council(pid, data)


def is_git_repo(path):
    return (Path(path) / ".git").exists()


def _git(path, *args):
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=20)


def _compute_diff_signature(path):
    """Everything currently different from HEAD — tracked-file diff plus a
    preview of untracked new files — so a run captures uncommitted work too,
    since Claude Code doesn't commit on its own."""
    tracked = _git(path, "diff", "HEAD").stdout
    status = _git(path, "status", "--porcelain").stdout
    untracked = [line[3:] for line in status.splitlines() if line.startswith("??")]
    preview = ""
    for f in untracked[:20]:
        fp = Path(path) / f
        try:
            if fp.is_file() and fp.stat().st_size < 20000:
                preview += f"\n--- new file: {f} ---\n{fp.read_text(errors='ignore')}\n"
        except Exception:
            pass
    full = tracked + preview
    signature = hashlib.sha256(full.encode()).hexdigest()
    return full, signature


def _run_reviewer(path, diff_text):
    """One independent `claude -p` pass over the diff. Returned in the same
    shape the old per-tester records used (name/verdict/reason) so existing
    council.json history and the Council tab keep rendering."""
    prompt = REVIEWER_PROMPT.format(diff=diff_text[:12000])
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=path, capture_output=True, text=True, timeout=240,
        )
        out = result.stdout.strip()
    except Exception as e:
        return {"name": "Reviewer", "verdict": "ERROR", "reason": str(e)}
    lines = out.splitlines()
    first = lines[0].strip().upper() if lines else ""
    verdict = "APPROVE" if first.startswith("APPROVE") else "REJECT"
    reason = "\n".join(lines[1:]).strip() or out
    return {"name": "Reviewer", "verdict": verdict, "reason": reason[:4000]}


def run_council(pid):
    """Entry point called after a chat turn completes. No-ops unless the
    project is a git repo with a real, not-yet-reviewed change, and skips if
    a review is already in flight for this project."""
    meta = load_meta(pid)
    if not meta:
        return
    path = meta.get("path")
    if not path or not os.path.isdir(path) or not is_git_repo(path):
        return

    # In-flight tracking lives in memory, not in council.json: the file's
    # "running_council"/"fixing" state survives a server restart mid-run, and
    # trusting it left projects stuck in "reviewing…" forever with every later
    # review silently skipped (seen on a real project, stale since 2026-07-31).
    with _review_lock:
        if pid in _review_inflight:
            return
        _review_inflight.add(pid)
    try:
        council = load_council(pid)
        for run in council.get("runs", []):
            if not run.get("finished"):
                run["finished"] = datetime.now().isoformat()
                run["aborted"] = True  # orphaned by a restart; shown as "interrupted"

        diff_text, signature = _compute_diff_signature(path)
        if not diff_text.strip() or signature == council.get("last_signature"):
            if council.get("state") in ("running_council", "fixing"):
                council["state"] = "idle"
                save_council(pid, council)
            return

        council["last_signature"] = signature
        _review_loop(pid, path, diff_text, council, attempt=1)
    finally:
        with _review_lock:
            _review_inflight.discard(pid)


def _finish(pid, council, state, name, message):
    council["state"] = state
    save_council(pid, council)
    notify_macos(name, message)


def _review_loop(pid, path, diff_text, council, attempt):
    name = (load_meta(pid) or {}).get("name", pid)
    run = {
        "id": uuid.uuid4().hex[:8],
        "attempt": attempt,
        "started": datetime.now().isoformat(),
        "finished": None,
        "diff_preview": diff_text[:4000],
        "testers": [],
        "approved": None,
        "author": None,  # {"decision": "DONE"|"AGAIN"|"NONE", "reply": str} once the author has answered
    }
    council["state"] = "running_council"
    council.setdefault("runs", []).append(run)
    save_council(pid, council)

    reviewer = _run_reviewer(path, diff_text)
    run["testers"] = [reviewer]
    run["approved"] = reviewer["verdict"] == "APPROVE"
    run["finished"] = datetime.now().isoformat()

    if run["approved"]:
        _finish(pid, council, "approved", name, "✅ Reviewer approved the latest change")
        return
    if reviewer["verdict"] == "ERROR" or not session_running(pid):
        _finish(pid, council, "gave_up", name, "⚠️ Review couldn't complete — see Council tab")
        return

    # Hand the findings to the author. It fixes what it agrees with and then
    # decides whether the loop continues.
    council["state"] = "fixing"
    save_council(pid, council)

    fix_prompt = FIX_PROMPT.format(findings=reviewer["reason"])
    msgs = load_messages(pid)
    next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
    reply_id = next_id + 1
    append_message(pid, {"id": next_id, "role": "user", "text": "[Review feedback]\n" + fix_prompt, "status": "done", "timestamp": datetime.now().isoformat()})
    append_message(pid, {"id": reply_id, "role": "assistant", "text": "Addressing review feedback...", "status": "thinking", "timestamp": datetime.now().isoformat()})
    reply = send_to_session(pid, fix_prompt, reply_id, name, notify=False) or ""

    m = REVIEW_DECISION_RE.search(reply)
    decision = m.group(1).upper() if m else "NONE"
    run["author"] = {"decision": decision, "reply": reply[-2000:]}

    # Whatever the author did counts as reviewed: record the new tree state so
    # the next chat turn doesn't re-review the fix itself.
    new_diff, council["last_signature"] = _compute_diff_signature(path)

    if decision != "AGAIN":
        # DONE, or no explicit decision at all — either way the author didn't
        # ask for another pass, and defaulting to "stop" is what keeps this
        # loop finite when the reply is malformed.
        suffix = "" if decision == "DONE" else " (no explicit decision in its reply)"
        _finish(pid, council, "done", name, f"✔ Author addressed the review and closed it{suffix}")
        return
    if attempt >= REVIEW_MAX_ROUNDS:
        _finish(pid, council, "gave_up", name, f"⚠️ Review still open after {attempt} rounds — see Council tab")
        return
    if not new_diff.strip():
        _finish(pid, council, "done", name, "✔ Review closed — nothing left to re-review")
        return
    _review_loop(pid, path, new_diff, council, attempt + 1)


# ── Split runs (parallel mini-agents, one git worktree each) ─────────────────
# The normal chat drives ONE session through a task sequentially. For a big task
# that decomposes cleanly, a split instead fans it out: a planner reads the repo
# and proposes independent pieces, the user edits/approves them, and each piece
# gets its own git worktree + branch + Claude Code tmux session. Every agent is a
# full interactive session — same terminal mirror and input box as Chat — so the
# user can correct any one of them mid-flight without touching the others. When
# they're done the branches are merged back into the base branch together.
