# lane

A TUI dashboard for running parallel Claude Code sessions across isolated git worktrees.

Lane is a thin orchestration layer around Claude Code. It manages a fixed pool of pre-warmed git worktrees so you can run multiple Claude sessions in parallel on the same repo — each in an isolated checkout, all visible from one dashboard. You don't lose any Claude functionality: every session is a real interactive Claude session. The dashboard shows a live terminal preview, detects when Claude needs input, and lets you respond without leaving the view.

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

To update:
```bash
uv tool install --force git+https://github.com/stewardwilf/lane.git
```

**Prerequisites:** git >= 2.20, python >= 3.11. tmux is installed automatically via Homebrew during `lane init` if missing.

## Quick start

```bash
cd ~/code/my-project

# 1. Initialise a pool of 4 worktrees
lane init --count 4 --base main

# 2. Open the dashboard
lane dashboard
```

From the dashboard:
1. Press **`n`** to dispatch a task — Claude starts in its own worktree
2. The right pane shows a live preview of Claude's terminal (updated every 500ms)
3. When Claude needs input (permission prompts, option selects), the status flashes **INPUT** and you get a system notification
4. Press **`1`/`2`/`3`** to respond to numbered prompts directly
5. Press **`` ` ``** to switch to Claude mode — arrow keys, Tab, Shift+Tab, Space, Enter, Escape all forward to Claude's session
6. Press **`` ` ``** again to switch back to Dashboard mode and navigate between worktrees
7. Press **`a`** to attach for the full Claude experience (Ctrl+D to return)
8. When Claude finishes, the worktree moves to **DONE** — press **`i`** to continue with a follow-up, or **`r`** to release

## Commands

| Command | Description |
|---|---|
| `lane init` | Create N worktrees in the current repo |
| `lane task <description>` | Dispatch a task to an idle worktree |
| `lane dashboard` | Open the TUI dashboard |
| `lane attach <wt-id>` | Attach to a Claude session (full interactive) |
| `lane continue <wt-id> [prompt]` | Start a new Claude session in a done worktree |
| `lane status [--watch]` | Show pool status |
| `lane stop <wt-id>` | Kill a running agent |
| `lane release <wt-id>` | Auto-commit changes, reset worktree to idle |
| `lane destroy` | Tear down the entire pool |

## Dashboard

The dashboard has two modes, toggled with **`` ` ``** (backtick):

### Dashboard mode (default)

Arrow keys navigate between worktrees. The right pane shows a live terminal snapshot of the selected worktree.

| Key | Action |
|---|---|
| `Up/Down` | Select worktree |
| `n` | Dispatch a new task |
| `a` | Attach to Claude session (Ctrl+D to return) |
| `i` | Focus reply input (send text to Claude / continue / dispatch) |
| `c` | Continue a done worktree with a follow-up prompt |
| `s` | Stop the selected agent |
| `r` | Release the selected worktree |
| `1` `2` `3` `y` | Send directly to Claude (works in both modes) |
| `q` | Quit |

### Claude mode

Arrow keys and other keys forward to the selected worktree's Claude session — navigate option menus, toggle checkboxes, confirm prompts.

| Key | Action |
|---|---|
| `Up/Down` | Navigate Claude's menus |
| `Space` | Toggle checkboxes |
| `Enter` | Confirm selection |
| `Escape` | Cancel |
| `Tab` / `Shift+Tab` | Cycle Claude modes |
| `Left/Right` | Navigate tabs |

### Reply input

Press **`i`** to focus the reply input bar. What happens when you hit Enter depends on the worktree status:

| Status | Behaviour |
|---|---|
| **BUSY** | Sends your message to the running Claude session via tmux |
| **DONE** | Starts a new Claude session with your message as a follow-up prompt |
| **IDLE** | Dispatches as a new task |

### Notifications

When Claude needs input (permission prompts, option selects, etc.):
- The status column flashes red **INPUT**
- A macOS system notification is sent
- A terminal bell rings

Connected MCP servers are shown in the bottom-left panel.

## How it works

1. **`lane init`** creates N sibling worktrees pinned to holding branches forked from your base. Once per project.

2. **`lane task`** (or `n` / `i` in dashboard) atomically claims an idle worktree, creates a `task/<slug>` branch, and launches interactive Claude in a tmux session.

3. The dashboard's right pane uses **`tmux capture-pane`** to show exactly what Claude's terminal looks like — proper formatting, no garbled output.

4. When Claude exits, the worktree moves to **DONE** (not released). Your branch and all changes are preserved. Press `i` or `c` to continue, or `r` to release.

5. **`lane release`** auto-commits any uncommitted work, resets the worktree to the holding branch, and returns it to the idle pool.

## Configuration

```
lane init [options]

--count, -n     Number of worktrees (default: 4)
--base, -b      Base branch to fork tasks from (default: main)
--holding       Holding branch prefix (default: lane/idle)
--remote        Git remote (default: origin)
--agent-cmd     Agent command (default: "claude")
--setup-script  Script to run in each worktree after creation
```

Bring your own agent:
```bash
lane init --agent-cmd "aider --message"
lane init --agent-cmd "claude -p"  # non-interactive print mode
```

## License

MIT
