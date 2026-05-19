"""Stale-PID detection and recovery."""

from __future__ import annotations

from lane.runner import is_pid_alive
from lane.state import PoolState


def check_stale_workers(state: PoolState) -> list[str]:
    """Check for busy worktrees with dead PIDs. Returns list of stale wt IDs."""
    stale = []
    for wt in state.worktrees:
        if wt.status == "busy" and wt.pid is not None:
            if not is_pid_alive(wt.pid):
                wt.status = "error"
                stale.append(wt.id)
    return stale
