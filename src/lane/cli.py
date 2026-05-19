"""CLI commands — the full lane surface."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lane import __version__
from lane.config import LaneConfig, find_root, lane_dir, LANE_DIR
from lane.state import PoolState, Worktree, read_state, write_state, with_state_lock, now_iso
from lane import git_ops
from lane.runner import tmux_available, spawn_tmux, spawn_subprocess, kill_agent
from lane.recovery import check_stale_workers
from lane.recovery import check_stale_workers

app = typer.Typer(
    name="lane",
    help="A worktree pool manager for parallel AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# ── init ────────────────────────────────────────────────────────

@app.command()
def init(
    count: int = typer.Option(4, "--count", "-n", help="Number of worktrees to create."),
    base: str = typer.Option("main", "--base", "-b", help="Base branch to fork tasks from."),
    holding: str = typer.Option("lane/idle", "--holding", help="Holding branch for idle worktrees."),
    remote: str = typer.Option("origin", "--remote", help="Git remote name."),
    agent_cmd: str = typer.Option("claude -p", "--agent-cmd", help="Agent command (space-separated)."),
    setup_script: str = typer.Option(None, "--setup-script", help="Script to run in each worktree after creation."),
):
    """Initialise a lane pool in the current git repo."""
    root = find_root()
    ld = lane_dir(root)

    if (ld / "state.json").exists():
        console.print("[yellow]Pool already initialised.[/yellow] Run [bold]lane destroy[/bold] first to reinitialise.")
        raise typer.Exit(1)

    # Ensure tmux is available
    _ensure_tmux()

    # Verify base branch exists
    console.print(f"Fetching from {remote}...")
    git_ops.fetch(remote, cwd=root)

    remote_base = f"{remote}/{base}"
    if not git_ops.local_branch_exists(remote_base, cwd=root):
        console.print(f"[red]Branch {remote_base} not found.[/red]")
        raise typer.Exit(1)

    # Ensure pool dir is in .gitignore
    gitignore = root / ".gitignore"
    ignore_entry = LANE_DIR + "/"
    if gitignore.exists():
        content = gitignore.read_text()
        if ignore_entry not in content:
            console.print(f"Adding [bold]{ignore_entry}[/bold] to .gitignore")
            with open(gitignore, "a") as f:
                f.write(f"\n{ignore_entry}\n")
    else:
        gitignore.write_text(f"{ignore_entry}\n")

    # Create directory structure
    ld.mkdir(parents=True, exist_ok=True)
    (ld / "logs").mkdir(exist_ok=True)
    (ld / "worktrees").mkdir(exist_ok=True)

    # Build config
    cfg = LaneConfig(
        base_branch=base,
        holding_branch=holding,
        remote=remote,
        pool_dir=os.path.join(LANE_DIR, "worktrees"),
        logs_dir=os.path.join(LANE_DIR, "logs"),
        agent_cmd=agent_cmd.split(),
        use_tmux=tmux_available(),
        setup_script=setup_script,
    )

    worktrees: list[Worktree] = []

    for i in range(1, count + 1):
        wt_id = f"wt-{i:02d}"
        wt_rel_path = os.path.join(cfg.pool_dir, wt_id)
        wt_abs_path = str(root / wt_rel_path)
        # Each worktree needs a unique branch (git requirement)
        wt_branch = f"{holding}/{wt_id}"

        console.print(f"  Creating worktree [bold]{wt_id}[/bold]...")
        git_ops.create_worktree(wt_abs_path, wt_branch, remote_base, cwd=root)

        worktrees.append(Worktree(
            id=wt_id,
            path=wt_rel_path,
            status="idle",
            branch=wt_branch,
            last_released_at=now_iso(),
        ))

    # Run setup script if provided
    if setup_script:
        for wt in worktrees:
            wt_abs = str(root / wt.path)
            console.print(f"  Running setup script in {wt.id}...")
            subprocess.run(["bash", setup_script], cwd=wt_abs, check=False)

    # Write state
    state = PoolState(version=1, config=cfg, worktrees=worktrees)
    write_state(state, root)

    console.print(f"\n[green]Pool initialised with {count} worktrees.[/green]")
    console.print(f"  Base:    {base}")
    console.print(f"  Holding: {holding}")
    console.print(f"  Agent:   {' '.join(cfg.agent_cmd)}")
    if not cfg.use_tmux:
        console.print("  [yellow]tmux not found — attach/detach will be unavailable.[/yellow]")


# ── task ────────────────────────────────────────────────────────

@app.command()
def task(
    description: str = typer.Argument(..., help="Task description for the agent."),
    task_id: str = typer.Option(None, "--id", help="Custom task ID (default: random hex)."),
    branch_prefix: str = typer.Option("task/", "--branch-prefix", help="Branch name prefix."),
    background: bool = typer.Option(False, "--background", "--bg", help="Dispatch and return immediately."),
):
    """Dispatch a task to an idle worktree."""
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    import time

    root = find_root()

    if task_id is None:
        task_id = "t-" + secrets.token_hex(4)

    slug = _slugify(description)
    branch_name = f"{branch_prefix}{slug}-{task_id}"

    # ── Step 1: Claim ──────────────────────────────────────────
    _step(f"Claiming idle worktree")
    claimed_id: str | None = None
    with with_state_lock(root) as state:
        for wt in state.worktrees:
            if wt.status == "idle":
                wt.status = "claiming"
                claimed_id = wt.id
                break

        if claimed_id is None:
            console.print("[red]  No idle worktrees available.[/red] All slots are busy.")
            raise typer.Exit(75)  # EX_TEMPFAIL

    _done(f"Claimed [bold]{claimed_id}[/bold]")

    # ── Step 2: Branch ─────────────────────────────────────────
    claimed_wt = next(w for w in state.worktrees if w.id == claimed_id)
    wt_abs = str(root / claimed_wt.path)
    remote = state.config.remote
    base_ref = f"{remote}/{state.config.base_branch}"

    _step(f"Fetching {remote} and creating branch")
    try:
        git_ops.fetch(remote, cwd=wt_abs)
        git_ops.checkout_new_branch(branch_name, base_ref, cwd=wt_abs)
    except Exception as e:
        with with_state_lock(root) as state:
            for wt in state.worktrees:
                if wt.id == claimed_id:
                    wt.status = "idle"
                    wt.branch = state.config.holding_branch
                    break
        console.print(f"[red]  Failed:[/red] {e}")
        raise typer.Exit(1)

    _done(f"Branch [dim]{branch_name}[/dim]")

    # ── Step 3: Spawn ──────────────────────────────────────────
    log_file = os.path.join(str(root), state.config.logs_dir, f"{claimed_id}.log")
    # Clear any old log
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(log_file).write_text("")

    tmux_session = f"lane:{claimed_id}"

    with with_state_lock(root) as state:
        for wt in state.worktrees:
            if wt.id == claimed_id:
                wt.status = "busy"
                wt.branch = branch_name
                wt.task = description
                wt.task_id = task_id
                wt.log_path = log_file
                wt.tmux_session = tmux_session
                wt.started_at = now_iso()
                break

    _step("Starting agent")
    if state.config.use_tmux and tmux_available():
        pid = spawn_tmux(
            session_name=tmux_session,
            wt_id=claimed_id,
            wt_path=wt_abs,
            log_path=log_file,
            agent_cmd=state.config.agent_cmd,
            task=description,
            root=root,
        )
    else:
        pid = spawn_subprocess(
            wt_id=claimed_id,
            wt_path=wt_abs,
            log_path=log_file,
            agent_cmd=state.config.agent_cmd,
            task=description,
            root=root,
        )

    with with_state_lock(root) as state:
        for wt in state.worktrees:
            if wt.id == claimed_id:
                wt.pid = pid
                break

    agent_name = " ".join(state.config.agent_cmd)
    _done(f"Agent running ({agent_name}) · pid {pid}")

    # ── Background mode: print summary and exit ────────────────
    if background:
        console.print(f"\n  [dim]Task ID[/dim]  {task_id}")
        console.print(f"  [dim]Attach[/dim]   lane attach {claimed_id}")
        console.print(f"  [dim]Logs[/dim]     lane logs {claimed_id} -f")
        return

    # ── Live streaming mode (default) ──────────────────────────
    console.print()
    _stream_log(root, claimed_id, log_file, description, branch_name, task_id)


# ── status ──────────────────────────────────────────────────────

@app.command()
def status(
    watch: bool = typer.Option(False, "--watch", "-w", help="Continuously refresh."),
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: table or json."),
):
    """Show pool status."""
    import json as json_mod
    import time

    root = find_root()

    while True:
        state = read_state(root)
        stale = check_stale_workers(state)
        if stale:
            write_state(state, root)

        if fmt == "json":
            console.print_json(json_mod.dumps(state.to_dict()))
        else:
            _print_status_table(state)

        if not watch:
            break
        time.sleep(2)


# ── dashboard ───────────────────────────────────────────────────

@app.command()
def dashboard():
    """Open the TUI dashboard."""
    from lane.tui.app import LaneDashboard
    root = find_root()
    app = LaneDashboard(root=root)
    app.run()


# ── logs ────────────────────────────────────────────────────────

@app.command()
def logs(
    wt_id: str = typer.Argument(..., help="Worktree ID (e.g. wt-01)."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output."),
):
    """Tail logs for a worktree."""
    root = find_root()
    state = read_state(root)
    wt = _find_worktree(state, wt_id)

    if not wt.log_path:
        console.print(f"[yellow]No log file for {wt_id}.[/yellow]")
        raise typer.Exit(1)

    log_file = Path(wt.log_path)
    if not log_file.exists():
        console.print(f"[yellow]Log file not found: {log_file}[/yellow]")
        raise typer.Exit(1)

    args = ["tail"]
    if follow:
        args.append("-f")
    args.extend(["-n", "100", str(log_file)])

    try:
        subprocess.run(args)
    except KeyboardInterrupt:
        pass


# ── attach ──────────────────────────────────────────────────────

@app.command()
def attach(
    wt_id: str = typer.Argument(..., help="Worktree ID to attach to."),
):
    """Attach to a worktree's tmux session."""
    root = find_root()
    state = read_state(root)
    wt = _find_worktree(state, wt_id)

    if not wt.tmux_session:
        console.print(f"[yellow]No tmux session for {wt_id}.[/yellow]")
        raise typer.Exit(1)

    os.execvp("tmux", ["tmux", "attach-session", "-t", wt.tmux_session])


# ── stop ────────────────────────────────────────────────────────

@app.command()
def stop(
    wt_id: str = typer.Argument(..., help="Worktree ID to stop."),
):
    """Kill the agent process in a worktree (wrapper auto-releases)."""
    root = find_root()
    state = read_state(root)
    wt = _find_worktree(state, wt_id)

    if wt.status not in ("busy", "error"):
        console.print(f"[yellow]{wt_id} is {wt.status}, not running.[/yellow]")
        raise typer.Exit(1)

    console.print(f"Stopping agent in [bold]{wt_id}[/bold]...")
    kill_agent(wt.pid, wt.tmux_session)
    console.print(f"[green]Stopped.[/green] The wrapper script will auto-release the worktree.")


# ── release ─────────────────────────────────────────────────────

@app.command()
def release(
    wt_id: str = typer.Argument(..., help="Worktree ID to release."),
):
    """Manually release a worktree back to idle."""
    root = find_root()
    _do_release(root, wt_id)
    console.print(f"[green]Released {wt_id}.[/green]")


# ── _release (internal) ────────────────────────────────────────

@app.command(hidden=True)
def _release(
    wt_id: str = typer.Argument(..., help="Worktree ID."),
):
    """Internal: called by wrapper.sh on agent exit."""
    root = find_root()
    _do_release(root, wt_id, quiet=True)


# ── destroy ─────────────────────────────────────────────────────

@app.command()
def destroy(
    force: bool = typer.Option(False, "--force", help="Force destroy even with busy worktrees."),
):
    """Remove all worktrees and lane state."""
    root = find_root()
    state = read_state(root)

    busy = [w for w in state.worktrees if w.status == "busy"]
    if busy and not force:
        console.print(f"[red]{len(busy)} worktree(s) are busy.[/red] Use --force to destroy anyway.")
        raise typer.Exit(1)

    # Kill any running agents
    for wt in state.worktrees:
        if wt.status == "busy" and wt.pid:
            kill_agent(wt.pid, wt.tmux_session)

    # Remove worktrees
    for wt in state.worktrees:
        wt_abs = str(root / wt.path)
        console.print(f"  Removing worktree [bold]{wt.id}[/bold]...")
        git_ops.remove_worktree(wt_abs, cwd=root, force=True)

    # Clean up lane directory
    import shutil
    ld = lane_dir(root)
    if ld.exists():
        shutil.rmtree(ld)

    console.print("[green]Pool destroyed.[/green]")


# ── version ─────────────────────────────────────────────────────

@app.command()
def version():
    """Print lane version."""
    console.print(f"lane {__version__}")


# ── helpers ─────────────────────────────────────────────────────

def _ensure_tmux() -> None:
    """Check for tmux and offer to install it if missing."""
    if tmux_available():
        return

    import shutil
    if shutil.which("brew"):
        console.print("[yellow]tmux not found.[/yellow] tmux is required for attach/detach.")
        if typer.confirm("Install tmux via Homebrew?", default=True):
            console.print("Installing tmux...")
            subprocess.run(["brew", "install", "tmux"], check=True)
            if tmux_available():
                console.print("[green]tmux installed.[/green]")
                return

    if not tmux_available():
        console.print("[yellow]tmux not found — attach/detach will be unavailable.[/yellow]")
        console.print("Install it manually: [bold]brew install tmux[/bold] (macOS) or [bold]apt install tmux[/bold] (Linux)")


def _find_worktree(state: PoolState, wt_id: str) -> Worktree:
    for wt in state.worktrees:
        if wt.id == wt_id:
            return wt
    console.print(f"[red]Worktree {wt_id} not found.[/red]")
    raise typer.Exit(1)


def _do_release(root: Path, wt_id: str, quiet: bool = False) -> None:
    """Perform the full release cycle for a worktree."""
    state = read_state(root)
    wt = _find_worktree(state, wt_id)
    cfg = state.config

    if wt.status == "idle":
        if not quiet:
            console.print(f"[yellow]{wt_id} is already idle.[/yellow]")
        return

    wt_abs = str(root / wt.path)

    # Auto-commit any uncommitted work
    try:
        if git_ops.has_uncommitted_changes(cwd=wt_abs):
            task_desc = wt.task or "unknown task"
            git_ops.add_all(cwd=wt_abs)
            git_ops.commit(f"WIP: {task_desc} [lane autosave]", cwd=wt_abs)
            if not quiet:
                console.print(f"  Auto-committed WIP changes in {wt_id}.")
    except Exception:
        pass  # Don't fail release on commit errors

    # Push if configured
    if cfg.push_on_release and wt.branch:
        try:
            git_ops.push(cfg.remote, wt.branch, cwd=wt_abs)
        except Exception:
            pass

    # Reset to per-worktree holding branch
    wt_holding = f"{cfg.holding_branch}/{wt_id}"
    try:
        git_ops.checkout_new_branch(wt_holding, f"{cfg.remote}/{cfg.base_branch}", cwd=wt_abs)
    except Exception:
        pass  # Best effort

    # Update state under lock
    with with_state_lock(root) as state:
        for w in state.worktrees:
            if w.id == wt_id:
                w.status = "idle"
                w.branch = wt_holding
                w.task = None
                w.task_id = None
                w.pid = None
                w.tmux_session = None
                w.log_path = None
                w.started_at = None
                w.last_released_at = now_iso()
                break


def _step(msg: str) -> None:
    console.print(f"  [dim]>[/dim] {msg}...")


def _done(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def _stream_log(root: Path, wt_id: str, log_file: str, task_desc: str, branch: str, task_id: str) -> None:
    """Stream agent log output live until the task finishes or user hits Ctrl+C."""
    import time
    from rich.rule import Rule

    log_path = Path(log_file)
    seen = 0

    console.print(Rule(f"[bold]{wt_id}[/bold] · {task_desc}", style="dim"))
    console.print(f"  [dim]branch[/dim]  {branch}")
    console.print(f"  [dim]task[/dim]    {task_id}")
    console.print(f"  [dim]ctrl+c[/dim]  detach (agent keeps running)")
    console.print()

    try:
        while True:
            # Check if still running
            state = read_state(root)
            wt = next((w for w in state.worktrees if w.id == wt_id), None)
            finished = wt is None or wt.status not in ("busy", "claiming")

            # Read new log lines
            if log_path.exists():
                with open(log_path, "r") as f:
                    f.seek(seen)
                    new = f.read()
                    if new:
                        seen += len(new)
                        for line in new.splitlines():
                            _print_log_line(line)

            if finished:
                console.print()
                if wt and wt.status == "error":
                    console.print(Rule("[red]agent exited with error[/red]", style="red"))
                else:
                    console.print(Rule("[green]task complete[/green]", style="green"))
                    console.print(f"  [dim]Branch[/dim] [bold]{branch}[/bold] [dim]has the changes.[/dim]")
                break

            time.sleep(0.3)

    except KeyboardInterrupt:
        console.print()
        console.print(Rule("[yellow]detached[/yellow]", style="yellow"))
        console.print(f"  Agent is still running in [bold]{wt_id}[/bold].")
        console.print(f"  [dim]Reattach:[/dim]  lane logs {wt_id} -f")
        console.print(f"  [dim]Attach:[/dim]    lane attach {wt_id}")
        console.print(f"  [dim]Stop:[/dim]      lane stop {wt_id}")


def _print_log_line(line: str) -> None:
    """Print a single log line with contextual styling."""
    stripped = line.strip()
    if not stripped:
        return

    # Lane system messages
    if stripped.startswith("[lane]"):
        console.print(f"  [dim]{stripped}[/dim]")
        return

    # Common agent patterns
    if any(stripped.startswith(p) for p in ("PASS", "✓", "ok ")):
        console.print(f"  [green]{stripped}[/green]")
    elif any(stripped.startswith(p) for p in ("FAIL", "ERROR", "✗", "error:", "Error:")):
        console.print(f"  [red]{stripped}[/red]")
    elif stripped.startswith("$") or stripped.startswith("> "):
        console.print(f"  [yellow]{stripped}[/yellow]")
    elif any(stripped.startswith(p) for p in ("warning:", "Warning:", "WARN")):
        console.print(f"  [yellow]{stripped}[/yellow]")
    else:
        console.print(f"  {stripped}")


def _print_status_table(state: PoolState) -> None:
    busy = sum(1 for w in state.worktrees if w.status == "busy")
    idle = sum(1 for w in state.worktrees if w.status == "idle")
    error = sum(1 for w in state.worktrees if w.status == "error")
    total = len(state.worktrees)

    console.print(f"\n[bold]lane[/bold] — pool={total}  [blue]busy={busy}[/blue]  [green]idle={idle}[/green]  [red]errors={error}[/red]\n")

    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
    table.add_column("ID", style="dim", width=8)
    table.add_column("Status", width=10)
    table.add_column("Task", min_width=30)
    table.add_column("Branch", style="dim", min_width=20)
    table.add_column("Elapsed", justify="right", width=10)

    for wt in state.worktrees:
        status_str = _status_styled(wt.status)
        task_str = wt.task or "—"
        branch_str = wt.branch or "—"
        elapsed = _elapsed(wt.started_at) if wt.started_at else "—"

        table.add_row(wt.id, status_str, task_str, branch_str, elapsed)

    console.print(table)
    console.print()


def _status_styled(status: str) -> str:
    colors = {
        "idle": "[green]IDLE[/green]",
        "busy": "[blue]BUSY[/blue]",
        "claiming": "[yellow]CLAIM[/yellow]",
        "releasing": "[yellow]RELEASE[/yellow]",
        "error": "[red]ERROR[/red]",
    }
    return colors.get(status, status)


def _elapsed(started_at: str | None) -> str:
    if not started_at:
        return "—"
    from datetime import datetime, timezone
    try:
        start = datetime.fromisoformat(started_at)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    except Exception:
        return "—"


def _slugify(text: str, max_len: int = 30) -> str:
    """Turn a task description into a branch-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    slug = slug.strip("-")[:max_len].rstrip("-")
    return slug or "task"


def dispatch_task_headless(root: Path, description: str) -> tuple[str, str | None]:
    """Dispatch a task without any console output. Returns (wt_id, error_msg).

    Used by the TUI dashboard. On success error_msg is None.
    """
    task_id = "t-" + secrets.token_hex(4)
    slug = _slugify(description)
    branch_name = f"task/{slug}-{task_id}"

    # Claim
    claimed_id: str | None = None
    with with_state_lock(root) as state:
        for wt in state.worktrees:
            if wt.status == "idle":
                wt.status = "claiming"
                claimed_id = wt.id
                break
    if claimed_id is None:
        return ("", "No idle worktrees — all slots busy")

    # Branch
    state = read_state(root)
    claimed_wt = next(w for w in state.worktrees if w.id == claimed_id)
    wt_abs = str(root / claimed_wt.path)
    remote = state.config.remote
    base_ref = f"{remote}/{state.config.base_branch}"

    try:
        git_ops.fetch(remote, cwd=wt_abs)
        git_ops.checkout_new_branch(branch_name, base_ref, cwd=wt_abs)
    except Exception as e:
        with with_state_lock(root) as state:
            for wt in state.worktrees:
                if wt.id == claimed_id:
                    wt.status = "idle"
                    wt.branch = f"{state.config.holding_branch}/{claimed_id}"
                    break
        return (claimed_id, f"Git setup failed: {e}")

    # Spawn
    log_file = os.path.join(str(root), state.config.logs_dir, f"{claimed_id}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(log_file).write_text("")
    tmux_session = f"lane:{claimed_id}"

    with with_state_lock(root) as state:
        for wt in state.worktrees:
            if wt.id == claimed_id:
                wt.status = "busy"
                wt.branch = branch_name
                wt.task = description
                wt.task_id = task_id
                wt.log_path = log_file
                wt.tmux_session = tmux_session
                wt.started_at = now_iso()
                break

    if state.config.use_tmux and tmux_available():
        pid = spawn_tmux(
            session_name=tmux_session, wt_id=claimed_id, wt_path=wt_abs,
            log_path=log_file, agent_cmd=state.config.agent_cmd,
            task=description, root=root,
        )
    else:
        pid = spawn_subprocess(
            wt_id=claimed_id, wt_path=wt_abs, log_path=log_file,
            agent_cmd=state.config.agent_cmd, task=description, root=root,
        )

    with with_state_lock(root) as state:
        for wt in state.worktrees:
            if wt.id == claimed_id:
                wt.pid = pid
                break

    return (claimed_id, None)


