"""Stale-PID detection and auto-recovery."""

from __future__ import annotations

from pathlib import Path

from lane.runner import is_pid_alive, is_tmux_session_alive
from lane.state import PoolState


def check_stale_workers(state: PoolState, root: Path | None = None) -> list[str]:
    """Check for busy worktrees whose agent has exited.

    Uses tmux session existence as the primary check (more reliable than PID),
    falls back to PID check for non-tmux workers.

    Returns list of stale wt IDs.
    """
    stale = []
    for wt in state.worktrees:
        if wt.status not in ("busy",):
            continue

        # If there's a tmux session, check that
        if wt.tmux_session:
            if not is_tmux_session_alive(wt.tmux_session):
                stale.append(wt.id)
        # Otherwise fall back to PID, but only if we have a real PID
        elif wt.pid is not None and wt.pid > 0:
            if not is_pid_alive(wt.pid):
                stale.append(wt.id)

    return stale


def auto_recover(root: Path, wt_ids: list[str]) -> None:
    """Release stale worktrees back to idle."""
    if not wt_ids:
        return
    from lane.cli import _do_release
    for wt_id in wt_ids:
        try:
            _do_release(root, wt_id, quiet=True)
        except Exception:
            pass
