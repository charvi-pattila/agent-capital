# Claude Manager — Progress Log

## What We Built

A full-stack "ultimate Claude bot manager" — one dashboard to run and manage multiple Claude Code projects instead of juggling terminals.

---

## Stack
- **Backend:** Flask (Python) on port 8888
- **Frontend:** React + Vite on port 5173 (dev)
- **Session management:** tmux (one session per project)
- **Storage:** JSON files + markdown per project under `data/projects/{id}/`

---

## File Structure
```
phone-to-claude/
├── backend/
│   ├── server.py          # Flask API (full rewrite)
│   └── chat_runner.sh     # tmux session starter
├── frontend/
│   ├── src/
│   │   ├── App.jsx                        # Router + sidebar + Close All
│   │   ├── App.css                        # Global dark theme
│   │   ├── index.css                      # Component styles
│   │   ├── api/index.js                   # All fetch + SSE helpers
│   │   ├── pages/
│   │   │   ├── Dashboard.jsx              # Project list + new project
│   │   │   ├── Project.jsx                # Per-project view (tabs)
│   │   │   └── Running.jsx                # Active sessions
│   │   └── components/
│   │       ├── ProjectCard.jsx            # Card on dashboard
│   │       ├── NewProjectModal.jsx        # Create project modal
│   │       ├── Chat.jsx                   # Live terminal mirror
│   │       ├── MemoryPanel.jsx            # Memory hierarchy viewer/editor
│   │       ├── TestPanel.jsx              # Test runner
│   │       └── PreviewPanel.jsx           # Embedded iframe preview tab
├── data/projects/{id}/
│   ├── meta.json                          # Project metadata
│   ├── messages.json                      # Chat history (legacy, kept for context)
│   ├── memory/
│   │   ├── last_session.md               # Auto-saved on pause
│   │   ├── errors.md                     # Error log
│   │   ├── skills.md                     # Reusable patterns
│   │   └── components/{name}.md          # Per-component memory
│   └── tests/tests.json                  # Test definitions + results
└── venv/                                  # Python venv (flask, flask-cors)
```

---

## Key Behaviors Working

| Feature | Status |
|---|---|
| Dashboard with project cards | ✅ |
| Create project (emoji, name, desc, path, opening message) | ✅ |
| Start project → spins up tmux Claude session | ✅ |
| Memory context injected on Start (last session, errors, skills, components) | ✅ |
| Pause → Claude auto-saves session summary → kills tmux | ✅ |
| Chat tab = live terminal mirror (SSE, 300ms, diff-only updates) | ✅ |
| Memory tab = view/edit all memory types + add components | ✅ |
| Testing tab = add tests (description/screenshot/URL), run via Claude | ✅ |
| Running tab = shows all active sessions | ✅ |
| Vite proxy fix (127.0.0.1 not localhost, IPv4 vs IPv6 issue) | ✅ |
| Native terminal window opens on Start (`tmux attach` in Terminal.app), auto-closes on Pause/Close All (tracked by window ID, doesn't touch windows you open yourself) | ✅ |
| Close All (sidebar button) — saves memory + kills every running session in parallel, waits for the real reply to finish rather than a fixed timer | ✅ (needs UI polish, see below) |
| Native macOS notifications when a prompt finishes (chat messages, tests, auto-resume-on-start); suppressed during Pause/Close All | ✅ |
| Preview tab — per-project embedded iframe URL, editable, with Refresh/Edit/Open-in-new-tab, warns if URL is the manager itself | ✅ |
| Image paste/drag-drop in Chat tab (uploads to project dir, path sent to Claude) | ✅ |
| Close All modal with progress bar + per-project status | ✅ |
| Import existing folders as projects (scan `~/code/my-claude/`) | ✅ |
| Delete project (card hover 🗑) + search/filter on dashboard | ✅ |

---

## How to Run
```bash
# Terminal 1 — backend
cd phone-to-claude
source venv/bin/activate
python backend/server.py

# Terminal 2 — frontend
cd phone-to-claude/frontend
npm run dev
```
Open: http://localhost:5173

---

## 2026-07-08 session — entire 07-07 backlog shipped

All five user-reported items built and tested. Full browser UI test suite ran same day (15/15 passed) via puppeteer-core + CDP against a scratch Chrome instance — covered dashboard cards, running badge, search, delete-with-confirm, import modal (list/uncheck/import lucky-planner), chat image paste → chip → send → upload, preview URL + self-URL warning, and the Close All modal through confirm → progress → done. Screenshots reviewed. (The Claude Chrome *extension* wouldn't connect — likely needs a claude.ai login in Chrome Profile 3 — hence the CDP route; worth fixing for future sessions.)

1. **Image/screenshot paste in Chat tab — DONE.** Paste (or drag-drop) images into the Chat input → thumbnail chips with × remove appear above the input → on Send each image uploads via new `POST /api/projects/<pid>/upload` (saved to `data/projects/{pid}/uploads/<timestamp>.<ext>`) and the message gets `[Image attached: /abs/path]` lines appended so Claude reads the file. Can send images with no text. Verified: upload endpoint (direct + through Vite proxy) and tmux delivery of the composed message (via a fake tmux session, no tokens burned). *Still open: pasting into the native Terminal.app window — separate question, untested.*
2. **Preview tab wrong URL — was indeed stale test data.** `5cab0770`'s `preview_url` reset to the real snake-and-ladders URL `http://localhost:4173` (port read from its `server.js`). Also added a guard: PreviewPanel now shows a ⚠ warning (in both edit form and a banner over the iframe) whenever the URL points at Claude Manager's own origin, so this can't silently recur.
3. **Sidebar/color polish — first pass done (user hasn't seen it yet).** Sidebar: darker panel, gradient logo text, "Workspace" section label, softer active-nav style (tinted bg instead of solid purple), live green count badge on Running, Close All pinned to bottom above a divider. Palette tweaked (deeper bg, better text contrast). If user still dislikes something, get specifics.
4. **Close All modal — DONE.** New `CloseAllModal.jsx`: confirm step listing the running projects → progress bar + per-project spinner/✓ (polls `/api/running` every 2s while `POST /api/close-all` is in flight) → done state. Replaces window.confirm + inline sidebar text.
5. **Bulk-import folders — DONE (explicit button flow).** "⤵ Import folders" button on Dashboard → `ImportModal.jsx` lists untracked folders under `~/code/my-claude/` via `GET /api/import/candidates` (skips hidden/node_modules/etc, the manager repo itself, and already-tracked paths; guesses description from package.json/README and an emoji from the name), all pre-checked with editable names → `POST /api/import` creates them. **Gotcha fixed:** the `To-Do List ` folder has a trailing space in its real name — import must not blindly `.strip()` paths (falls back to stripped only if raw path missing). "To-Do List" was imported for real as the test; the other 5 folders (lucky-planner, my-react-app, research-emailer, thatgirl, empty buget-planner) are left for the user to pick in the modal.

Bonus roadmap items done: **Delete project** (🗑 on card hover, hidden while running, confirm explains code folder is untouched) and **Search/filter projects** (header input, filters name+description).

### Same-day follow-ups (user-requested, built + tested)
- **Create folder + auto-start from New Project modal**: the project folder is now created on disk immediately at creation (`os.makedirs`), and a "Start a Claude session right away" checkbox (default ON) starts Claude + opens the Terminal window even with no opening message (`auto_start` flag on `POST /api/projects`). Tested live: folder created, tmux session up, then cleaned up.
- **Unread reply badges**: `meta.unread` increments whenever a chat reply finishes (both the `/message` watcher and `send_to_session` when notifying); `POST /api/projects/<pid>/seen` zeroes it. Project page marks seen on open + every 10s while open. Running page shows a red count badge on the project emoji + "N new replies since you looked" line (polls every 3s — note: `/api/running/stream` SSE is dead weight, backend never broadcasts to it). Sidebar Running badge turns red + pulses with total unread count when any exist (green count of running sessions otherwise).

### Older backlog (still not built)
- Screenshot comparison testing (visual diff for web apps)
- Blocked-session detection (when Claude is waiting for user input, flag it in Running tab)
- Mobile responsive layout
- Global memory (shared across all projects)
- Auth for remote access (phone browser)
- Native Terminal.app window image-paste behavior — still unverified

---

## 2026-07-20 session — Chat tab scroll, fixed for real

User reported "scroll doesn't work" repeatedly; took several rounds to find the actual cause(s) rather than guessing. In order:
1. **Modal overflow bug (real, fixed).** `.modal`/`.modal-overlay` (App.css) had no `max-height`/`overflow-y`, so on a short browser window a modal's bottom (Create/Cancel buttons) rendered off-screen with zero way to reach it. Fixed with `max-height: calc(100vh - 48px); overflow-y: auto` on `.modal` and `overflow-y: auto` on `.modal-overlay`.
2. **Chat.jsx live-pin bug (real, fixed).** `handleScroll` referenced an undeclared `atBottomRef`, and the `pauseLive()`/`resumeLive()` mechanism (described in the component's own comments) was never wired to any event, so the terminal snapped to the bottom on every SSE tick while Claude was generating — impossible to read scrollback while it was actively working. Fixed by having `handleScroll` call `pauseLive()`/`resumeLive()` based on distance from bottom.
3. **The actual root cause.** Claude Code's CLI runs inside tmux's *alternate screen buffer* (confirmed via `tmux display -p '#{alternate_on} history=#{history_size}'` → `1` / `0` on real sessions) — meaning tmux keeps **zero scrollback**, and `start_session` never set an explicit pane size, so tmux defaulted to a small ~23-row pane. The mirrored `.terminal-body` almost never had more than 23 lines to show, so there was rarely anything to scroll into, and the box looked mostly empty.
4. **Real fix.** `start_session` now creates tmux sessions at `-x 120 -y 200` and locks that size with `tmux set-option -w window-size manual` (so the native Terminal.app window attaching later — a smaller client — doesn't shrink it back down). Claude Code's TUI renders scrollback to fill whatever pane it's given, so this alone makes the mirror capture ~200 lines instead of ~23 — enough to genuinely overflow `.terminal-body` and get a **real native browser scrollbar**, filling the box properly. `Chat.jsx`'s wheel handler was simplified to a boundary trigger: normal wheel scroll is left alone (pure native scrolling, smooth, no jank); only when the user hits `scrollTop <= 0` and keeps scrolling up does it POST to the new `POST /api/projects/<pid>/scroll` endpoint (`{direction, count}`), which runs `tmux send-keys ... PageUp/PageDown` so Claude's own TUI pages back further (it says as much in its own UI: "scroll with PgUp/PgDn").
5. Already-running sessions don't get the bigger pane automatically (only new `start_session` calls do) — apply live via `tmux set-option -t claude_<pid> -w window-size manual && tmux resize-window -t claude_<pid> -x 120 -y 200` (safe, non-destructive, same as resizing any terminal).

All of it verified against a real, disposable `claude --dangerously-skip-permissions` session in a scratch directory (never the user's actual project sessions) — confirmed PageUp/PageDown genuinely scroll the CLI's transcript, confirmed the manual window-size survives a simulated smaller-client attach, and confirmed via headless Chrome that ordinary scrolling now fires zero API calls while the boundary fallback fires only when actually stuck at the top.

**Also:** repo had never been committed — initialized git, added a root `.gitignore` (excludes `venv/`, `data/` (personal chat histories/memory — not source), `.claude/settings.local.json`, pycache/logs), and made the initial commit.
