"""Textual TUI dashboard for lane."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Footer, Static, DataTable, Input, Label

from lane.state import read_state, PoolState, Worktree
from lane.recovery import check_stale_workers
from lane.runner import kill_agent


LOGO = """\
[bold]┌─┬─┐[/bold]
[bold]│[blue]●[/blue]│ │[/bold]  [bold]lane[/bold] [dim]worktree pool[/dim]
[bold]├─┼─┤[/bold]
[bold]│ │ │[/bold]
[bold]└─┴─┘[/bold]\
"""


class WorktreeTable(DataTable):
    def on_mount(self) -> None:
        self.add_column("ID", key="id", width=8)
        self.add_column("Status", key="status", width=10)
        self.add_column("Task", key="task")
        self.add_column("Elapsed", key="elapsed", width=10)
        self.cursor_type = "row"
        self.zebra_stripes = True


class StatusBar(Static):
    def update_from_state(self, state: PoolState) -> None:
        busy = sum(1 for w in state.worktrees if w.status == "busy")
        idle = sum(1 for w in state.worktrees if w.status == "idle")
        done = sum(1 for w in state.worktrees if w.status == "done")
        total = len(state.worktrees)

        parts = [
            f" [bold]lane[/bold]  ·  pool={total}",
            f"[cyan]busy={busy}[/cyan]",
            f"[green]idle={idle}[/green]",
        ]
        if done:
            parts.append(f"[yellow]done={done}[/yellow]")
        parts.append(f"·  base={state.config.base_branch}")
        self.update("  ".join(parts))


class DetailPanel(Static):
    def update_from_worktree(self, wt: Worktree | None) -> None:
        if wt is None:
            self.update("No worktree selected")
            return

        elapsed = _elapsed(wt.started_at) if wt.started_at else "—"
        lines = [
            f"[dim]selected[/dim] · [bold]{wt.id}[/bold]",
            f"[dim]branch[/dim]   {wt.branch or '—'}",
            f"[dim]task[/dim]     {wt.task or '—'}",
            f"[dim]status[/dim]   {_status_styled(wt.status)}",
            f"[dim]elapsed[/dim]  {elapsed}",
        ]
        self.update("\n".join(lines))


class TerminalView(Static):
    """Shows a live snapshot of the tmux pane — exactly what Claude looks like."""

    DEFAULT_CSS = """
    TerminalView {
        height: 1fr;
        padding: 0 1;
        overflow: auto;
    }
    """


class ReplyInput(Input):
    DEFAULT_CSS = """
    ReplyInput { dock: bottom; margin: 0 0; }
    """


class TaskInputScreen(ModalScreen[str | None]):
    CSS = """
    TaskInputScreen { align: center middle; }
    #task-dialog {
        width: 70; height: auto; max-height: 12;
        padding: 1 2; background: $surface;
        border: thick $primary-background;
    }
    #task-label { margin-bottom: 1; }
    #task-input { width: 100%; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str = "New task", placeholder: str = "e.g. Fix the broken login redirect", **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="task-dialog"):
            yield Label(f"[bold]{self._title}[/bold]", id="task-label")
            yield Input(placeholder=self._placeholder, id="task-input")

    def on_mount(self) -> None:
        self.query_one("#task-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LaneDashboard(App):
    TITLE = "lane"

    CSS = """
    Screen { layout: vertical; }
    #header-bar {
        height: 5; padding: 0 2;
        background: $surface;
        border-bottom: solid $primary-background;
        layout: horizontal;
    }
    #logo { width: auto; padding: 0 1; }
    StatusBar { height: 5; padding: 1 2; content-align: left middle; }
    #main { layout: horizontal; height: 1fr; }
    #left {
        width: 1fr; max-width: 64;
        border-right: solid $primary-background;
        layout: vertical;
    }
    WorktreeTable { height: 1fr; }
    DetailPanel {
        height: auto; max-height: 8;
        padding: 1 2;
        border-top: solid $primary-background;
        background: $surface;
    }
    #right { width: 2fr; layout: vertical; }
    #pane-header {
        height: 3; padding: 1 2;
        background: $surface;
        border-bottom: solid $primary-background;
    }
    TerminalView { height: 1fr; padding: 0 1; }
    #reply-bar {
        height: auto;
        border-top: solid $primary-background;
        background: $surface;
        padding: 0 1;
    }
    #reply-hint { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("tab", "toggle_focus", "Tab: switch mode", show=True),
        Binding("a", "attach", "Attach", show=True),
        Binding("c", "continue_task", "Continue", show=True),
        Binding("s", "stop", "Stop", show=True),
        Binding("r", "release", "Release", show=True),
        Binding("n", "new_task", "New task", show=True),
        Binding("i", "focus_reply", "Reply", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    root: Path
    _poll_timer: Timer | None = None
    _bg_check_timer: Timer | None = None
    _selected_wt_id: str | None = None
    _state: PoolState | None = None
    _table_initialized: bool = False
    _waiting_input: set[str]
    _notified_input: set[str]
    _claude_focus: bool

    def __init__(self, root: Path, **kwargs):
        super().__init__(**kwargs)
        self.root = root
        self._table_initialized = False
        self._waiting_input = set()
        self._notified_input = set()
        self._claude_focus = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-bar"):
            yield Static(LOGO, id="logo")
            yield StatusBar(id="status-bar")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield WorktreeTable()
                yield DetailPanel(id="detail")
            with Vertical(id="right"):
                yield Static("", id="pane-header")
                yield TerminalView(id="term-view")
                with Vertical(id="reply-bar"):
                    yield Static("[dim]i to reply · sends to selected worktree[/dim]", id="reply-hint")
                    yield ReplyInput(placeholder="Type a message to Claude...", id="reply-input")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_state()
        self._poll_timer = self.set_interval(0.5, self._refresh_state)
        self._bg_check_timer = self.set_interval(3.0, self._check_background_worktrees)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "reply-input":
            return

        message = event.value.strip()
        event.input.value = ""

        if not message:
            self.query_one(WorktreeTable).focus()
            return

        if not self._selected_wt_id or not self._state:
            self.notify("No worktree selected", severity="warning")
            self.query_one(WorktreeTable).focus()
            return

        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt:
            self.query_one(WorktreeTable).focus()
            return

        if wt.status == "busy" and wt.tmux_session:
            _send_to_tmux(wt.tmux_session, message)
            self.notify(f"Sent to {wt.id}", timeout=2)
        elif wt.status == "done":
            self.notify(f"Continuing {wt.id}...", timeout=2)
            def _cont():
                try:
                    from lane.cli import _continue_worktree
                    _, err = _continue_worktree(self.root, wt.id, message)
                    if err:
                        self.call_from_thread(self.notify, f"Failed: {err}", severity="error")
                    else:
                        self.call_from_thread(self.notify, f"Resumed {wt.id}")
                except Exception as e:
                    self.call_from_thread(self.notify, f"Error: {e}", severity="error")
            Thread(target=_cont, daemon=True).start()
        elif wt.status == "idle":
            self.notify(f"Dispatching...", timeout=2)
            def _dispatch():
                try:
                    from lane.cli import dispatch_task_headless
                    wt_id, err = dispatch_task_headless(self.root, message)
                    if err:
                        self.call_from_thread(self.notify, f"Failed: {err}", severity="error")
                    else:
                        self.call_from_thread(self.notify, f"Dispatched to {wt_id}")
                        self.call_from_thread(self._select_worktree, wt_id)
                except Exception as e:
                    self.call_from_thread(self.notify, f"Error: {e}", severity="error")
            Thread(target=_dispatch, daemon=True).start()

        self.query_one(WorktreeTable).focus()

    def _refresh_state(self) -> None:
        try:
            state = read_state(self.root)
        except SystemExit:
            return

        check_stale_workers(state, self.root)
        self._state = state

        self.query_one(StatusBar).update_from_state(state)

        table = self.query_one(WorktreeTable)
        if not self._table_initialized:
            for wt in state.worktrees:
                table.add_row(
                    wt.id,
                    Text.from_markup(_status_styled(wt.status)),
                    _task_text(wt),
                    _elapsed(wt.started_at) if wt.started_at else "—",
                    key=wt.id,
                )
            self._table_initialized = True
        else:
            for wt in state.worktrees:
                try:
                    table.get_row(wt.id)
                    table.update_cell(wt.id, "status", Text.from_markup(_status_styled(wt.status)), update_width=False)
                    table.update_cell(wt.id, "task", _task_text(wt), update_width=False)
                    table.update_cell(wt.id, "elapsed", _elapsed(wt.started_at) if wt.started_at else "—", update_width=False)
                except Exception:
                    table.clear()
                    self._table_initialized = False
                    return

        if self._selected_wt_id:
            wt = next((w for w in state.worktrees if w.id == self._selected_wt_id), None)
            self.query_one(DetailPanel).update_from_worktree(wt)
            self._update_pane_header(wt)
            self._update_reply_hint(wt)
            self._refresh_terminal_view(wt)

    def _check_background_worktrees(self) -> None:
        """Slow poll: check non-selected busy worktrees for input prompts."""
        if not self._state:
            return
        for wt in self._state.worktrees:
            if wt.status == "busy" and wt.tmux_session and wt.id != self._selected_wt_id:
                content = _capture_tmux_pane(wt.tmux_session)
                if content:
                    self._update_input_alert(wt.id, _needs_user_input(content))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            new_id = str(event.row_key.value)
            if new_id == self._selected_wt_id:
                return
            self._selected_wt_id = new_id
            if self._state:
                wt = next((w for w in self._state.worktrees if w.id == new_id), None)
                self.query_one(DetailPanel).update_from_worktree(wt)
                self._update_pane_header(wt)
                self._update_reply_hint(wt)
                self._refresh_terminal_view(wt)

    def _update_pane_header(self, wt: Worktree | None) -> None:
        header = self.query_one("#pane-header", Static)
        if not wt:
            header.update(" [dim]select a worktree[/dim]")
            return

        mode_badge = "[bold white on blue] CLAUDE [/bold white on blue]" if self._claude_focus else "[bold white on #444444] DASHBOARD [/bold white on #444444]"

        if wt.status == "busy":
            header.update(f" {mode_badge} [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· Tab switch · a attach · i reply[/dim]")
        elif wt.status == "done":
            header.update(f" {mode_badge} [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· i continue · r release[/dim]")
        elif wt.status == "idle":
            header.update(f" {mode_badge} [bold]{wt.id}[/bold] [dim]· idle · i or n to dispatch[/dim]")
        else:
            header.update(f" {mode_badge} [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· {wt.status}[/dim]")

    def _update_reply_hint(self, wt: Worktree | None) -> None:
        hint = self.query_one("#reply-hint", Static)
        if not wt or wt.status == "idle":
            hint.update("[dim]i to type · dispatches a new task[/dim]")
        elif wt.status == "busy":
            hint.update(f"[dim]i to reply · sends to [bold]{wt.id}[/bold] Claude session[/dim]")
        elif wt.status == "done":
            hint.update(f"[dim]i to continue · starts new Claude session in [bold]{wt.id}[/bold][/dim]")

    def _refresh_terminal_view(self, wt: Worktree | None) -> None:
        """Show live tmux pane snapshot for busy worktrees."""
        view = self.query_one(TerminalView)
        if not wt:
            view.update("[dim]Select a worktree[/dim]")
            return

        if wt.status == "busy" and wt.tmux_session:
            content = _capture_tmux_pane(wt.tmux_session)
            if content is not None:
                view.update(Text.from_ansi(content))
                # Check if Claude is waiting for input
                needs_input = _needs_user_input(content)
                self._update_input_alert(wt.id, needs_input)
            return

        if wt.status == "done":
            view.update(f"[dim]Claude finished. Press [bold]i[/bold] to continue or [bold]r[/bold] to release.[/dim]")
            return

        if wt.status == "idle":
            view.update(f"[dim]Idle. Press [bold]i[/bold] or [bold]n[/bold] to dispatch a task.[/dim]")
            return

        view.update(f"[dim]{wt.status}[/dim]")

    # ── Input detection ────────────────────────────────────────

    def _update_input_alert(self, wt_id: str, needs_input: bool) -> None:
        """Track and notify when a worktree needs user input."""
        if needs_input:
            self._waiting_input.add(wt_id)
            # Update the status cell to show waiting indicator
            if self._table_initialized:
                table = self.query_one(WorktreeTable)
                try:
                    table.update_cell(wt_id, "status", Text.from_markup("[bold red on dark_red] INPUT [/bold red on dark_red]"), update_width=False)
                except Exception:
                    pass
            # Auto-switch to Claude mode if this is the selected worktree
            if wt_id == self._selected_wt_id and not self._claude_focus:
                self._claude_focus = True
                self.query_one(TerminalView).can_focus = True
                self.query_one(TerminalView).focus()
                self._update_mode_indicator()
            # Send system notification (once per prompt)
            if wt_id not in self._notified_input:
                self._notified_input.add(wt_id)
                wt = next((w for w in self._state.worktrees if w.id == wt_id), None) if self._state else None
                task_name = wt.task if wt else wt_id
                _system_notify(f"lane · {wt_id}", f"Needs input: {task_name}")
                self.bell()
        else:
            self._waiting_input.discard(wt_id)
            self._notified_input.discard(wt_id)

    # ── Focus mode + key passthrough ────────────────────────────

    def action_toggle_focus(self) -> None:
        self._claude_focus = not self._claude_focus
        if self._claude_focus:
            # Focus the terminal view so DataTable doesn't eat arrow keys
            self.query_one(TerminalView).can_focus = True
            self.query_one(TerminalView).focus()
        else:
            self.query_one(TerminalView).can_focus = False
            self.query_one(WorktreeTable).focus()
        mode = "Claude" if self._claude_focus else "Dashboard"
        self.notify(f"Mode: {mode} — Tab to switch", timeout=2)
        self._update_mode_indicator()

    def _update_mode_indicator(self) -> None:
        header = self.query_one("#pane-header", Static)
        if not self._selected_wt_id or not self._state:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt:
            return

        mode_badge = "[bold white on blue] CLAUDE [/bold white on blue]" if self._claude_focus else "[bold white on #444444] DASHBOARD [/bold white on #444444]"

        if wt.status == "busy":
            header.update(f" {mode_badge} [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· Tab to switch[/dim]")
        elif wt.status == "done":
            header.update(f" {mode_badge} [bold]{wt.id}[/bold] · {wt.task or ''}")
        else:
            header.update(f" {mode_badge} [bold]{wt.id}[/bold]")

    def on_key(self, event) -> None:
        """In Claude mode, forward keys to the tmux session. In Dashboard mode, normal navigation."""
        if isinstance(self.focused, Input):
            return

        # Keys that always pass through to Claude when it's busy (regardless of mode)
        ALWAYS_PASSTHROUGH = {"1": "1", "2": "2", "3": "3", "y": "y"}

        # Keys that only pass through in Claude focus mode
        FOCUS_PASSTHROUGH = {
            "up": "Up", "down": "Down", "left": "Left", "right": "Right",
            "enter": "Enter", "escape": "Escape", "space": "Space",
        }

        if not self._selected_wt_id or not self._state:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt or wt.status != "busy" or not wt.tmux_session:
            return

        tmux_key = ALWAYS_PASSTHROUGH.get(event.key)
        if not tmux_key and self._claude_focus:
            tmux_key = FOCUS_PASSTHROUGH.get(event.key)

        if tmux_key:
            subprocess.run(
                ["tmux", "send-keys", "-t", wt.tmux_session, tmux_key],
                capture_output=True, check=False,
            )
            event.prevent_default()

    # ── Actions ─────────────────────────────────────────────────

    def action_focus_reply(self) -> None:
        self.query_one("#reply-input", ReplyInput).focus()

    def action_new_task(self) -> None:
        self.push_screen(
            TaskInputScreen("New task", "Describe the work for Claude"),
            self._on_task_submitted,
        )

    def _on_task_submitted(self, description: str | None) -> None:
        if not description:
            return
        self.notify(f"Dispatching: {description}...", timeout=3)
        def _dispatch():
            try:
                from lane.cli import dispatch_task_headless
                wt_id, err = dispatch_task_headless(self.root, description)
                if err:
                    self.call_from_thread(self.notify, f"Failed: {err}", severity="error", timeout=5)
                else:
                    self.call_from_thread(self.notify, f"Dispatched to {wt_id}", timeout=3)
                    self.call_from_thread(self._select_worktree, wt_id)
            except Exception as e:
                self.call_from_thread(self.notify, f"Error: {e}", severity="error", timeout=8)
        Thread(target=_dispatch, daemon=True).start()

    def action_continue_task(self) -> None:
        if not self._selected_wt_id or not self._state:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt:
            return
        if wt.status == "busy":
            self.notify("Already running — press a to attach, or i to reply", severity="warning")
            return
        if wt.status == "idle":
            self.notify("Idle — press n or i to dispatch", severity="warning")
            return
        self.push_screen(
            TaskInputScreen("Continue task", "Follow-up prompt (or leave empty to resume)"),
            self._on_continue_submitted,
        )

    def _on_continue_submitted(self, prompt: str | None) -> None:
        wt_id = self._selected_wt_id
        if not wt_id:
            return
        self.notify(f"Continuing {wt_id}...", timeout=3)
        def _cont():
            try:
                from lane.cli import _continue_worktree
                _, err = _continue_worktree(self.root, wt_id, prompt)
                if err:
                    self.call_from_thread(self.notify, f"Failed: {err}", severity="error", timeout=5)
                else:
                    self.call_from_thread(self.notify, f"Resumed {wt_id}", timeout=3)
            except Exception as e:
                self.call_from_thread(self.notify, f"Error: {e}", severity="error", timeout=8)
        Thread(target=_cont, daemon=True).start()

    def _select_worktree(self, wt_id: str) -> None:
        self._selected_wt_id = wt_id
        if self._state:
            table = self.query_one(WorktreeTable)
            try:
                idx = next(i for i, w in enumerate(self._state.worktrees) if w.id == wt_id)
                table.move_cursor(row=idx)
            except (StopIteration, Exception):
                pass

    def action_attach(self) -> None:
        if not self._selected_wt_id or not self._state:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt or not wt.tmux_session:
            self.notify("No running session — press c to continue or i to reply", severity="warning")
            return
        if wt.status != "busy":
            self.notify(f"{wt.id} is not running", severity="warning")
            return
        with self.suspend():
            # Bind Ctrl+D to detach for easy return to dashboard
            os.system(f"tmux bind-key -T root C-d detach 2>/dev/null; tmux attach-session -t {wt.tmux_session}")
            # Unbind after detach so it doesn't linger
            os.system(f"tmux unbind-key -T root C-d 2>/dev/null")

    def action_stop(self) -> None:
        if not self._selected_wt_id or not self._state:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt or wt.status != "busy":
            self.notify(f"{self._selected_wt_id} is not running", severity="warning")
            return
        kill_agent(wt.pid, wt.tmux_session)
        self.notify(f"Stopped {self._selected_wt_id}")

    def action_release(self) -> None:
        if not self._selected_wt_id:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if wt and wt.status == "idle":
            self.notify(f"{self._selected_wt_id} is already idle", severity="warning")
            return
        if wt and wt.status == "busy":
            self.notify("Stop the agent first (s)", severity="warning")
            return
        def _release():
            from lane.cli import _do_release
            try:
                _do_release(self.root, self._selected_wt_id, quiet=True)
                self.call_from_thread(self.notify, f"Released {self._selected_wt_id}")
            except Exception as e:
                self.call_from_thread(self.notify, f"Release failed: {e}", severity="error")
        Thread(target=_release, daemon=True).start()


def _capture_tmux_pane(session_name: str) -> str | None:
    """Capture the current visible content of a tmux pane — exactly what a human would see."""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-e"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.rstrip('\n')
    except Exception:
        pass
    return None


def _send_to_tmux(session_name: str, text: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, text, "Enter"],
        capture_output=True,
        check=False,
    )


def _task_text(wt: Worktree) -> str:
    t = wt.task or "—"
    return t[:37] + "..." if len(t) > 40 else t


def _elapsed(started_at: str | None) -> str:
    if not started_at:
        return "—"
    try:
        start = datetime.fromisoformat(started_at)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    except Exception:
        return "—"


def _needs_user_input(content: str) -> bool:
    """Check if the tmux pane content shows Claude waiting for user input."""
    # Strip ANSI for pattern matching
    plain = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', content)
    plain = re.sub(r'\x1b\][^\x07]*\x07', '', plain)

    indicators = [
        r'Do you want to proceed\?',
        r'Select .+:',
        r'›\s*\d+\.',                    # Selected option indicator
        r'❯\s*\d+\.',
        r'\)\s*\d+\.',                    # ) 1. Yes
        r'Esc to cancel',
        r'Tab to amend',
        r'Enter to continue',
        r'\[Y/n\]',
        r'\[y/N\]',
    ]
    return any(re.search(p, plain) for p in indicators)


def _system_notify(title: str, message: str) -> None:
    """Send a macOS system notification."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            capture_output=True, check=False, timeout=3,
        )
    except Exception:
        pass


def _status_styled(status: str) -> str:
    return {
        "idle": "[green]IDLE[/green]",
        "busy": "[cyan]BUSY[/cyan]",
        "done": "[yellow]DONE[/yellow]",
        "claiming": "[dim]CLAIM[/dim]",
        "error": "[red]ERROR[/red]",
    }.get(status, status)
