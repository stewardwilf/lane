"""Stale-PID detection and auto-recovery."""

from __future__ import annotations

from pathlib import Path

from lane.runner import is_pid_alive
from lane.state import PoolState


def check_stale_workers(state: PoolState, root: Path | None = None) -> list[str]:
    """Check for busy worktrees with dead PIDs. Auto-releases them.

    Returns list of recovered wt IDs.
    """
    stale = []
    for wt in state.worktrees:
        if wt.status in ("busy", "claiming") and wt.pid is not None:
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
