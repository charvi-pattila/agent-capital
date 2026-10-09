import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from core import CLAUDE_BIN, TMUX, _lock, all_projects, broadcast_split, load_meta, project_dir, slugify
from tmuxlib import _tmux_has_session, capture_session_pane, pane_body, paste_into_session, spawn_claude_tmux, wait_for_claude_ready
from council import _git, is_git_repo


SPLIT_ROOT = Path.home() / ".agent-capital-splits"
SPLIT_MAX_BRANCHES = 6
SPLIT_DEFAULT_BRANCHES = 3
SPLIT_CHECK_INTERVAL = 8    # seconds between agent-status sweeps
SPLIT_IDLE_THRESHOLD = 45   # seconds of unchanged pane before an agent reads as idle

# Statuses the status sweep is allowed to overwrite. Merge outcomes
# (merged/conflict/empty) are decisions, not observations — they stick until the
# next merge attempt re-decides them.
SPLIT_LIVE_STATUSES = ("starting", "working", "idle", "stopped")

SPLIT_PLAN_PROMPT = """You are planning how to split one large task across independent Claude Code agents that will work in PARALLEL — each in its own git worktree on its own branch of this project — whose branches are all merged back together at the end.

The task:
---
{prompt}
---

First read enough of this project to understand how it's actually structured. Then split the task into independent pieces. Rules:
- Each piece must be workable on its own, right now, without waiting on another piece.
- Minimize file overlap. Two agents editing the same file means a merge conflict. If some shared file (a router, an index, a schema, a config) has to change, assign it to exactly ONE piece and tell the others not to touch it.
- Aim for {count} pieces, but use fewer if the task doesn't honestly divide that far. Never more than {max}.
- Each "task" is the ONLY thing its agent will be told, besides the repo itself. Write it as complete standalone instructions: what to build, where it goes, and how it meets the other pieces (names of functions/props/endpoints it should expose or assume).

Reply with ONLY a JSON array and nothing else — no prose, no markdown fence:
[{{"name": "short-kebab-name", "task": "full standalone instructions for this agent", "files": ["likely/path.js"]}}]"""

SPLIT_AGENT_PROMPT = """You are agent {n} of {total} working in PARALLEL with other Claude Code agents on one larger task. You're in your own git worktree on branch `{branch}`; the others are in their own worktrees on their own branches, and all the branches get merged together once everyone is done.

The overall goal, for context only:
{overall}

YOUR piece — the only piece you should implement:
{task}
{files_line}
Rules:
- Stay inside your piece. Do not implement the other agents' pieces, and avoid editing files outside yours: {others}
- The other agents' work will NOT appear in your worktree. Don't wait for it. If your piece needs something they're building, code against the interface described above and move on.
- Commit your work on this branch when it's done: `git add -A && git commit -m "..."` with a short message.
- Finish by printing one line starting with "DONE:" and a one-sentence summary."""


def split_path(pid):
    return project_dir(pid) / "split.json"


# Unlike the other state files, split.json has genuinely concurrent writers: the
# status sweep, the agent-launch thread, and whatever the user just clicked. So
# writes go through a temp file + atomic rename (a reader can never catch a
# half-written file), and every read-modify-write holds the project's lock so one
# update can't silently clobber another's.
_split_locks = {}
_split_locks_guard = threading.Lock()


def split_lock(pid):
    with _split_locks_guard:
        return _split_locks.setdefault(pid, threading.RLock())


def load_split(pid):
    p = split_path(pid)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"state": "idle", "run": None, "history": []}


def save_split(pid, data):
    p = split_path(pid)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)
    broadcast_split(pid, data)


def split_session_name(pid, bid):
    return f"claude_split_{pid}_{bid}"


def split_run_dir(pid, run_id):
    meta = load_meta(pid) or {}
    return SPLIT_ROOT / f"{slugify(meta.get('name', pid))}-{pid}" / run_id


def _split_error(pid, message):
    with split_lock(pid):
        data = load_split(pid)
        data["state"] = "idle" if not data.get("run") else data["state"]
        data["error"] = message
        save_split(pid, data)
        return data


def split_preflight(pid):
    """(path, base_branch, base_sha, error). A split needs a git repo with at
    least one commit — worktrees branch off HEAD."""
    meta = load_meta(pid) or {}
    path = meta.get("path")
    if not path or not os.path.isdir(path):
        return None, None, None, "Project folder not found."
    if not is_git_repo(path):
        return None, None, None, "This project isn't a git repo. Run `git init` in the project folder to use splits."
    head = _git(path, "rev-parse", "HEAD")
    if head.returncode != 0:
        return None, None, None, "This repo has no commits yet. Make one commit, then split."
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    return path, branch, head.stdout.strip(), None


def _parse_plan_json(out):
    """Pull the JSON array out of a planner reply, tolerating a stray fence or
    a sentence of preamble."""
    text = out.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    branches = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        task = str(item.get("task") or "").strip()
        if not name or not task:
            continue
        files = [str(f) for f in (item.get("files") or []) if isinstance(f, (str, int))][:12]
        branches.append({"name": name, "task": task, "files": files})
    return branches[:SPLIT_MAX_BRANCHES]


def plan_split(pid, prompt, count):
    path, base_branch, base_sha, err = split_preflight(pid)
    if err:
        return _split_error(pid, err)

    with split_lock(pid):
        data = load_split(pid)
        data.update({"state": "planning", "error": "", "prompt": prompt, "run": None})
        save_split(pid, data)

    planner = SPLIT_PLAN_PROMPT.format(prompt=prompt, count=count, max=SPLIT_MAX_BRANCHES)
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", planner, "--dangerously-skip-permissions"],
            cwd=path, capture_output=True, text=True, timeout=420,
        )
        branches = _parse_plan_json(result.stdout)
    except Exception as e:
        return _split_error(pid, f"Planner failed: {e}")

    if not branches:
        return _split_error(pid, "The planner didn't return a usable split. Try rewording the task.")

    with split_lock(pid):
        data = load_split(pid)
        data.update({
            "state": "planned",
            "error": "",
            "prompt": prompt,
            "proposed": branches,
            "base_branch": base_branch,
            "base_sha": base_sha,
        })
        save_split(pid, data)
        return data


def _unique_slugs(names):
    slugs, seen = [], set()
    for name in names:
        base = slugify(name)[:32] or "branch"
        slug, n = base, 2
        while slug in seen:
            slug = f"{base}-{n}"
            n += 1
        seen.add(slug)
        slugs.append(slug)
    return slugs


def launch_split(pid, prompt, branches):
    path, base_branch, base_sha, err = split_preflight(pid)
    if err:
        return _split_error(pid, err)

    # One run at a time. Overwriting a live run would strand its worktrees and
    # tmux sessions with nothing left pointing at them to clean up.
    if (load_split(pid) or {}).get("run"):
        return _split_error(pid, "A split run is already open — close it before starting another.")

    branches = [b for b in branches if (b.get("name") or "").strip() and (b.get("task") or "").strip()]
    if len(branches) < 2:
        return _split_error(pid, "A split needs at least 2 branches.")
    branches = branches[:SPLIT_MAX_BRANCHES]

    run_id = uuid.uuid4().hex[:6]
    run_dir = split_run_dir(pid, run_id)
    slugs = _unique_slugs([b["name"] for b in branches])

    entries = []
    for i, (b, slug) in enumerate(zip(branches, slugs), start=1):
        entries.append({
            "id": f"{run_id}{i}",
            "n": i,
            "name": b["name"].strip(),
            "slug": slug,
            "task": b["task"].strip(),
            "files": b.get("files") or [],
            "branch": f"split/{run_id}/{slug}",
            "worktree": str(run_dir / slug),
            "status": "starting",
            "changed_files": [],
            "note": "",
        })

    run = {
        "id": run_id,
        "prompt": prompt,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "started": datetime.now().isoformat(),
        "merged": None,
        "branches": entries,
    }
    with split_lock(pid):
        data = load_split(pid)
        data.update({"state": "running", "error": "", "prompt": prompt, "run": run, "proposed": [], "needs_base_commit": []})
        save_split(pid, data)

        run_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            r = _git(path, "worktree", "add", "-b", entry["branch"], entry["worktree"], base_sha)
            if r.returncode != 0:
                entry["status"] = "stopped"
                entry["note"] = f"Couldn't create worktree: {r.stderr.strip()[:300]}"
        save_split(pid, data)

    threading.Thread(target=_start_split_agents, args=(pid, run["id"]), daemon=True).start()
    return data


def _brief_landed(session, marker, timeout=12):
    """Confirm a submitted message actually reached the TUI — it echoes the text
    into its transcript, so the branch name showing up means it took."""
    deadline = datetime.now().timestamp() + timeout
    while datetime.now().timestamp() < deadline:
        if marker in capture_session_pane(session):
            return True
        threading.Event().wait(0.5)
    return False


def _start_split_agents(pid, run_id):
    """Boot each agent's session and hand it its brief.

    Sessions are created first, in one pass, so they all boot concurrently — then
    each is briefed once its TUI is actually up. (Creation stays sequential and
    blocking: tmux session creation racing itself on a cold server is the failure
    start_session()'s blocking run() exists to avoid.)"""
    data = load_split(pid)
    run = data.get("run") or {}
    if run.get("id") != run_id:
        return
    entries = [e for e in run["branches"] if e["status"] != "stopped"]
    total = len(entries)

    for entry in entries:
        spawn_claude_tmux(split_session_name(pid, entry["id"]), entry["worktree"])

    for entry in entries:
        names = [e["name"] for e in entries if e["id"] != entry["id"]]
        files_line = ("\nFiles you'll most likely be working in: " + ", ".join(entry["files"]) + "\n") if entry["files"] else ""
        brief = SPLIT_AGENT_PROMPT.format(
            n=entry["n"], total=total, branch=entry["branch"], overall=run["prompt"],
            task=entry["task"], files_line=files_line,
            others=", ".join(names) if names else "(none)",
        )
        session = split_session_name(pid, entry["id"])
        # Waiting and pasting are slow (seconds each) — deliberately outside the
        # lock, so a merge or a correction the user fires meanwhile isn't blocked.
        wait_for_claude_ready(session)
        paste_into_session(session, brief)
        if not _brief_landed(session, entry["branch"]):
            # Belt and braces: an agent that silently never received its brief
            # just sits at an empty prompt looking "idle", which is the one
            # failure the user can't tell apart from "finished already".
            paste_into_session(session, brief)
            if not _brief_landed(session, entry["branch"]):
                entry["note"] = "Couldn't confirm this agent received its brief — check its terminal."
        entry["status"] = "working"

        with split_lock(pid):
            data = load_split(pid)
            if (data.get("run") or {}).get("id") != run_id:
                return  # run was cleaned up under us
            for e in data["run"]["branches"]:
                if e["id"] == entry["id"]:
                    e["status"] = "working"
                    e["note"] = entry.get("note", "")
            save_split(pid, data)


def split_branch_files(entry, base_sha):
    """Everything this agent has touched — committed on its branch plus whatever
    is still uncommitted in its worktree."""
    wt = entry["worktree"]
    if not os.path.isdir(wt):
        return []
    files = set()
    committed = _git(wt, "diff", "--name-only", base_sha)
    if committed.returncode == 0:
        files.update(f for f in committed.stdout.splitlines() if f.strip())
    status = _git(wt, "status", "--porcelain")
    if status.returncode == 0:
        files.update(line[3:].strip() for line in status.stdout.splitlines() if line[3:].strip())
    return sorted(files)[:40]


_split_pane_watch = {}  # "pid:bid" -> {"body": str, "changed_at": float}


def _split_tick():
    """Keep each agent's live status (working / idle / stopped) and touched-file
    list current, using the same stable-pane diffing as the blocked watchdog.

    Observations (pane captures, git calls) are gathered WITHOUT the project lock
    since they're slow, then applied to a freshly-reloaded copy under it — so a
    merge or cleanup landing mid-sweep wins instead of being overwritten by
    readings taken before it happened."""
    now = datetime.now().timestamp()
    seen = set()
    for meta in all_projects():
        pid = meta["id"]
        data = load_split(pid)
        run = data.get("run")
        if not run or data.get("state") not in ("running", "merging", "merged", "conflict"):
            continue

        observed = {}
        for entry in run["branches"]:
            key = f"{pid}:{entry['id']}"
            seen.add(key)
            files = split_branch_files(entry, run["base_sha"])
            status = None

            if entry["status"] in SPLIT_LIVE_STATUSES:
                session = split_session_name(pid, entry["id"])
                if not _tmux_has_session(session):
                    status = "stopped"
                else:
                    body = pane_body(capture_session_pane(session))
                    with _lock:
                        prev = _split_pane_watch.get(key)
                        if prev is None or prev["body"] != body:
                            _split_pane_watch[key] = {"body": body, "changed_at": now}
                            idle_for = 0
                        else:
                            idle_for = now - prev["changed_at"]
                    status = "idle" if idle_for >= SPLIT_IDLE_THRESHOLD else "working"
            observed[entry["id"]] = (status, files)

        with split_lock(pid):
            fresh = load_split(pid)
            fresh_run = fresh.get("run")
            if not fresh_run or fresh_run["id"] != run["id"]:
                continue  # run was replaced or cleaned up while we looked
            dirty = False
            for entry in fresh_run["branches"]:
                status, files = observed.get(entry["id"], (None, None))
                if files is not None and files != entry.get("changed_files"):
                    entry["changed_files"] = files
                    dirty = True
                # Re-check against the fresh copy: a merge may have moved this
                # branch to a terminal status since the reading was taken.
                if status and entry["status"] in SPLIT_LIVE_STATUSES and status != entry["status"]:
                    entry["status"] = status
                    dirty = True
            if dirty:
                save_split(pid, fresh)

    with _lock:
        for key in list(_split_pane_watch):
            if key not in seen:
                del _split_pane_watch[key]


def split_monitor():
    while True:
        try:
            _split_tick()
        except Exception as e:
            print(f"[split-monitor] error: {e}")
        threading.Event().wait(SPLIT_CHECK_INTERVAL)


def merge_split(pid, commit_base=False):
    """Commit each agent's leftover work, then merge the branches into the base
    branch one at a time. Stops at the first conflict with the conflicted files
    named, so the user can send that one agent a fix instead of untangling a
    half-merged tree."""
    with split_lock(pid):
        return _merge_split_locked(pid, commit_base)


def _merge_split_locked(pid, commit_base):
    data = load_split(pid)
    run = data.get("run")
    if not run:
        return _split_error(pid, "No split run to merge.")

    path = (load_meta(pid) or {}).get("path")
    if not path or not is_git_repo(path):
        return _split_error(pid, "Project folder is no longer a git repo.")

    # git merge lands on whatever HEAD currently is. If the working tree moved to
    # another branch since launch, merging would dump every agent's work onto the
    # wrong branch — so require the base branch back before touching anything.
    current = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != run["base_branch"]:
        return _split_error(
            pid,
            f"This split branched off `{run['base_branch']}`, but the project is now on "
            f"`{current}`. Switch back to `{run['base_branch']}` before merging.",
        )

    # A merge into a dirty base would mix the user's own uncommitted work into
    # the merge commit (or just be refused by git), so it's opt-in.
    status = _git(path, "status", "--porcelain")
    base_dirty = [line[3:].strip() for line in status.stdout.splitlines() if line[3:].strip()]
    if base_dirty:
        if not commit_base:
            data["error"] = ""
            data["needs_base_commit"] = base_dirty[:40]
            save_split(pid, data)
            return data
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "Snapshot before merging split branches")
    data["needs_base_commit"] = []

    data["state"] = "merging"
    data["error"] = ""
    save_split(pid, data)

    for entry in run["branches"]:
        if entry["status"] == "merged":
            continue
        wt = entry["worktree"]
        if not os.path.isdir(wt):
            entry["status"] = "empty"
            entry["note"] = "Worktree is gone — nothing to merge."
            continue

        if _git(wt, "status", "--porcelain").stdout.strip():
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", f"split({entry['slug']}): agent work")

        ahead = _git(path, "rev-list", "--count", f"{run['base_branch']}..{entry['branch']}")
        if ahead.stdout.strip() in ("", "0"):
            entry["status"] = "empty"
            entry["note"] = "This agent didn't commit any changes."
            continue

        r = _git(path, "merge", "--no-ff", entry["branch"], "-m",
                 f"Merge split branch {entry['slug']} ({entry['name']})")
        if r.returncode == 0:
            entry["status"] = "merged"
            entry["note"] = ""
            continue

        conflicts = _git(path, "diff", "--name-only", "--diff-filter=U").stdout.split()
        _git(path, "merge", "--abort")
        entry["status"] = "conflict"
        entry["conflicts"] = conflicts[:40]
        entry["note"] = (r.stdout + r.stderr).strip()[:400]
        data["state"] = "conflict"
        save_split(pid, data)
        return data

    run["merged"] = datetime.now().isoformat()
    data["state"] = "merged"
    save_split(pid, data)
    return data


REBASE_PROMPT = """Your branch `{branch}` conflicts with the base branch `{base}` — the other agents' work landed there first, so your changes no longer apply cleanly.

Conflicting files:
{files}

Please: `git fetch` isn't needed (same repo). Run `git merge {base}` here in your worktree, resolve the conflicts by hand — keeping BOTH your work and what's already on {base} — then commit the merge. Tell me "DONE:" when the worktree is clean and your work still does what it should."""


def ask_branch_to_rebase(pid, bid):
    with split_lock(pid):
        data = load_split(pid)
        run = data.get("run") or {}
        entry = next((e for e in run.get("branches", []) if e["id"] == bid), None)
        if not entry:
            return None
        session = split_session_name(pid, bid)
        if not _tmux_has_session(session):
            return None
        files = "\n".join(f"- {f}" for f in entry.get("conflicts", [])) or "(see git status)"
        paste_into_session(session, REBASE_PROMPT.format(
            branch=entry["branch"], base=run.get("base_branch", "main"), files=files))
        entry["status"] = "working"
        if data.get("state") == "conflict":
            data["state"] = "running"
        save_split(pid, data)
        return data


def cleanup_split(pid, delete_branches=False):
    """Tear down the run: kill the agent sessions, remove the worktrees, and file
    the run under history. Branches are kept by default — merged work lives on
    the base branch, but an unmerged branch is the only copy of that agent's
    work, so deleting it is opt-in."""
    with split_lock(pid):
        return _cleanup_split_locked(pid, delete_branches)


def _cleanup_split_locked(pid, delete_branches):
    data = load_split(pid)
    run = data.get("run")
    if not run:
        data.update({"state": "idle", "proposed": [], "error": ""})
        save_split(pid, data)
        return data
    path = (load_meta(pid) or {}).get("path")

    for entry in run["branches"]:
        session = split_session_name(pid, entry["id"])
        if _tmux_has_session(session):
            subprocess.run([TMUX, "kill-session", "-t", session])
        if path and is_git_repo(path):
            _git(path, "worktree", "remove", "--force", entry["worktree"])
            if delete_branches:
                _git(path, "branch", "-D", entry["branch"])
    if path and is_git_repo(path):
        _git(path, "worktree", "prune")

    # Tidy the now-empty run/project scratch dirs. rmdir (not rmtree) on purpose:
    # it fails harmlessly if anything is left, so a worktree that didn't come off
    # cleanly keeps its files rather than being silently deleted.
    if run["branches"]:
        run_dir = Path(run["branches"][0]["worktree"]).parent
        for d in (run_dir, run_dir.parent):
            try:
                d.rmdir()
            except OSError:
                break

    run["closed"] = datetime.now().isoformat()
    run["final_state"] = data.get("state")
    data.setdefault("history", []).insert(0, run)
    data["history"] = data["history"][:10]
    data.update({"state": "idle", "run": None, "proposed": [], "error": "", "needs_base_commit": []})
    save_split(pid, data)
    return data


def kill_split_sessions(pid):
    """Stop a project's agent sessions without tearing the worktrees down — used
    by Close All and server shutdown, where the run should survive to be resumed."""
    run = (load_split(pid) or {}).get("run")
    for entry in (run or {}).get("branches", []):
        session = split_session_name(pid, entry["id"])
        if _tmux_has_session(session):
            subprocess.run([TMUX, "kill-session", "-t", session])


# ── Daily report ─────────────────────────────────────────────────────────────
# Close All already asks every live session for a handoff summary before killing
# it. That's the raw material for a day's report: gather each project touched
# today (the ones just closed, plus any paused earlier or auto-paused by the
# context monitor), render it to a PDF via the Chrome that's already used for
# screenshot tests, and email it as an attachment.
