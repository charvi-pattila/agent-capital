import json
import subprocess
from datetime import datetime
from pathlib import Path
from core import DATA_DIR, all_projects, load_meta
from messaging import load_messages
from memory import get_memory
from testrunner import CHROME_BIN
from council import load_council
from split import load_split


REPORTS_DIR = DATA_DIR.parent / "reports"
EMAIL_CONFIG_PATH = DATA_DIR.parent / ".email_config"


def load_email_config():
    """{smtp_user, smtp_pass, to, smtp_host?, smtp_port?} or None if unconfigured.
    Absent config is a normal state, not an error — the report is still built and
    saved to disk, just not mailed."""
    if not EMAIL_CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(EMAIL_CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return None
    if not cfg.get("smtp_user") or not cfg.get("smtp_pass"):
        return None
    cfg.setdefault("to", cfg["smtp_user"])
    cfg.setdefault("smtp_host", "smtp.gmail.com")
    cfg.setdefault("smtp_port", 465)
    return cfg


def _is_today(iso, today=None):
    if not iso:
        return False
    today = today or datetime.now().date()
    try:
        return datetime.fromisoformat(iso).date() == today
    except (ValueError, TypeError):
        return False


def collect_day_activity(pid, today=None):
    """What this project did today, or None if it did nothing.

    Activity is judged from recorded timestamps only — meta's `last_activity` and
    the message log — never from file mtimes. Every path that writes a session
    summary (Close All, the context-bloat monitor) goes through send_to_session, which
    stamps last_activity, so mtime adds nothing but false positives: restoring a
    backup or touching a file would otherwise resurrect a project into the
    report. Close All additionally passes the sessions it just closed explicitly."""
    today = today or datetime.now().date()
    meta = load_meta(pid)
    if not meta:
        return None

    msgs = load_messages(pid)
    today_msgs = [m for m in msgs if _is_today(m.get("timestamp"), today)]
    touched = bool(today_msgs) or _is_today(meta.get("last_activity"), today)

    if not touched:
        return None

    mem = get_memory(pid)["memory"]
    summary = (mem.get("last_session") or "").strip()

    times = sorted(m["timestamp"] for m in today_msgs if m.get("timestamp"))
    council = load_council(pid)
    council_runs = [r for r in council.get("runs", []) if _is_today(r.get("started"), today)]
    split = load_split(pid)
    splits = [r for r in ([split["run"]] if split.get("run") else []) + split.get("history", [])
              if _is_today(r.get("started"), today)]

    return {
        "id": pid,
        "name": meta.get("name", pid),
        "emoji": meta.get("emoji", "📁"),
        "description": meta.get("description", ""),
        "summary": summary,
        "errors": (mem.get("errors") or "").strip(),
        "messages": len(today_msgs),
        "first": times[0] if times else None,
        "last": times[-1] if times else None,
        "council": {
            "runs": len(council_runs),
            "approved": sum(1 for r in council_runs if r.get("approved")),
        } if council_runs else None,
        "splits": [{
            "branches": [b["name"] for b in s.get("branches", [])],
            "merged": bool(s.get("merged")),
        } for s in splits],
    }


def _fmt_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%-I:%M %p")
    except (ValueError, TypeError):
        return ""


def build_report_html(projects, day=None):
    day = day or datetime.now()
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    total_msgs = sum(p["messages"] for p in projects)

    cards = []
    for p in projects:
        meta_bits = []
        if p["first"] and p["last"]:
            meta_bits.append(f"{_fmt_time(p['first'])} – {_fmt_time(p['last'])}")
        if p["messages"]:
            meta_bits.append(f"{p['messages']} message{'s' if p['messages'] != 1 else ''}")

        extras = ""
        if p["council"]:
            c = p["council"]
            extras += (f"<div class='extra'><b>Council:</b> {c['approved']}/{c['runs']} "
                       f"review{'s' if c['runs'] != 1 else ''} approved</div>")
        for s in p["splits"]:
            extras += (f"<div class='extra'><b>Split:</b> {', '.join(esc(b) for b in s['branches'])}"
                       f" — {'merged' if s['merged'] else 'not merged'}</div>")
        if p["errors"]:
            extras += f"<div class='extra errors'><b>Open problems:</b> {esc(p['errors'][:600])}</div>"

        summary = esc(p["summary"]) or "<i>No summary was saved for this session.</i>"
        cards.append(f"""
      <section class="project">
        <h2><span class="emoji">{esc(p['emoji'])}</span> {esc(p['name'])}</h2>
        <div class="meta">{esc(' · '.join(meta_bits)) or 'No message activity recorded'}</div>
        <div class="summary">{summary}</div>
        {extras}
      </section>""")

    body = "".join(cards) or "<section class='project'><div class='meta'>No project activity recorded today.</div></section>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Daily report</title><style>
  @page {{ size: letter; margin: 16mm 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
         color: #1a1a24; font-size: 11pt; line-height: 1.55; margin: 0; }}
  header {{ border-bottom: 3px solid #7c5cfc; padding-bottom: 12px; margin-bottom: 22px; }}
  h1 {{ font-size: 21pt; margin: 0 0 4px; letter-spacing: -0.4px; }}
  .day {{ color: #6b6b80; font-size: 10.5pt; }}
  .totals {{ margin-top: 10px; font-size: 10.5pt; color: #4a4a5c; }}
  .totals b {{ color: #7c5cfc; }}
  .project {{ page-break-inside: avoid; border: 1px solid #e2e2ec; border-radius: 10px;
              padding: 14px 16px; margin-bottom: 14px; }}
  h2 {{ font-size: 13pt; margin: 0 0 3px; }}
  .emoji {{ margin-right: 5px; }}
  .meta {{ color: #6b6b80; font-size: 9.5pt; margin-bottom: 9px; }}
  .summary {{ white-space: pre-wrap; }}
  .extra {{ margin-top: 9px; padding-top: 8px; border-top: 1px solid #eeeef4;
            font-size: 10pt; color: #4a4a5c; }}
  .extra.errors {{ color: #a13030; }}
  footer {{ margin-top: 20px; color: #9b9bb2; font-size: 9pt;
            border-top: 1px solid #e2e2ec; padding-top: 10px; }}
</style></head><body>
  <header>
    <h1>Daily report</h1>
    <div class="day">{day.strftime('%A, %B %-d, %Y')}</div>
    <div class="totals">
      <b>{len(projects)}</b> project{'s' if len(projects) != 1 else ''} worked on
      · <b>{total_msgs}</b> message{'s' if total_msgs != 1 else ''} exchanged
    </div>
  </header>
  {body}
  <footer>Generated by Agent Capital at {day.strftime('%-I:%M %p')} — summaries are each session's own handoff notes.</footer>
</body></html>"""


def render_pdf(html, out_path):
    """HTML → PDF through the same Chrome the screenshot tests already shell out
    to, so this adds no new dependency."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = out_path.with_suffix(".html")
    src.write_text(html)
    try:
        subprocess.run(
            [CHROME_BIN, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={out_path}", src.as_uri()],
            capture_output=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def send_report_email(pdf_path, projects, day=None):
    """(sent, detail). Never raises — a failed send must not take down Close All."""
    cfg = load_email_config()
    if not cfg:
        return False, f"No email configured ({EMAIL_CONFIG_PATH.name} missing) — report saved to disk only."

    import smtplib
    from email.message import EmailMessage

    day = day or datetime.now()
    names = ", ".join(p["name"] for p in projects) or "no active projects"
    msg = EmailMessage()
    msg["Subject"] = f"Your day in code — {day.strftime('%a %b %-d')}"
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["to"]
    lines = [f"Here's what you got done on {day.strftime('%A, %B %-d')}.", "", f"Projects: {names}", ""]
    for p in projects:
        lines.append(f"— {p['emoji']} {p['name']}")
        if p["summary"]:
            lines.append(f"   {' '.join(p['summary'].split())[:300]}")
        lines.append("")
    lines.append("Full report attached as a PDF.")
    msg.set_content("\n".join(lines))

    try:
        msg.add_attachment(Path(pdf_path).read_bytes(), maintype="application",
                           subtype="pdf", filename=Path(pdf_path).name)
        with smtplib.SMTP_SSL(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=45) as s:
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)
        return True, f"Emailed to {cfg['to']}"
    except Exception as e:
        return False, f"Email failed: {type(e).__name__}: {e}"


def build_and_send_daily_report(extra_pids=None):
    """Gather today's work, render the PDF, mail it. `extra_pids` forces projects
    in even if the activity check misses them — Close All passes the sessions it
    just closed, which are the whole point of the report."""
    today = datetime.now().date()
    forced = set(extra_pids or [])
    projects = []
    for meta in all_projects():
        entry = collect_day_activity(meta["id"], today)
        if entry is None and meta["id"] in forced:
            entry = {**{
                "id": meta["id"], "name": meta.get("name", meta["id"]),
                "emoji": meta.get("emoji", "📁"), "description": meta.get("description", ""),
                "summary": "", "errors": "", "messages": 0, "first": None, "last": None,
                "council": None, "splits": [],
            }}
        if entry:
            projects.append(entry)
    projects.sort(key=lambda p: (p["last"] or ""), reverse=True)

    now = datetime.now()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = REPORTS_DIR / f"daily-report-{now:%Y-%m-%d}.pdf"
    html = build_report_html(projects, now)

    if not projects:
        # Nothing happened today — an empty report in the inbox is just noise.
        detail = "No project activity today — no report sent."
        print(f"[daily-report] {detail}")
        return {"ok": True, "emailed": False, "detail": detail, "projects": 0, "pdf": ""}

    if not render_pdf(html, pdf_path):
        result = {"ok": False, "detail": "Couldn't render the PDF.", "projects": len(projects)}
        print(f"[daily-report] {result['detail']}")
        return result

    sent, detail = send_report_email(pdf_path, projects, now)
    print(f"[daily-report] {len(projects)} project(s) — {detail}")
    return {"ok": True, "emailed": sent, "detail": detail,
            "projects": len(projects), "pdf": str(pdf_path)}


# ── Routes ────────────────────────────────────────────────────────────────────
