# lane
<img width="1728" height="1004" alt="Screenshot 2026-05-21 at 15 21 26" src="https://github.com/user-attachments/assets/d638c63e-7f76-4b97-855e-839271b81124" />

A TUI dashboard for running parallel Claude Code sessions across isolated git worktrees.

Lane is a thin orchestration layer around Claude Code. It manages a fixed pool of pre-warmed git worktrees so you can run multiple Claude sessions in parallel on the same repo — each in an isolated checkout, all visible from one dashboard. You don't lose any Claude functionality: every session is a real interactive Claude session. The dashboard shows a live scrollable terminal view, detects when Claude needs input, and lets you interact directly — including typing, navigating menus, and approving prompts — without ever leaving the view.

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
2. The right pane shows a live scrollable view of Claude's terminal (500 lines of history)
3. When Claude needs input, the status flashes **INPUT** and you get a macOS notification
4. Press **`1`/`2`/`3`** to respond to numbered prompts directly from either mode
5. Press **`` ` ``** to switch to **Claude mode** — type directly into Claude, navigate menus with arrow keys, toggle checkboxes with Space, confirm with Enter
6. Press **`` ` ``** again to switch back to **Dashboard mode** and navigate between worktrees
7. Press **`a`** to attach for the full native Claude experience (Ctrl+D to return)
8. When Claude finishes, the worktree moves to **DONE** — press **`i`** to continue with a follow-up, or **`r`** to release

## CLI commands

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

The dashboard has two modes, toggled with **`` ` ``** (backtick). A badge in the header shows which mode you're in.

### Dashboard mode (default)

Navigate between worktrees. The right pane shows a live scrollable terminal snapshot of the selected session.

| Key | Action |
|---|---|
| `Up` / `Down` | Select worktree |
| `n` | Dispatch a new task |
| `a` | Attach to Claude session (Ctrl+D to return) |
| `i` | Focus reply input |
| `c` | Continue a done worktree with a follow-up prompt |
| `s` | Stop the selected agent |
| `r` | Release the selected worktree |
| `1` `2` `3` `y` | Send directly to Claude (works in both modes) |
| `q` | Quit |

### Claude mode

Full keyboard passthrough to the selected worktree's Claude session. Type directly, navigate menus, approve prompts — all without attaching.

| Key | Action |
|---|---|
| Any character | Types into Claude's session |
| `Up` / `Down` | Navigate Claude's menus |
| `Space` | Toggle checkboxes |
| `Enter` | Confirm selection |
| `Escape` | Cancel |
| `Tab` / `Shift+Tab` | Cycle Claude modes |
| `Left` / `Right` | Navigate tabs |
| `Backspace` / `Delete` | Edit text |

### Reply input

Press **`i`** to focus the reply input bar. Behaviour depends on the worktree status:

| Status | What happens on Enter |
|---|---|
| **BUSY** | Sends your message to the running Claude session |
| **DONE** | Starts a new Claude session with your message as a follow-up |
| **IDLE** | Dispatches as a new task |

### Notifications

When Claude needs input (permission prompts, option selects, etc.):
- The status column flashes red **INPUT**
- A macOS system notification is sent
- A terminal bell rings

### MCP servers

The bottom-left panel shows connected MCP servers with live status, refreshed every 10 seconds:
- **●** connected
- **△** needs authentication

Sources: project `.mcp.json`, per-project Claude config, claude.ai integrations, installed plugins.

## How it works

### Worktree lifecycle

```
IDLE ──[n/i dispatch]──> BUSY ──[Claude exits]──> DONE ──[i/c continue]──> BUSY
                                                       ──[r release]────> IDLE
```

1. **`lane init`** creates N worktrees pinned to holding branches. Once per project.
2. **`lane task`** (or `n` / `i` in dashboard) claims an idle worktree, creates a `task/<slug>` branch, launches interactive Claude in a tmux session.
3. The dashboard uses **`tmux capture-pane`** to show exactly what Claude's terminal looks like — proper formatting, scrollable history.
4. When Claude exits, the worktree moves to **DONE**. Branch and changes are preserved. Continue or release.
5. **`lane release`** auto-commits uncommitted work, resets to holding branch, returns to idle.

### Permissions

Lane automatically syncs your `.claude/settings.json`, `settings.local.json`, and `CLAUDE.md` into each worktree — on init, dispatch, and continue. Your permission allow-list carries over so Claude doesn't re-prompt for file reads, bash commands, etc.

### Terminal preview

The right pane uses `tmux capture-pane` to show exactly what Claude's terminal looks like, updated every 500ms. This is a read-only preview — press **`` ` ``** for Claude mode to interact, or **`a`** to attach for the full native experience.

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
