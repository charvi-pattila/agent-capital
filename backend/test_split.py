"""Drives the split engine's git plumbing against a scratch repo, with the
Claude/tmux spawning stubbed out so no real sessions or usage are burned."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/charvipattila/code/my-claude/agent-capitol/backend")
import server

TMP = Path(tempfile.mkdtemp(prefix="splittest-"))
REPO = TMP / "repo"
REPO.mkdir(parents=True)


def git(*args, cwd=REPO):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        globals()["FAILED"] = True


FAILED = False

# ── scratch repo ──
git("init", "-b", "main")
git("config", "user.email", "t@t.t")
git("config", "user.name", "t")
(REPO / "base.txt").write_text("base\n")
(REPO / "shared.txt").write_text("line1\nline2\nline3\n")
git("add", "-A")
git("commit", "-m", "init")

# ── isolated project store ──
server.DATA_DIR = TMP / "projects"
server.DATA_DIR.mkdir()
server.SPLIT_ROOT = TMP / "splits"
PID = "testpid1"
(server.DATA_DIR / PID).mkdir()
(server.DATA_DIR / PID / "meta.json").write_text(json.dumps({
    "id": PID, "name": "Split Test", "path": str(REPO), "emoji": "🧪",
}))

# ── stub out agent spawning ──
spawned = []
pasted = []
real_paste = server.paste_into_session   # kept for the live-tmux delivery checks below
server.spawn_claude_tmux = lambda session, cwd: spawned.append((session, cwd)) or True
server.paste_into_session = lambda session, msg, delay=0.3: pasted.append((session, msg))
server._tmux_has_session = lambda name: False   # sessions never "exist" -> restart/kill paths no-op
# There's no real TUI to boot or echo the brief back, so short-circuit both waits.
server.wait_for_claude_ready = lambda session, timeout=60: True
server._brief_landed = lambda session, marker, timeout=12: True

print("\n1. preflight")
path, base_branch, base_sha, err = server.split_preflight(PID)
check("git repo with a commit passes preflight", err is None, err or "")
check("base branch detected", base_branch == "main", base_branch)

print("\n2. launch")
data = server.launch_split(PID, "Build three things at once", [
    {"name": "Alpha part", "task": "make alpha.txt", "files": ["alpha.txt"]},
    {"name": "Beta part", "task": "make beta.txt"},
    {"name": "Alpha part", "task": "duplicate name on purpose"},
])
run = data["run"]
check("state is running", data["state"] == "running", data["state"])
check("3 branches created", len(run["branches"]) == 3)
check("duplicate names get unique slugs",
      [b["slug"] for b in run["branches"]] == ["alpha-part", "beta-part", "alpha-part-2"],
      str([b["slug"] for b in run["branches"]]))
check("worktrees exist on disk", all(os.path.isdir(b["worktree"]) for b in run["branches"]))
wt_list = git("worktree", "list").stdout
check("git knows about all 3 worktrees", wt_list.count("split/") == 3, wt_list.replace("\n", " | "))
check("each worktree is on its own branch",
      all(git("rev-parse", "--abbrev-ref", "HEAD", cwd=b["worktree"]).stdout.strip() == b["branch"]
          for b in run["branches"]))
check("main worktree untouched", git("status", "--porcelain").stdout.strip() == "")

# _start_split_agents runs in a background thread from launch — wait for it
import time
for _ in range(50):
    if len(pasted) >= 3:
        break
    time.sleep(0.1)
check("every agent got a brief, exactly once", len(pasted) == 3, str(len(pasted)))
check("brief names the branch", run["branches"][0]["branch"] in pasted[0][1])
check("brief warns off the other pieces", "Beta part" in pasted[0][1])
data = server.load_split(PID)
check("agents marked working", all(b["status"] == "working" for b in data["run"]["branches"]),
      str([b["status"] for b in data["run"]["branches"]]))

print("\n3. agents do work")
run = server.load_split(PID)["run"]
a, b, c = run["branches"]
(Path(a["worktree"]) / "alpha.txt").write_text("alpha\n")
git("add", "-A", cwd=a["worktree"]); git("commit", "-m", "alpha", cwd=a["worktree"])
(Path(b["worktree"]) / "beta.txt").write_text("beta\n")   # left UNCOMMITTED on purpose
# c makes no changes at all

files = server.split_branch_files(a, run["base_sha"])
check("committed work shows in changed files", files == ["alpha.txt"], str(files))
files = server.split_branch_files(b, run["base_sha"])
check("uncommitted work shows in changed files", files == ["beta.txt"], str(files))
check("idle agent shows nothing", server.split_branch_files(c, run["base_sha"]) == [])

print("\n4. merge with a dirty base")
(REPO / "user-edit.txt").write_text("user was mid-edit\n")
data = server.merge_split(PID)
check("dirty base blocks the merge and names the files",
      data.get("needs_base_commit") == ["user-edit.txt"], str(data.get("needs_base_commit")))
check("nothing merged yet", all(x["status"] != "merged" for x in data["run"]["branches"]))

print("\n5. merge for real")
data = server.merge_split(PID, commit_base=True)
statuses = [x["status"] for x in data["run"]["branches"]]
check("run state merged", data["state"] == "merged", data["state"])
check("alpha + beta merged, empty branch flagged", statuses == ["merged", "merged", "empty"], str(statuses))
check("alpha landed on main", (REPO / "alpha.txt").exists())
check("beta's uncommitted work was committed and landed", (REPO / "beta.txt").exists())
check("user's own edit preserved", (REPO / "user-edit.txt").exists())
log = git("log", "--oneline").stdout
check("merge commits recorded", log.count("Merge split branch") == 2, log.replace("\n", " | "))

print("\n6. conflict path")
blocked = server.launch_split(PID, "second run while one is open", [
    {"name": "X", "task": "x"}, {"name": "Y", "task": "y"},
])
check("a second launch is refused while a run is open",
      "already open" in (blocked.get("error") or "") and blocked["run"]["id"] == run["id"],
      blocked.get("error"))
check("the open run's worktrees survived the refusal",
      all(os.path.isdir(x["worktree"]) for x in run["branches"]))

server.cleanup_split(PID)
data = server.launch_split(PID, "two agents touch the same lines", [
    {"name": "Left", "task": "edit shared.txt"},
    {"name": "Right", "task": "also edit shared.txt"},
])
for _ in range(50):
    if len(pasted) >= 5:
        break
    time.sleep(0.1)
run = server.load_split(PID)["run"]
l, r = run["branches"]
(Path(l["worktree"]) / "shared.txt").write_text("LEFT\nline2\nline3\n")
git("add", "-A", cwd=l["worktree"]); git("commit", "-m", "left", cwd=l["worktree"])
(Path(r["worktree"]) / "shared.txt").write_text("RIGHT\nline2\nline3\n")
git("add", "-A", cwd=r["worktree"]); git("commit", "-m", "right", cwd=r["worktree"])

data = server.merge_split(PID)
statuses = [x["status"] for x in data["run"]["branches"]]
check("state flips to conflict", data["state"] == "conflict", data["state"])
check("first merged, second conflicts", statuses == ["merged", "conflict"], str(statuses))
check("conflicting file named", data["run"]["branches"][1].get("conflicts") == ["shared.txt"],
      str(data["run"]["branches"][1].get("conflicts")))
check("repo is NOT left mid-merge", not (REPO / ".git" / "MERGE_HEAD").exists())
check("base worktree still clean after abort", git("status", "--porcelain").stdout.strip() == "")
check("the merge that did work survived", "LEFT" in (REPO / "shared.txt").read_text())

print("\n6b. merging from the wrong branch is refused")
# Conflict run is still open here; move the base worktree off its branch.
git("checkout", "-q", "-b", "some-other-work")
head_before = git("rev-parse", "HEAD").stdout.strip()
data = server.merge_split(PID)
check("merge refused when the tree moved off the base branch",
      "Switch back to" in (data.get("error") or ""), data.get("error"))
check("the wrong branch was left exactly as it was",
      git("rev-parse", "HEAD").stdout.strip() == head_before)
git("checkout", "-q", "main")

print("\n7. cleanup")
data = server.cleanup_split(PID, delete_branches=False)
check("state back to idle", data["state"] == "idle" and data["run"] is None)
check("run filed in history", len(data["history"]) == 2, str(len(data["history"])))
check("worktrees removed from disk", all(not os.path.isdir(x["worktree"]) for x in run["branches"]))
check("git worktree list is clean", "split/" not in git("worktree", "list").stdout)
check("unmerged branch kept by default", "split/" in git("branch", "--list", "split/*").stdout)

data = server.load_split(PID)
run2 = data["history"][0]
server_run = {"run": run2}
check("history keeps the prompt", "same lines" in run2["prompt"], run2["prompt"])

print("\n7b. tmux message delivery (real tmux, no Claude)")
# Both of these were found by the review council and reproduced against a live
# server: buffers are global, and a leading "-" is parsed as a flag.
TM = server.TMUX
subprocess.run([TM, "kill-server"], capture_output=True)
time.sleep(1)
sinks = {"qaA": TMP / "qa_a.txt", "qaB": TMP / "qa_b.txt"}
for s, f in sinks.items():
    subprocess.run([TM, "new-session", "-d", "-s", s, "-x", "80", "-y", "20", f"cat > {f}"])
time.sleep(1)

import threading as _th
ts = [_th.Thread(target=real_paste, args=(s, f"MESSAGE-FOR-{s}")) for s in sinks]
[t.start() for t in ts]
[t.join() for t in ts]
time.sleep(0.6)
real_paste("qaA", "- a message starting with a dash")
time.sleep(0.4)
real_paste("qaA", "--- a/backend/server.py")
time.sleep(1)
subprocess.run([TM, "kill-server"], capture_output=True)
time.sleep(0.5)

got = {s: f.read_text().splitlines() for s, f in sinks.items()}
check("concurrent sends reach the right sessions",
      got["qaA"][0] == "MESSAGE-FOR-qaA" and got["qaB"][0] == "MESSAGE-FOR-qaB", str(got))
check("a message starting with '-' is delivered verbatim",
      "- a message starting with a dash" in got["qaA"], str(got["qaA"]))
check("a diff line is delivered verbatim", "--- a/backend/server.py" in got["qaA"], str(got["qaA"]))
check("no message is silently re-sent", len(got["qaA"]) == len(set(got["qaA"])), str(got["qaA"]))

print("\n8. non-git project is refused")
plain = TMP / "plain"
plain.mkdir()
(server.DATA_DIR / PID / "meta.json").write_text(json.dumps({"id": PID, "name": "Plain", "path": str(plain)}))
_, _, _, err = server.split_preflight(PID)
check("non-git repo blocked with a useful message", err and "git init" in err, err)

empty = TMP / "emptyrepo"
empty.mkdir()
subprocess.run(["git", "-C", str(empty), "init", "-b", "main"], capture_output=True)
(server.DATA_DIR / PID / "meta.json").write_text(json.dumps({"id": PID, "name": "Empty", "path": str(empty)}))
_, _, _, err = server.split_preflight(PID)
check("commitless repo blocked with a useful message", err and "no commits" in err, err)

print("\n" + ("SOME CHECKS FAILED" if FAILED else "ALL CHECKS PASSED"))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
