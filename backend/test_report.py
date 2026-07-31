"""Exercises the daily report end to end against a fabricated day of work:
activity detection, HTML/PDF rendering, and the email assembly (with SMTP faked
out, so nothing is actually sent)."""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server

TMP = Path(tempfile.mkdtemp(prefix="reporttest-"))
FAILED = False


def check(label, cond, extra=""):
    global FAILED
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED = True


# ── isolated data store ──
server.DATA_DIR = TMP / "projects"
server.DATA_DIR.mkdir(parents=True)
server.REPORTS_DIR = TMP / "reports"
server.EMAIL_CONFIG_PATH = TMP / ".email_config"

now = datetime.now()


def make_project(pid, name, emoji, summary, *, msgs=0, errors="", when=None,
                 limit_paused=False, council=None, split=None, stale=False):
    d = server.DATA_DIR / pid
    (d / "memory" / "components").mkdir(parents=True)
    (d / "memory" / "last_session.md").write_text(summary)
    (d / "memory" / "errors.md").write_text(errors)
    (d / "memory" / "skills.md").write_text("")
    when = when or now
    (d / "meta.json").write_text(json.dumps({
        "id": pid, "name": name, "emoji": emoji, "path": str(TMP / pid),
        "description": "test project", "limit_paused": limit_paused,
        "last_activity": (when - timedelta(days=9) if stale else when).isoformat(),
    }))
    ts = (when - timedelta(days=9)) if stale else when
    (d / "messages.json").write_text(json.dumps([
        {"id": i + 1, "role": "user" if i % 2 == 0 else "assistant", "text": f"m{i}",
         "status": "done", "timestamp": (ts - timedelta(minutes=(msgs - i) * 7)).isoformat()}
        for i in range(msgs)
    ]))
    if council:
        (d / "council.json").write_text(json.dumps(council))
    if split:
        (d / "split.json").write_text(json.dumps(split))
    if stale:
        # Freshly-written files with today's mtime, but timestamps from last week:
        # activity must be judged from the recorded times, not the filesystem.
        pass


make_project(
    "aaa1", "lucky-planner", "📅", msgs=14,
    summary="Rebuilt the event editor as a five-stage flow and moved validation into "
            "the stage machine. Next: the recurrence rules still ignore DST.",
    council={"state": "approved", "runs": [
        {"id": "r1", "started": now.isoformat(), "approved": True, "testers": []},
        {"id": "r2", "started": now.isoformat(), "approved": False, "testers": []}]},
)
make_project(
    "bbb2", "Personal Website", "🌐", msgs=6,
    summary="Swapped the hero section for the new layout and fixed the mobile nav overlap.",
    errors="Lighthouse still flags the hero image as render-blocking.",
    split={"state": "merged", "run": None, "history": [
        {"id": "s1", "started": now.isoformat(), "merged": now.isoformat(),
         "branches": [{"name": "hero-layout"}, {"name": "mobile-nav"}]}]},
)
make_project(
    "ccc3", "Resume Rebuilder", "📄", msgs=3, limit_paused=True,
    summary="Started the PDF export path; got as far as the template loader before the "
            "usage limit hit. Pick up at export_pdf().",
)
make_project("ddd4", "buget-planner", "💰", "An old summary from last week.", msgs=4, stale=True)

print("1. activity detection")
active = [server.collect_day_activity(p) for p in ("aaa1", "bbb2", "ccc3", "ddd4")]
check("today's projects detected", all(a is not None for a in active[:3]))
check("a project idle since last week is excluded", active[3] is None)
check("a just-written file with old timestamps is still excluded (no mtime heuristic)",
      (server.DATA_DIR / "ddd4" / "memory" / "last_session.md").stat().st_mtime > (now - timedelta(minutes=5)).timestamp()
      and active[3] is None)
check("message counts are today-only", [a["messages"] for a in active[:3]] == [14, 6, 3],
      str([a["messages"] for a in active[:3]]))
check("council runs summarised", active[0]["council"] == {"runs": 2, "approved": 1}, str(active[0]["council"]))
check("split run summarised", active[1]["splits"] == [{"branches": ["hero-layout", "mobile-nav"], "merged": True}],
      str(active[1]["splits"]))
check("limit pause flagged", active[2]["limit_paused"])
check("errors carried through", "render-blocking" in active[1]["errors"])

print("\n2. html render")
html = server.build_report_html([a for a in active if a])
check("every active project appears", all(n in html for n in ("lucky-planner", "Personal Website", "Resume Rebuilder")))
check("the stale project doesn't", "buget-planner" not in html)
check("totals are right", "<b>3</b> project" in html and "<b>23</b> message" in html)
check("summary text included", "recurrence rules still ignore DST" in html)
check("html is escaped", "&amp;" in html or "<script" not in html)

print("\n3. pdf render (real Chrome)")
pdf = TMP / "report.pdf"
ok = server.render_pdf(html, pdf)
check("chrome produced a pdf", ok and pdf.exists())
check("pdf is non-trivial", pdf.stat().st_size > 8000, f"{pdf.stat().st_size} bytes")
check("file really is a pdf", pdf.read_bytes()[:5] == b"%PDF-", str(pdf.read_bytes()[:5]))

print("\n4. email with no config")
sent, detail = server.send_report_email(pdf, [a for a in active if a])
check("unconfigured email is reported, not raised", sent is False and "No email configured" in detail, detail)

print("\n5. email with config (SMTP faked)")
server.EMAIL_CONFIG_PATH.write_text(json.dumps({
    "smtp_user": "someone@gmail.com", "smtp_pass": "app password here", "to": "someone@gmail.com"}))

captured = {}


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        captured["host"], captured["port"] = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        captured["login"] = (u, p)

    def send_message(self, msg):
        captured["msg"] = msg


import smtplib
smtplib.SMTP_SSL = FakeSMTP
sent, detail = server.send_report_email(pdf, [a for a in active if a])
check("reports as sent", sent is True, detail)
check("connects to gmail over SSL", captured.get("host") == "smtp.gmail.com" and captured["port"] == 465)
check("logs in with the configured credentials", captured.get("login") == ("someone@gmail.com", "app password here"))

msg = captured["msg"]
atts = [p for p in msg.iter_attachments()]
check("subject names the day", "Your day in code" in msg["Subject"], msg["Subject"])
check("addressed to the configured recipient", msg["To"] == "someone@gmail.com")
check("exactly one pdf attached", len(atts) == 1 and atts[0].get_content_type() == "application/pdf",
      str([a.get_content_type() for a in atts]))
check("attachment is the real pdf", atts[0].get_payload(decode=True)[:5] == b"%PDF-")
check("plain-text body lists the projects",
      all(n in msg.get_body(("plain",)).get_content() for n in ("lucky-planner", "Personal Website")))

print("\n6. full pipeline")
res = server.build_and_send_daily_report()
check("pipeline reports success", res["ok"] and res["emailed"], str(res))
check("counted the right projects", res["projects"] == 3, str(res["projects"]))
check("pdf written to the reports dir", Path(res["pdf"]).exists() and Path(res["pdf"]).parent == server.REPORTS_DIR)

print("\n7. a day with nothing on it")
for d in server.DATA_DIR.iterdir():
    shutil.rmtree(d)
captured.clear()
res = server.build_and_send_daily_report()
check("succeeds without sending an empty report", res["ok"] and res["emailed"] is False, str(res))
check("says zero projects", res["projects"] == 0)
check("no email was actually attempted", "msg" not in captured)

print("\n" + ("SOME CHECKS FAILED" if FAILED else "ALL CHECKS PASSED"))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
