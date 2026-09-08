# Claude Manager

A dashboard for running and managing multiple [Claude Code](https://claude.com/claude-code) sessions in parallel, instead of juggling terminal tabs. Each project gets its own tmux-backed Claude Code session, mirrored live in the browser, with tooling wrapped around it: memory injection, automated code review, task splitting across parallel agents, and end-of-day reporting.

Built solo, iteratively, almost entirely by describing features to Claude Code itself and reviewing the diffs — this repo is also a running log of that process (see [`file.md`](./file.md)).

## What it does

- **Live terminal mirror** — every project's Claude Code session runs in a real tmux pane; the browser streams it over SSE, so you can watch and type into it like a native terminal, from any device on the LAN (including a phone).
- **Per-project memory** — a markdown memory hierarchy (global + per-project) gets injected into context automatically when a session starts.
- **Review loop** — after each chat turn, one independent Claude reviewer reads the diff. Findings go back into the live session, which fixes what it agrees with and then decides whether to close the review or ask for another pass (capped at 3 rounds as a backstop).
- **Split runs (parallel mini-agents)** — for a task too big to do sequentially, a planning pass reads the repo and proposes independent pieces. Each approved piece becomes its own `git worktree` + branch + full Claude Code session, all running concurrently with their own terminal and input box, so any one agent can be corrected mid-flight without touching the others. "Merge all" folds the branches back in one at a time, stopping at the first conflict.
- **Daily report** — closing all sessions at once triggers a per-project handoff summary, rendered to PDF and (optionally) emailed.
- **Context-bloat monitor** — watches each session's live context size (from Claude Code's own transcript) and, once it's bloated or the session has run for hours, has the agent write a handoff checkpoint into project memory and then `/compact` with instructions about what to keep. Manual "Trim" button in the project header.
- **Blocked-session watchdog, screenshot-diff test runner, native Terminal.app windows, mobile-responsive layout.**

## Stack

- **Backend:** Flask (Python), tmux for session management, SSE for live streaming
- **Frontend:** React + Vite
- **Storage:** flat JSON/markdown per project, no database

## Running it

```bash
cd backend && pip install -r requirements.txt && python server.py   # :8888
cd frontend && npm install && npm run dev                            # :5173
```

### Remote access (Tailscale)

To reach the dashboard from your phone with a stable `https://` URL (no LAN-IP
drift, works on campus Wi-Fi that blocks device-to-device traffic), see
[`docs/REMOTE-ACCESS.md`](./docs/REMOTE-ACCESS.md) and run
`scripts/tailscale-https.sh`. Hosting on a separate Windows/Linux box instead: [`docs/WINDOWS-SERVER.md`](./docs/WINDOWS-SERVER.md).

Auth and email-report delivery are both opt-in (`backend/set_password.py`, `backend/set_email.py`) — a fresh checkout runs with neither configured.

## Run as a background service (macOS)

Instead of keeping `python server.py` in a foreground terminal, install it as a launchd LaunchAgent: it starts at login, restarts itself if it dies, and the app stays reachable from your phone/Mac.

```bash
scripts/install-service.sh              # installs + starts com.agent-capitol.server on :8888
scripts/install-service.sh --port 9000  # or on another port
```

The installer renders `launchd/com.agent-capitol.server.plist` with absolute paths (repo root, `venv/bin/python`, `backend/server.py`) into `~/Library/LaunchAgents/`, then bootstraps it into your **gui** login domain (required: desktop notifications go through osascript). It refuses to install while something else — e.g. a foreground `python backend/server.py` — is listening on the port, so stop that first. Re-running it is safe; it reloads the service in place. Needs `frontend/dist` built (`cd frontend && npm run build`) since the service serves it.

Optional `backend/.env` (git-ignored, `KEY=VALUE` lines) is merged into the service environment:

```
CONTEXT_TRIM_TOKENS=120000     # context size at which a session gets checkpointed + compacted
CONTEXT_MAX_HOURS=8            # ...or earlier, once a session this old is past half that size
PORT=8888                      # --port on the installer overrides this
HOST=0.0.0.0
SSL_CERT=/path/fullchain.pem   # both set + present -> serves HTTPS
SSL_KEY=/path/privkey.pem
```

Day to day:

```bash
tail -f ~/Library/Logs/agent-capitol/server.err.log            # request log + tracebacks (stdout in server.out.log)
launchctl kickstart -k gui/$(id -u)/com.agent-capitol.server   # restart (e.g. after pulling changes)
launchctl print gui/$(id -u)/com.agent-capitol.server          # status / pid
scripts/uninstall-service.sh                                   # stop + remove (logs are kept)
```

## Run on a Windows PC as an always-on server (WSL2 + systemd)

To host it on a spare Windows machine and code from your phone from anywhere, the backend runs inside Ubuntu on WSL2 as a systemd service, and Tailscale on the Windows side publishes it. The full walkthrough — WSL install, power settings, auto-boot after Windows Update reboots, Tailscale, phone setup — is in [`docs/WINDOWS-SERVER.md`](./docs/WINDOWS-SERVER.md). The Ubuntu side is one script:

```bash
scripts/setup-linux.sh                                   # deps, venv, UI build, password, systemd service
scripts/setup-linux.sh --public-url https://pc.tailnet.ts.net   # later: tell the About tab its Tailscale address
```

`scripts/install-service-linux.sh` / `uninstall-service-linux.sh` manage just the service (`systemd/agent-capitol.service` is the template; logs via `journalctl -u agent-capitol -f`). The backend itself is portable: macOS-only bits (Terminal.app windows, Notification Center, `.local` names) are skipped on Linux, and `claude`/`tmux` are found on `PATH` (override with `CLAUDE_BIN` / `TMUX_BIN`).

## Mac app (Electron wrapper)

`desktop/` wraps the dashboard in a native macOS window (`Agent Capitol.app`) you can keep in the Dock. It is a thin Electron shell around `http://localhost:8888` — no bundled backend, no code signing.

```bash
cd desktop && npm install
npm run build          # unsigned .app under desktop/dist/mac*/Agent Capitol.app
npm run install-app    # copies it to /Applications (falls back to ~/Applications), replacing any old copy
npm start              # dev: run unpackaged from the repo
```

How it finds the backend: on launch it probes `http://localhost:$AGENT_CAPITOL_PORT` (default 8888). If something is already listening (the launchd service or a foreground `python backend/server.py`) it just opens it and never touches that process. Otherwise it spawns `<repo>/venv/bin/python backend/server.py` itself with `PORT` set, shows a "Starting…" page until the server answers (20s timeout, then the error plus the log path `~/.agent-capitol/logs/desktop-backend.log`), and kills that child again on Quit — only a backend it started itself. `frontend/dist` must be built, since the backend serves it.

Overrides (env vars win over `~/.agent-capitol/desktop.json`, which wins over the built-in defaults):

| env | `desktop.json` key | default |
|---|---|---|
| `AGENT_CAPITOL_PORT` | `port` | `8888` |
| `AGENT_CAPITOL_ROOT` | `root` | repo checkout (`desktop/..` for `npm start`; the absolute path baked in at build time for the packaged app) |
| `AGENT_CAPITOL_PYTHON` | `python` | `<root>/venv/bin/python` |
| `AGENT_CAPITOL_SERVER` | `server` | `<root>/backend/server.py` |

The window remembers its size/position, Cmd+R reloads, View → Toggle Developer Tools opens DevTools, and links to other origins open in your default browser. The icon is generated from `desktop/build/make-icon.py` (`npm run icon`).

## A note on scope

Sessions launch Claude Code with `--dangerously-skip-permissions`, and the whole point of the tool is letting an agent act on your machine. That's the intended, accepted trade-off for a personal local dev tool — it is **not** meant to be exposed beyond your own LAN, and this repo is shared as source to read/run yourself, not as a hosted service.
