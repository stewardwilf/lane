"""Default configuration and loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


LANE_DIR = ".lane"
STATE_FILE = "state.json"
LOCK_FILE = "state.lock"
LOGS_DIR = "logs"
WORKTREES_DIR = "worktrees"


@dataclass
class LaneConfig:
    base_branch: str = "main"
    holding_branch: str = "lane/idle"
    remote: str = "origin"
    pool_dir: str = os.path.join(LANE_DIR, WORKTREES_DIR)
    logs_dir: str = os.path.join(LANE_DIR, LOGS_DIR)
    agent_cmd: list[str] = field(default_factory=lambda: ["claude", "-p"])
    use_tmux: bool = True
    push_on_release: bool = False
    setup_script: str | None = None

    def to_dict(self) -> dict:
        return {
            "base_branch": self.base_branch,
            "holding_branch": self.holding_branch,
            "remote": self.remote,
            "pool_dir": self.pool_dir,
            "logs_dir": self.logs_dir,
            "agent_cmd": self.agent_cmd,
            "use_tmux": self.use_tmux,
            "push_on_release": self.push_on_release,
            "setup_script": self.setup_script,
        }

    @classmethod
    def from_dict(cls, d: dict) -> LaneConfig:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def find_root() -> Path:
    """Find the git repo root (where .git lives)."""
    cwd = Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists():
            return p
    raise SystemExit("fatal: not a git repository (or any parent)")


def lane_dir(root: Path | None = None) -> Path:
    if root is None:
        root = find_root()
    return root / LANE_DIR


def state_path(root: Path | None = None) -> Path:
    return lane_dir(root) / STATE_FILE


def lock_path(root: Path | None = None) -> Path:
    return lane_dir(root) / LOCK_FILE
