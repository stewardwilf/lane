# lane

A worktree pool manager for parallel AI coding agents.

Lane manages a fixed pool of pre-warmed git worktrees so you can dispatch multiple AI coding agents (Claude Code, etc.) into isolated checkouts of the same repo — without file conflicts, without re-installing dependencies every time, and with one dashboard to watch them all.

## Install

```bash
# pipx (recommended — isolated install, on your PATH)
pipx install lane

# or uv
uv tool install lane

# or from source
git clone https://github.com/wilfsteward/lane
cd lane && uv sync && uv run lane --help
```

**Prerequisites:** git >= 2.20, python >= 3.11, tmux (recommended; fallback exists but you lose `attach`)

## Quick start

```bash
cd ~/code/my-project

# 1. Initialise a pool of 4 worktrees off main
lane init --count 4 --base main

# 2. Dispatch tasks
lane task "Refactor auth middleware to use JWT"
lane task "Add pagination to /api/orders"
lane task "Fix the broken redirect on /login"

# 3. Watch them work
lane dashboard

# 4. Inspect or intervene
lane attach wt-02      # join the agent's tmux session
lane logs wt-02 -f     # just tail the log
lane stop wt-02        # kill the agent (auto-releases)
```

## Commands

| Command | Description |
|---|---|
| `lane init` | Create N worktrees in the current repo |
| `lane task <description>` | Dispatch a task to an idle worktree |
| `lane status [--watch]` | Show pool status |
| `lane dashboard` | Open the TUI dashboard |
| `lane logs <wt-id> [-f]` | Tail a worktree's log |
| `lane attach <wt-id>` | Attach to a tmux session |
| `lane stop <wt-id>` | Kill an agent (auto-releases) |
| `lane release <wt-id>` | Manually release a worktree |
| `lane destroy` | Tear down the entire pool |

## How it works

1. **`lane init`** creates N sibling worktrees and pins each to a holding branch forked from your base branch. You do this once per project.

2. **`lane task`** atomically claims an idle worktree, creates a `task/<slug>` branch off the base, and launches your agent in a tmux session.

3. When the agent finishes (or you `lane stop` it), the wrapper script auto-commits any uncommitted work as a WIP save, then resets the worktree back to the holding branch so it's ready for the next task.

4. **`lane dashboard`** gives you a live TUI with all slots, their status, and a log tail for the selected worktree. Press `a` to attach, `s` to stop, `r` to release.

## Configuration

`lane init` accepts these options:

```
--count, -n     Number of worktrees (default: 4)
--base, -b      Base branch to fork tasks from (default: main)
--holding       Holding branch prefix (default: lane/idle)
--remote        Git remote (default: origin)
--agent-cmd     Agent command, space-separated (default: "claude -p")
--setup-script  Script to run in each worktree after creation
```

The agent command receives the task description as a final argument:
```bash
# default: claude -p "your task description here"
# custom:  aider --message "your task description here"
lane init --agent-cmd "aider --message"
```

## License

MIT
