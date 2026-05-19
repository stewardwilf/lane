"""Stale-PID detection — marks done, never auto-releases."""

from __future__ import annotations

from pathlib import Path

from lane.runner import is_pid_alive, is_tmux_session_alive
from lane.state import PoolState, write_state


def check_stale_workers(state: PoolState, root: Path | None = None) -> list[str]:
    """Check for busy worktrees whose agent has exited.

    Marks them as 'done' so the user can continue or release.
    Returns list of worktree IDs that were marked done.
    """
    marked = []
    for wt in state.worktrees:
        if wt.status != "busy":
            continue

        dead = False
        if wt.tmux_session:
            dead = not is_tmux_session_alive(wt.tmux_session)
        elif wt.pid is not None and wt.pid > 0:
            dead = not is_pid_alive(wt.pid)

        if dead:
            wt.status = "done"
            wt.pid = None
            wt.tmux_session = None
            marked.append(wt.id)

    if marked and root:
        write_state(state, root)

    return marked
