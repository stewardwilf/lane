#!/usr/bin/env bash
set -uo pipefail

WT_ID="$1"; WT_PATH="$2"; LOG="$3"; ROOT="$4"; LANE_BIN="$5"
shift 5

cd "$WT_PATH" || exit 1

mkdir -p "$(dirname "$LOG")"

echo "[lane] agent started at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
echo "[lane] worktree: $WT_ID" >> "$LOG"
echo "[lane] command: $*" >> "$LOG"
echo "" >> "$LOG"

# Capture pane output to log file. The agent sees a real tty (no buffering).
if [ -n "${TMUX_PANE:-}" ]; then
    tmux pipe-pane -o "cat >> \"$LOG\""
fi

# Run the agent — prompt is the last argument(s)
"$@"
EXIT=$?

# Stop capturing
if [ -n "${TMUX_PANE:-}" ]; then
    tmux pipe-pane
fi

echo "" >> "$LOG"
echo "[lane] agent exited with code $EXIT at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"

cd "$ROOT" && "$LANE_BIN" mark-done "$WT_ID" >> "$LOG" 2>&1
