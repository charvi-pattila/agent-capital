import json
import subprocess
from datetime import datetime
from pathlib import Path
from core import load_meta, project_dir
from tmuxlib import session_running
from messaging import append_message, load_messages, send_to_session


# ── Tests ─────────────────────────────────────────────────────────────────────

def tests_path(pid):
    return project_dir(pid) / "tests" / "tests.json"


def load_tests(pid):
    p = tests_path(pid)
    return json.loads(p.read_text()) if p.exists() else []


def save_tests(pid, tests):
    tests_path(pid).parent.mkdir(parents=True, exist_ok=True)
    tests_path(pid).write_text(json.dumps(tests, indent=2))


# ── Screenshot comparison tests ──────────────────────────────────────────────
# Shells out to the Chrome already on the Mac (no Playwright/Selenium install) and
# diffs pixels with Pillow. First run on a test just records the baseline; every
# run after that compares against it and fails once the differing-pixel share
# crosses SCREENSHOT_DIFF_THRESHOLD.

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCREENSHOT_DIFF_THRESHOLD = 1.0  # percent of pixels allowed to differ before a fail


def screenshot_test_dir(pid, test_id):
    d = project_dir(pid) / "tests" / test_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture_screenshot(url, out_path):
    try:
        subprocess.run(
            [CHROME_BIN, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--window-size=1280,800", f"--screenshot={out_path}", url],
            capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return Path(out_path).exists()


def diff_screenshots(baseline_path, latest_path, diff_path):
    """Saves a diff image and returns the % of pixels that differ."""
    from PIL import Image, ImageChops
    a = Image.open(baseline_path).convert("RGB")
    b = Image.open(latest_path).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    diff.save(diff_path)
    # ignore very small per-channel deltas (anti-aliasing/font-rendering noise)
    mask = diff.convert("L").point(lambda p: 255 if p > 8 else 0)
    changed = mask.histogram()[255]
    total = a.size[0] * a.size[1]
    return round(100 * changed / total, 2) if total else 0.0


def run_screenshot_test(pid, test):
    if not test.get("target_url"):
        test["status"] = "failing"
        test["error"] = "No target URL set"
        return
    d = screenshot_test_dir(pid, test["id"])
    baseline, latest, diff = d / "baseline.png", d / "latest.png", d / "diff.png"
    if not capture_screenshot(test["target_url"], latest):
        test["status"] = "failing"
        test["error"] = "Could not capture screenshot (is the URL reachable?)"
        return
    if not baseline.exists():
        baseline.write_bytes(latest.read_bytes())
        test["status"] = "baseline_saved"
        test["error"] = ""
        test["diff_percent"] = 0.0
        return
    percent = diff_screenshots(baseline, latest, diff)
    test["diff_percent"] = percent
    test["status"] = "passing" if percent < SCREENSHOT_DIFF_THRESHOLD else "failing"
    test["error"] = "" if test["status"] == "passing" else f"{percent}% of pixels differ from baseline"


def run_tests_for_project(pid):
    tests = load_tests(pid)
    if not tests:
        return
    name = (load_meta(pid) or {}).get("name", pid)

    for i, test in enumerate(tests):
        tests[i]["status"] = "running"
        tests[i]["last_run"] = datetime.now().isoformat()
    save_tests(pid, tests)

    for i, test in enumerate(tests):
        if test.get("type") == "screenshot":
            run_screenshot_test(pid, tests[i])
            continue

        if not session_running(pid):
            tests[i]["status"] = "failing"
            tests[i]["error"] = "Session not running"
            continue

        prompt = f"Run this test and tell me if it passes. Reply with PASS or FAIL followed by a brief reason.\nTest: {test['description']}"
        if test.get("target_url"):
            prompt += f"\nURL to check: {test['target_url']}"

        msgs = load_messages(pid)
        next_id = (max(m["id"] for m in msgs) + 1) if msgs else 1
        reply_id = next_id + 1
        append_message(pid, {"id": next_id, "role": "user", "text": f"[Auto-test] {test['description']}", "status": "done", "timestamp": datetime.now().isoformat()})
        append_message(pid, {"id": reply_id, "role": "assistant", "text": "Running test...", "status": "thinking", "timestamp": datetime.now().isoformat()})

        send_to_session(pid, prompt, reply_id, name)
        msgs = load_messages(pid)
        reply = next((m for m in msgs if m["id"] == reply_id), None)
        reply_text = (reply or {}).get("text", "")

        tests[i]["status"] = "passing" if reply_text.strip().upper().startswith("PASS") else "failing"
        tests[i]["error"] = reply_text if tests[i]["status"] == "failing" else ""

    save_tests(pid, tests)


# ── Review loop: one reviewer, the author decides when it's done ──────────────
# After each chat turn, if the working tree changed, a single one-shot
# `claude -p` reviewer (independent of the interactive session, no shared
# context) reads the diff and either APPROVEs or reports concrete problems.
# Problems go back into the live session, whose agent fixes what it agrees
# with and then decides: "REVIEW: DONE" (it judges the change complete — the
# loop ends) or "REVIEW: AGAIN" (re-review the new diff). The author's
# decision is what terminates the loop; REVIEW_MAX_ROUNDS is only a backstop
# so a stuck exchange can't cycle forever.
#
# This replaced a 5-tester unanimous-vote council (2026-09-05). With LLM
# reviewers, five parallel opinions never converge — each one always finds
# *something* — so it produced noise rather than signal, and the agent that
# actually wrote the change had no say in when it was good enough.
