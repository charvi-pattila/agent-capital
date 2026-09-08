# Projects

Overview of the personal projects tracked in this manager. Each has its own
`README.md` with full setup details — this is just the index.

---

## 🗓️ Lucky Planner
**Path:** `~/code/my-claude/lucky-planner`

A personal digital planner, available in three forms that share one Flask
backend and calendar-sync layer:

- **PDF Generator** — a 431-page GoodNotes/Notability-ready planner pre-filled
  with Apple/Google Calendar events (`main.py`, CLI).
- **Web App** — a live browser planner (Flask + React/TypeScript/Vite) with
  month/week/to-do views, Apple Pencil drawing, calendar sync, habit tracking,
  and themes (`web_app/`).
- **Mobile App** — a native iPad/iOS rewrite (React Native + Expo),
  feature-matched to the web client (`mobile/`).

Syncs with Apple Calendar/Reminders (iCloud CalDAV) and Google Calendar
(OAuth 2.0). See `SETUP.md` in the project for credentials setup.

---

## 👨‍👩‍👧‍👦 Pattila Family Manager
**Path:** `~/code/my-claude/pattila-family-manager`

A local household app (Next.js) for tracking family documents and renewal
dates — driver's licenses, health insurance, passports, anything with an
expiry.

- **Profiles** — each family member has a customizable avatar/color and a PIN.
- **Items** — anyone can add an item (title, category, optional renewal date,
  notes) and mark it **private** (PIN-gated) or **public**.
- **Calendar** — month view aggregating every visible renewal date,
  color-coded by owner.
- **Reminders** — email reminders at 30/14/7/1 days out (configurable) via
  Gmail SMTP, plus overdue notices. Triggered by `npm run reminders:check`
  (wire into cron/launchd) or a manual "Check reminders now" button.

Run: `npm install && npm run db:push && npm run dev` → http://localhost:3000.

PIN model is household-trust only, not a real security boundary — keeps
siblings out of each other's stuff, nothing more.

---

## 📄 Resume Rebuilder
**Path:** `~/code/my-claude/resume-rebuilder`

A job-application assistant (Next.js) for landing a SWE internship:

1. Upload a resume (PDF/DOCX/TXT) — Claude parses it into structured data.
2. Refresh the job feed — pulls the community-maintained SimplifyJobs
   internship list (~450–1,300 active postings).
3. Per job: fetch/paste the JD → score fit (0–100, strengths/gaps/ATS
   keywords) → tailor resume bullets (no fabrication — reword/reorder/
   emphasize only) → print/save as PDF.
4. Track each application's status from *saved* to *offer* on a dashboard.

Run: `npm install`, set `ANTHROPIC_API_KEY` in `.env.local`, then
`npm run dev` → http://localhost:3000. Uses `claude-opus-4-8`; all data
stored locally in SQLite (`data/app.db`).

---

## 🤖 All-in-One Bot Manager
**Path:** `~/code/my-claude/claude-bot-manager`

A personal AI agent/bot manager (Python + Flask) for creating and chatting
with Claude-backed bots from phone or Mac browser, with persistent chat
history and Gmail-sending tool use.

- **Local:** `source venv/bin/activate && python3 app.py` → http://127.0.0.1:8080
  (LAN: `http://<Mac LAN IP>:8080`)
- **Deployed:** Railway, installable to iPhone home screen as a PWA.
- **Data:** `bots.json` (agent configs), `chat_history.db` (SQLite, synced
  across devices), `.env` (API keys, gitignored).

Currently hardening the app per a security/reliability review (see this
repo's own memory) — a batch of fixes has landed, more in progress.

---

## Stack at a glance

| Project | Backend | Frontend | Data |
|---|---|---|---|
| Lucky Planner | Flask | React/TS (web) + React Native/Expo (mobile) | SQLite + iCloud/Google Calendar |
| Pattila Family Manager | Next.js API routes | Next.js/React | Prisma + SQLite |
| Resume Rebuilder | Next.js API routes | Next.js/React | SQLite |
| Bot Manager | Flask | Server-rendered HTML (PWA) | SQLite |
