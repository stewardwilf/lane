"""State store with fcntl advisory locking."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from lane.config import LaneConfig, state_path, lock_path


@dataclass
class Worktree:
    id: str
    path: str
    status: str = "idle"  # idle, claiming, busy, releasing, error
    branch: str = ""
    task: str | None = None
    task_id: str | None = None
    pid: int | None = None
    tmux_session: str | None = None
    log_path: str | None = None
    started_at: str | None = None
    last_released_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Worktree:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PoolState:
    version: int = 1
    config: LaneConfig = field(default_factory=LaneConfig)
    worktrees: list[Worktree] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "config": self.config.to_dict(),
            "worktrees": [w.to_dict() for w in self.worktrees],
        }

    @classmethod
    def from_dict(cls, d: dict) -> PoolState:
        return cls(
            version=d.get("version", 1),
            config=LaneConfig.from_dict(d.get("config", {})),
            worktrees=[Worktree.from_dict(w) for w in d.get("worktrees", [])],
        )


def read_state(root: Path | None = None) -> PoolState:
    sp = state_path(root)
    if not sp.exists():
        raise SystemExit(f"fatal: no lane pool found at {sp}\nRun `lane init` first.")
    return PoolState.from_dict(json.loads(sp.read_text()))


def write_state(state: PoolState, root: Path | None = None) -> None:
    sp = state_path(root)
    sp.write_text(json.dumps(state.to_dict(), indent=2) + "\n")


@contextmanager
def with_state_lock(root: Path | None = None) -> Generator[PoolState, None, None]:
    """Acquire an advisory lock, yield mutable state, write on exit."""
    lp = lock_path(root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            state = read_state(root)
            yield state
            write_state(state, root)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
