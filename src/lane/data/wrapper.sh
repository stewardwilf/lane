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

# Capture output to log via tmux pipe-pane (agent sees a real tty)
if [ -n "${TMUX_PANE:-}" ]; then
    tmux pipe-pane -o "cat >> '$LOG'"
fi

"$@"
EXIT=$?

if [ -n "${TMUX_PANE:-}" ]; then
    tmux pipe-pane
fi

echo "" >> "$LOG"
echo "[lane] agent exited with code $EXIT at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"

# Mark as done — do NOT release. User decides when to release.
cd "$ROOT" && "$LANE_BIN" mark-done "$WT_ID" >> "$LOG" 2>&1
