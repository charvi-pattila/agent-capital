#!/bin/bash
# Opens a real Claude Code session in tmux and attaches to it in Terminal
# Usage: chat_runner.sh <chat_id> <chat_name>

CHAT_ID="$1"
SESSION="claude_$CHAT_ID"

# Start a new tmux session running real Claude Code
tmux new-session -d -s "$SESSION" "claude --dangerously-skip-permissions" 2>/dev/null || true

# Wait for claude to start
sleep 2

# Signal server that terminal is ready
touch "/tmp/phone_ready_${CHAT_ID}"

# Attach so the user sees the full Claude Code TUI
tmux attach-session -t "$SESSION"
