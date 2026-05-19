#!/usr/bin/env bash
set -uo pipefail

WT_ID="$1"; WT_PATH="$2"; LOG="$3"; ROOT="$4"; PYTHON="$5"
shift 5

cd "$WT_PATH" || exit 1

# Ensure log directory exists
mkdir -p "$(dirname "$LOG")"

echo "[lane] agent started at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
echo "[lane] worktree: $WT_ID" >> "$LOG"
echo "[lane] command: $*" >> "$LOG"
echo "" >> "$LOG"

"$@" 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

echo "" >> "$LOG"
echo "[lane] agent exited with code $EXIT at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"

# Release the worktree using the same Python that launched us
cd "$ROOT" && "$PYTHON" -m lane auto-release "$WT_ID" >> "$LOG" 2>&1
