# lane

A worktree pool manager for parallel AI coding agents.

Lane is a thin orchestration layer around Claude Code (or any agent). It manages a fixed pool of pre-warmed git worktrees so you can run multiple Claude sessions in parallel on the same repo — each in an isolated checkout, all visible from one dashboard. You don't lose any Claude functionality: every session is a real interactive Claude session you can attach to and use normally.

## Install

```bash
# uv (recommended)
uv tool install git+https://github.com/stewardwilf/lane.git

# or pipx
pipx install git+https://github.com/stewardwilf/lane.git

# or from source
git clone https://github.com/stewardwilf/lane
cd lane && uv sync && uv run lane --help
```

To update to the latest version:
```bash
uv tool install --force git+https://github.com/stewardwilf/lane.git
```

**Prerequisites:** git >= 2.20, python >= 3.11. tmux is installed automatically via Homebrew during `lane init` if missing.

## Quick start

```bash
cd ~/code/my-project

# 1. Initialise a pool of 4 worktrees off main
lane init --count 4 --base main

# 2. Open the dashboard
lane dashboard

# 3. Press `n` to dispatch a task — Claude starts in its own worktree
# 4. Press `a` to attach — you're in a full Claude session
#    (approve tools, run skills, type follow-ups — everything works)
# 5. Ctrl+B, D to detach back to the dashboard
# 6. Dispatch more tasks to other worktrees
```

Or from the CLI:
```bash
lane task "Refactor auth middleware to use JWT"
lane task --bg "Add pagination to /api/orders"
lane attach wt-01
```

## Commands

| Command | Description |
|---|---|
| `lane init` | Create N worktrees in the current repo |
| `lane task <description>` | Dispatch a task to an idle worktree |
| `lane dashboard` | Open the TUI dashboard |
| `lane attach <wt-id>` | Attach to a Claude session (full interactive) |
| `lane status [--watch]` | Show pool status |
| `lane logs <wt-id> [-f]` | Tail a worktree's log |
| `lane stop <wt-id>` | Kill an agent (auto-releases) |
| `lane release <wt-id>` | Manually release a worktree |
| `lane destroy` | Tear down the entire pool |

### Dashboard keybinds

| Key | Action |
|---|---|
| `n` | Dispatch a new task |
| `a` | Attach to the selected worktree's Claude session |
| `s` | Stop the selected agent |
| `r` | Release the selected worktree |
| `q` | Quit |

## How it works

1. **`lane init`** creates N sibling worktrees and pins each to a holding branch forked from your base. You do this once per project.

2. **`lane task`** (or `n` in the dashboard) atomically claims an idle worktree, creates a `task/<slug>` branch, and launches Claude in a tmux session with your task as the initial prompt.

3. **`lane attach`** (or `a` in the dashboard) drops you into the full Claude session. Approve tools, run skills, type follow-ups — everything works exactly as if you ran Claude directly. **Ctrl+B, D** to detach back to the dashboard without killing Claude.

4. When Claude finishes (or you `lane stop` it), the worktree auto-commits any changes, resets to the holding branch, and returns to the idle pool.

5. **`lane dashboard`** shows all worktrees, their status, and a live preview of the selected agent's output.

## Configuration

`lane init` accepts these options:

```
--count, -n     Number of worktrees (default: 4)
--base, -b      Base branch to fork tasks from (default: main)
--holding       Holding branch prefix (default: lane/idle)
--remote        Git remote (default: origin)
--agent-cmd     Agent command, space-separated (default: "claude")
--setup-script  Script to run in each worktree after creation
```

The agent command receives the task description as a final argument:
```bash
# default: claude "your task description here"
# custom:  aider --message "your task description here"
lane init --agent-cmd "aider --message"
```

## License

MIT
