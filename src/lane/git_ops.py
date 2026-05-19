"""Thin wrappers around git subprocess calls."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(args: list[str], cwd: Path | str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git"] + args
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def fetch(remote: str, cwd: Path | None = None) -> None:
    run_git(["fetch", remote], cwd=cwd)


def branch_exists_on_remote(remote: str, branch: str, cwd: Path | None = None) -> bool:
    r = run_git(["ls-remote", "--heads", remote, branch], cwd=cwd, check=False)
    return bool(r.stdout.strip())


def local_branch_exists(branch: str, cwd: Path | None = None) -> bool:
    r = run_git(["rev-parse", "--verify", branch], cwd=cwd, check=False)
    return r.returncode == 0


def create_worktree(worktree_path: str, branch: str, start_point: str, cwd: Path | None = None) -> None:
    """Create a worktree. Each worktree gets its own unique branch since git
    doesn't allow multiple worktrees on the same branch."""
    run_git(["worktree", "add", "-B", branch, worktree_path, start_point], cwd=cwd)


def remove_worktree(worktree_path: str, cwd: Path | None = None, force: bool = False) -> None:
    args = ["worktree", "remove", worktree_path]
    if force:
        args.append("--force")
    run_git(args, cwd=cwd, check=False)


def checkout_new_branch(branch: str, start_point: str, cwd: Path | None = None) -> None:
    run_git(["checkout", "-B", branch, start_point], cwd=cwd)


def checkout_branch(branch: str, cwd: Path | None = None) -> None:
    run_git(["checkout", branch], cwd=cwd)


def hard_reset(ref: str, cwd: Path | None = None) -> None:
    run_git(["reset", "--hard", ref], cwd=cwd)


def has_uncommitted_changes(cwd: Path | None = None) -> bool:
    r = run_git(["status", "--porcelain"], cwd=cwd)
    return bool(r.stdout.strip())


def add_all(cwd: Path | None = None) -> None:
    run_git(["add", "-A"], cwd=cwd)


def commit(message: str, cwd: Path | None = None) -> None:
    run_git(["commit", "-m", message], cwd=cwd)


def push(remote: str, branch: str, cwd: Path | None = None) -> None:
    run_git(["push", "-u", remote, branch], cwd=cwd)


def get_short_hash(ref: str = "HEAD", cwd: Path | None = None) -> str:
    r = run_git(["rev-parse", "--short", ref], cwd=cwd)
    return r.stdout.strip()


def clean_worktree(cwd: Path | None = None) -> None:
    """Remove untracked files and directories."""
    run_git(["clean", "-fdx"], cwd=cwd, check=False)
