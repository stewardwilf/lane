# lane
<img width="1728" height="1004" alt="Screenshot 2026-05-21 at 15 21 26" src="https://github.com/user-attachments/assets/d638c63e-7f76-4b97-855e-839271b81124" />

A TUI dashboard for running parallel Claude Code sessions across isolated git worktrees.

Lane is a thin orchestration layer around Claude Code. It manages a fixed pool of pre-warmed git worktrees so you can run multiple Claude sessions in parallel on the same repo — each in an isolated checkout, all visible from one dashboard. You don't lose any Claude functionality: every session is a real interactive Claude session. The dashboard shows a live scrollable terminal view, detects when Claude needs input, presents options as an interactive selector, and lets you respond — all without leaving the view.

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
1. Press **`n`** to dispatch a task (multi-line, Ctrl+Enter to submit)
2. The right pane shows a live scrollable view of Claude's terminal
3. When Claude needs input, the status flashes **INPUT** and an **interactive option selector** appears — arrow through and Enter to pick
4. Press **`1`**-**`9`** to quick-respond to numbered prompts
5. Press **`i`** to open the reply box — multi-line, Ctrl+Enter to send
6. Press **`a`** to attach for the full native Claude experience (Ctrl+D to return)
7. When Claude finishes, the worktree moves to **DONE** — press **`i`** to continue with a follow-up, or **`r`** to release

## CLI commands

| Command | Description |
|---|---|
| `lane init` | Create N worktrees in the current repo |
| `lane task <description>` | Dispatch a task to an idle worktree |
| `lane dashboard` | Open the TUI dashboard |
| `lane attach <wt-id>` | Attach to a Claude session (full interactive) |
| `lane continue <wt-id> [prompt]` | Continue a done worktree with a new prompt |
| `lane status [--watch]` | Show pool status |
| `lane stop <wt-id>` | Kill a running agent |
| `lane release <wt-id>` | Auto-commit changes, reset worktree to idle |
| `lane destroy` | Tear down the entire pool |

## Dashboard

### Keybinds

| Key | Action |
|---|---|
| `Up` / `Down` | Select worktree |
| `1`-`9` / `y` | Quick-respond to Claude's numbered prompts |
| `n` | Dispatch a new task (multi-line input) |
| `i` | Open reply box (multi-line, Ctrl+Enter to send) |
| `a` | Attach to Claude session (Ctrl+D to return) |
| `c` | Continue a done worktree with a follow-up prompt |
| `s` | Stop the selected agent |
| `r` | Release the selected worktree |
| `q` | Quit |

### Interactive prompt selector

When Claude presents numbered options (permission prompts, menu choices, etc.), an **interactive option list** appears above the reply box. Arrow up/down to navigate, Enter to select. The last option is always "Type a response..." which opens the reply box.

### Reply box

Press **`i`** to focus the multi-line reply box. Type your message, then **Ctrl+Enter** to send. Escape to cancel. What happens depends on the worktree status:

| Status | What happens on Ctrl+Enter |
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

The bottom-left panel shows connected MCP servers with live status (refreshed every 10s):
- **●** connected
- **△** needs authentication

Sources: project `.mcp.json`, per-project Claude config, claude.ai integrations, installed plugins.

## How it works

### Worktree lifecycle

```
IDLE ──[n/i dispatch]──> BUSY ──[Claude exits]──> DONE ──[i/c continue]──> BUSY
                                                       ──[r release]────> IDLE
```

1. **`lane init`** creates N worktrees pinned to holding branches forked from your base. Once per project.
2. **`lane task`** (or `n` / `i` in dashboard) atomically claims an idle worktree, creates a `task/<slug>` branch, launches interactive Claude in a tmux session.
3. The dashboard uses **`tmux capture-pane`** to show exactly what Claude's terminal looks like — proper formatting, scrollable history. Claude's prompts are parsed and recreated as interactive selectors in lane's own UI.
4. When Claude exits, the worktree moves to **DONE**. Branch and changes are preserved. Continue or release.
5. **`lane release`** auto-commits uncommitted work, resets to holding branch, returns to idle.

### Permissions

Lane automatically syncs your `.claude/settings.json`, `settings.local.json`, and `CLAUDE.md` into each worktree — on init, dispatch, and continue. Your permission allow-list carries over so Claude doesn't re-prompt for file reads, bash commands, etc.

### Terminal preview

The right pane uses `tmux capture-pane` to show exactly what Claude's terminal looks like, updated every 500ms with 500 lines of scrollback. All interaction goes through lane's UI (option selector, reply box, quick keys) — never directly into the terminal.

For full native access, press **`a`** to attach to the tmux session. **Ctrl+D** to return.

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
