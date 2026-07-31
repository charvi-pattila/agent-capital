# Claude Manager

A dashboard for running and managing multiple [Claude Code](https://claude.com/claude-code) sessions in parallel, instead of juggling terminal tabs. Each project gets its own tmux-backed Claude Code session, mirrored live in the browser, with tooling wrapped around it: memory injection, automated code review, task splitting across parallel agents, and end-of-day reporting.

Built solo, iteratively, almost entirely by describing features to Claude Code itself and reviewing the diffs — this repo is also a running log of that process (see [`file.md`](./file.md)).

## What it does

- **Live terminal mirror** — every project's Claude Code session runs in a real tmux pane; the browser streams it over SSE, so you can watch and type into it like a native terminal, from any device on the LAN (including a phone).
- **Per-project memory** — a markdown memory hierarchy (global + per-project) gets injected into context automatically when a session starts.
- **Council review** — after each chat turn, 5 independent Claude reviewers evaluate the diff and vote; rejections get auto-fed back into the live session to fix, capped at 3 attempts.
- **Split runs (parallel mini-agents)** — for a task too big to do sequentially, a planning pass reads the repo and proposes independent pieces. Each approved piece becomes its own `git worktree` + branch + full Claude Code session, all running concurrently with their own terminal and input box, so any one agent can be corrected mid-flight without touching the others. "Merge all" folds the branches back in one at a time, stopping at the first conflict.
- **Daily report** — closing all sessions at once triggers a per-project handoff summary, rendered to PDF and (optionally) emailed.
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

Auth and email-report delivery are both opt-in (`backend/set_password.py`, `backend/set_email.py`) — a fresh checkout runs with neither configured.

## A note on scope

Sessions launch Claude Code with `--dangerously-skip-permissions`, and the whole point of the tool is letting an agent act on your machine. That's the intended, accepted trade-off for a personal local dev tool — it is **not** meant to be exposed beyond your own LAN, and this repo is shared as source to read/run yourself, not as a hosted service.
