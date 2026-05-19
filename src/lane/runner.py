"""Process supervision — tmux sessions and subprocess fallback."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def spawn_tmux(
    session_name: str,
    wt_id: str,
    wt_path: str,
    log_path: str,
    agent_cmd: list[str],
    task: str,
    root: Path,
) -> int:
    """Spawn an agent in a tmux session via the wrapper script.

    Returns the PID of the tmux server process for the session.
    """
    wrapper = Path(__file__).parent / "data" / "wrapper.sh"
    lane_bin = shutil.which("lane") or "lane"

    # Build the full command that tmux will run
    # The wrapper script handles: running the agent, logging, and auto-release
    cmd_parts = [str(wrapper), wt_id, wt_path, log_path, str(root), lane_bin] + agent_cmd + [task]
    shell_cmd = " ".join(_shell_quote(p) for p in cmd_parts)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, shell_cmd],
        check=True,
        capture_output=True,
    )

    # Get the PID of the shell running inside the tmux session
    r = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        return int(r.stdout.strip().splitlines()[0])

    # Fallback: return -1 if we can't get the PID
    return -1


def spawn_subprocess(
    wt_id: str,
    wt_path: str,
    log_path: str,
    agent_cmd: list[str],
    task: str,
    root: Path,
) -> int:
    """Spawn an agent as a detached subprocess (no tmux). Returns PID."""
    wrapper = Path(__file__).parent / "data" / "wrapper.sh"
    lane_bin = shutil.which("lane") or "lane"

    cmd = ["bash", str(wrapper), wt_id, wt_path, log_path, str(root), lane_bin] + agent_cmd + [task]

    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def kill_agent(pid: int, tmux_session: str | None = None) -> None:
    """Kill an agent process. If tmux, kill the session. Falls back to SIGTERM then SIGKILL."""
    if tmux_session:
        subprocess.run(
            ["tmux", "kill-session", "-t", tmux_session],
            capture_output=True,
            check=False,
        )

    if pid and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        # Wait up to 10 seconds, then SIGKILL
        for _ in range(20):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)  # Check if alive
            except ProcessLookupError:
                return

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def is_pid_alive(pid: int) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Alive but owned by another user


def _shell_quote(s: str) -> str:
    """Simple shell quoting."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"
