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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Footer, Static, DataTable, Input, Label, OptionList, TextArea
from textual.widgets.option_list import Option

from lane.state import read_state, PoolState, Worktree
from lane.recovery import check_stale_workers
from lane.runner import kill_agent


ACCENT = "#7C6FF7"
ACCENT_MID = "#9B8FFF"
ACCENT_LIGHT = "#BDB2FF"

LOGO = f"[{ACCENT}]██[/{ACCENT}][{ACCENT_MID}]██[/{ACCENT_MID}][{ACCENT_LIGHT}]██[/{ACCENT_LIGHT}]  [bold {ACCENT}]lane[/bold {ACCENT}]"


class WorktreeTable(DataTable):
    def on_mount(self) -> None:
        self.add_column("ID", key="id", width=8)
        self.add_column("Status", key="status", width=10)
        self.add_column("Elapsed", key="elapsed", width=10)
        self.add_column("Task", key="task")
        self.cursor_type = "row"
        self.zebra_stripes = True


class StatusBar(Static):
    def update_from_state(self, state: PoolState) -> None:
        busy = sum(1 for w in state.worktrees if w.status == "busy")
        idle = sum(1 for w in state.worktrees if w.status == "idle")
        done = sum(1 for w in state.worktrees if w.status == "done")
        total = len(state.worktrees)

        parts = [
            f"[bold #7C6FF7]●[/bold #7C6FF7] busy {busy}",
            f"[bold #00B894]●[/bold #00B894] idle {idle}",
        ]
        if done:
            parts.append(f"[bold #FDCB6E]●[/bold #FDCB6E] done {done}")
        parts.append(f"[dim]·  {state.config.base_branch}[/dim]")
        self.update("   ".join(parts))


class DetailPanel(Static):
    def update_from_worktree(self, wt: Worktree | None) -> None:
        if wt is None:
            self.update("No worktree selected")
            return
        elapsed = _elapsed(wt.started_at) if wt.started_at else "—"
        lines = [
            f"[bold #9B8FFF]{wt.id}[/bold #9B8FFF]  {_status_styled(wt.status)}  [dim]{elapsed}[/dim]",
            f"[dim]branch[/dim]  {wt.branch or '—'}",
            f"[dim]task[/dim]    {wt.task or '—'}",
        ]
        self.update("\n".join(lines))


class TerminalView(VerticalScroll):
    """Scrollable view of the tmux pane with history."""

    DEFAULT_CSS = """
    TerminalView {
        height: 1fr;
        padding: 0 1;
    }
    #term-content { width: 1fr; }
    """

    _was_at_bottom: bool = True

    def compose(self) -> ComposeResult:
        yield Static("", id="term-content")

    def update(self, content) -> None:
        self._was_at_bottom = self.scroll_offset.y >= (self.virtual_size.height - self.size.height - 2)
        self.query_one("#term-content", Static).update(content)
        if self._was_at_bottom:
            self.call_later(self.scroll_end, animate=False)


class PromptOptions(OptionList):
    """Interactive option selector for Claude's prompts."""

    BINDINGS = [Binding("escape", "dismiss", show=False)]

    DEFAULT_CSS = """
    PromptOptions {
        height: auto;
        max-height: 8;
        background: $surface;
        border-top: solid $primary-background;
        padding: 0 1;
    }
    PromptOptions:focus {
        border-top: solid #7C6FF7;
    }
    PromptOptions > .option-list--option-highlighted {
        background: #7C6FF7 30%;
    }
    """

    def action_dismiss(self) -> None:
        self.app.query_one(WorktreeTable).focus()


class ReplyInput(TextArea):
    """Multi-line reply input. Enter to send, Shift+Enter for new line."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    ReplyInput {
        height: 4;
        min-height: 3;
        max-height: 8;
    }
    """

    def _on_key(self, event) -> None:
        if event.key == "enter":
            # Plain Enter sends
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self.app._handle_reply(text)
            self.clear()
            self.app.query_one(WorktreeTable).focus()

    def action_dismiss(self) -> None:
        self.clear()
        self.app.query_one(WorktreeTable).focus()


class TaskInputScreen(ModalScreen[str | None]):
    CSS = """
    TaskInputScreen { align: center middle; }
    #task-dialog {
        width: 80; height: auto; max-height: 20;
        padding: 1 2; background: $surface;
        border: thick $primary-background;
    }
    #task-label { margin-bottom: 1; }
    #task-input { width: 100%; height: 8; min-height: 4; }
    #task-hint { height: 1; color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, title: str = "New task", placeholder: str = "e.g. Fix the broken login redirect", **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="task-dialog"):
            yield Label(f"[bold]{self._title}[/bold]", id="task-label")
            yield TextArea(id="task-input")
            yield Static("[dim]Enter to submit · Escape to cancel[/dim]", id="task-hint")

    def on_mount(self) -> None:
        self.query_one("#task-input", TextArea).focus()

    def action_submit(self) -> None:
        value = self.query_one("#task-input", TextArea).text.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LaneDashboard(App):
    TITLE = "lane"

    CSS = """
    Screen { layout: vertical; }
    #header-bar {
        height: 3; padding: 0 2;
        background: $surface;
        border-bottom: solid #7C6FF7;
        layout: horizontal;
        align: left middle;
    }
    #logo { width: auto; padding: 0 2 0 0; }
    StatusBar { padding: 0 2; content-align: left middle; width: 1fr; }
    #version-badge { width: auto; padding: 0 1; color: $text-muted; }
    #main { layout: horizontal; height: 1fr; }
    #left {
        width: 1fr; max-width: 64;
        border-right: solid $primary-background;
        layout: vertical;
    }
    WorktreeTable { height: 1fr; }
    #mcp-panel {
        height: auto; max-height: 10;
        padding: 1 2;
        border-top: solid $primary-background;
        background: $surface;
    }
    DetailPanel {
        height: auto; max-height: 8;
        padding: 1 2;
        border-top: solid $primary-background;
        background: $surface;
    }
    #right { width: 2fr; layout: vertical; }
    #pane-header {
        height: 1; padding: 0 2;
        background: $surface;
        border-bottom: solid $primary-background;
    }
    TerminalView { height: 1fr; }
    #interaction-bar {
        height: auto;
        border-top: solid $primary-background;
        background: $surface;
        padding: 0 1;
    }
    #reply-hint { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("a", "attach", "Attach", show=True),
        Binding("c", "continue_task", "Continue", show=True),
        Binding("s", "stop", "Stop", show=True),
        Binding("r", "release", "Release", show=True),
        Binding("n", "new_task", "New task", show=True),
        Binding("i", "focus_reply", "Reply", show=True),
        Binding("w", "add_worktree", "Add wt", show=True),
        Binding("d", "remove_worktree", "Del", show=True),
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
    _current_options: list[tuple[str, str]]  # (display_text, key_to_send)

    def __init__(self, root: Path, **kwargs):
        super().__init__(**kwargs)
        self.root = root
        self._table_initialized = False
        self._waiting_input = set()
        self._notified_input = set()
        self._current_options = []

    def compose(self) -> ComposeResult:
        from lane import __version__
        with Horizontal(id="header-bar"):
            yield Static(LOGO, id="logo")
            yield StatusBar(id="status-bar")
            yield Static(f"[dim]v{__version__}[/dim]", id="version-badge")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield WorktreeTable()
                yield Static("", id="mcp-panel")
                yield DetailPanel(id="detail")
            with Vertical(id="right"):
                yield Static("", id="pane-header")
                yield TerminalView(id="term-view")
                with Vertical(id="interaction-bar"):
                    yield PromptOptions(id="prompt-options")
                    yield Static("[dim]i to type · Enter to send · a to attach[/dim]", id="reply-hint")
                    yield ReplyInput(id="reply-input")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_state()
        self._poll_timer = self.set_interval(0.5, self._refresh_state)
        self._bg_check_timer = self.set_interval(3.0, self._check_background_worktrees)
        self._mcp_timer = self.set_interval(10.0, self._refresh_mcp)
        self._refresh_mcp()

    def _refresh_mcp(self) -> None:
        mcp = _get_mcp_servers(self.root)
        panel = self.query_one("#mcp-panel", Static)
        if mcp:
            panel.update(f"[dim]mcp[/dim] {mcp}")
        else:
            panel.update("[dim]mcp[/dim] [dim]none[/dim]")

    # ── Input handling ──────────────────────────────────────────

    def _handle_reply(self, message: str) -> None:
        """Handle submitted text from the reply area."""
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
            self._do_continue(wt.id, message)
        elif wt.status == "idle":
            self._do_dispatch(message)

        self.query_one(WorktreeTable).focus()

    def on_key(self, event) -> None:
        """Handle number keys for quick prompt responses."""
        if isinstance(self.focused, (Input, TextArea, PromptOptions)):
            return  # Let the widget handle its own keys

        # Number keys send the corresponding option + Enter to Claude
        if event.key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            if self._selected_wt_id and self._state:
                wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
                if wt and wt.status == "busy" and wt.tmux_session:
                    _send_key_async(wt.tmux_session, event.key)
                    _send_key_async(wt.tmux_session, "Enter")
                    event.prevent_default()
            return

        # y for yes + Enter
        if event.key == "y":
            if self._selected_wt_id and self._state:
                wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
                if wt and wt.status == "busy" and wt.tmux_session:
                    _send_key_async(wt.tmux_session, "y")
                    _send_key_async(wt.tmux_session, "Enter")
                    event.prevent_default()

    # ── State refresh ───────────────────────────────────────────

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
                    _elapsed(wt.started_at) if wt.started_at else "—",
                    _task_text(wt),
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
            self._refresh_terminal_view(wt)

    def _check_background_worktrees(self) -> None:
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
                self._refresh_terminal_view(wt)

    # ── UI updates ──────────────────────────────────────────────

    def _update_pane_header(self, wt: Worktree | None) -> None:
        header = self.query_one("#pane-header", Static)
        if not wt:
            header.update(" [dim]select a worktree[/dim]")
        elif wt.status == "busy":
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· a attach · i reply · 1-9 respond[/dim]")
        elif wt.status == "done":
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· i continue · r release[/dim]")
        elif wt.status == "idle":
            header.update(f" [bold]{wt.id}[/bold] [dim]· idle · i or n to dispatch[/dim]")
        else:
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· {wt.status}[/dim]")

    def _refresh_terminal_view(self, wt: Worktree | None) -> None:
        view = self.query_one(TerminalView)
        opts = self.query_one(PromptOptions)

        if not wt:
            view.update("[dim]Select a worktree[/dim]")
            self._set_prompt_options([])
            return

        if wt.status == "busy" and wt.tmux_session:
            content = _capture_tmux_pane(wt.tmux_session)
            if content is not None:
                needs_input = _needs_user_input(content)
                self._update_input_alert(wt.id, needs_input)
                options = _parse_options(content)
                self._set_prompt_options(options)
                # Trim Claude's input chrome — we handle input via our own UI
                view.update(Text.from_ansi(_trim_chrome(content)))
            return

        if wt.status == "done":
            view.update(f"[dim]Claude finished. Press [bold]i[/bold] to continue or [bold]r[/bold] to release.[/dim]")
            self._set_prompt_options([])
            return

        if wt.status == "idle":
            view.update(f"[dim]Idle. Press [bold]i[/bold] or [bold]n[/bold] to dispatch a task.[/dim]")
            self._set_prompt_options([])
            return

        view.update(f"[dim]{wt.status}[/dim]")
        self._set_prompt_options([])

    def _set_prompt_options(self, options: list[tuple[str, str]]) -> None:
        """Update the interactive option list. Only rebuilds if options changed."""
        opts_widget = self.query_one(PromptOptions)

        # Compare with current to avoid flicker
        new_keys = [(k, l) for k, l in options]
        if new_keys == self._current_options:
            return
        self._current_options = new_keys

        opts_widget.clear_options()
        if not options:
            opts_widget.display = False
            return

        opts_widget.display = True
        seen_ids = set()
        for key, label in options:
            opt_id = f"opt-{key}"
            if opt_id not in seen_ids:
                seen_ids.add(opt_id)
                opts_widget.add_option(Option(f"  {key}.  {label}", id=opt_id))
        opts_widget.add_option(Option("  ✎  Type a response...", id="opt-type"))

    def _update_reply_hint(self, wt: Worktree | None) -> None:
        hint = self.query_one("#reply-hint", Static)
        if not wt or wt.status == "idle":
            hint.update("[dim]i to type · dispatches a new task[/dim]")
        elif wt.status == "busy":
            hint.update(f"[dim]i to reply · sends to [bold]{wt.id}[/bold][/dim]")
        elif wt.status == "done":
            hint.update(f"[dim]i to continue · new Claude session in [bold]{wt.id}[/bold][/dim]")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle selection from the prompt options list."""
        option_id = event.option.id
        if not option_id:
            return

        if option_id == "opt-type":
            self.query_one("#reply-input", ReplyInput).focus()
            return

        # Extract the number from "opt-1", "opt-2", etc.
        key = option_id.replace("opt-", "")
        if self._selected_wt_id and self._state:
            wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
            if wt and wt.status == "busy" and wt.tmux_session:
                _send_key_async(wt.tmux_session, key)
                _send_key_async(wt.tmux_session, "Enter")
                self.notify(f"Sent {key} to {wt.id}", timeout=2)

    # ── Input detection ─────────────────────────────────────────

    def _update_input_alert(self, wt_id: str, needs_input: bool) -> None:
        if needs_input:
            self._waiting_input.add(wt_id)
            if self._table_initialized:
                table = self.query_one(WorktreeTable)
                try:
                    table.update_cell(wt_id, "status", Text.from_markup("[bold white on #FF6B6B] INPUT [/bold white on #FF6B6B]"), update_width=False)
                except Exception:
                    pass
            if wt_id not in self._notified_input:
                self._notified_input.add(wt_id)
                wt = next((w for w in self._state.worktrees if w.id == wt_id), None) if self._state else None
                task_name = wt.task if wt else wt_id
                _system_notify(f"lane · {wt_id}", f"Needs input: {task_name}")
                self.bell()
        else:
            self._waiting_input.discard(wt_id)
            self._notified_input.discard(wt_id)

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
        self._do_dispatch(description)

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
            lambda prompt: self._do_continue(self._selected_wt_id, prompt),
        )

    def _do_dispatch(self, description: str) -> None:
        self.notify(f"Dispatching: {description}...", timeout=3)
        def _run():
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
        Thread(target=_run, daemon=True).start()

    def _do_continue(self, wt_id: str, prompt: str | None) -> None:
        self.notify(f"Continuing {wt_id}...", timeout=3)
        def _run():
            try:
                from lane.cli import _continue_worktree
                _, err = _continue_worktree(self.root, wt_id, prompt)
                if err:
                    self.call_from_thread(self.notify, f"Failed: {err}", severity="error", timeout=5)
                else:
                    self.call_from_thread(self.notify, f"Resumed {wt_id}", timeout=3)
            except Exception as e:
                self.call_from_thread(self.notify, f"Error: {e}", severity="error", timeout=8)
        Thread(target=_run, daemon=True).start()

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
            os.system(f"tmux bind-key -T root C-d detach 2>/dev/null; tmux attach-session -t {wt.tmux_session}")
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

    def action_add_worktree(self) -> None:
        self.notify("Adding worktree...", timeout=2)
        def _add():
            try:
                from lane.cli import add_worktree_headless
                wt_id, err = add_worktree_headless(self.root)
                if err:
                    self.call_from_thread(self.notify, f"Failed: {err}", severity="error", timeout=5)
                else:
                    self.call_from_thread(self.notify, f"Added {wt_id}")
                    self.call_from_thread(self._rebuild_table)
            except Exception as e:
                self.call_from_thread(self.notify, f"Error: {e}", severity="error")
        Thread(target=_add, daemon=True).start()

    def action_remove_worktree(self) -> None:
        if not self._selected_wt_id or not self._state:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt:
            return
        if wt.status == "busy":
            self.notify("Stop the agent first (s)", severity="warning")
            return
        wt_id = self._selected_wt_id
        self.notify(f"Removing {wt_id}...", timeout=2)
        def _remove():
            try:
                from lane.cli import remove_worktree_headless
                err = remove_worktree_headless(self.root, wt_id)
                if err:
                    self.call_from_thread(self.notify, f"Failed: {err}", severity="error", timeout=5)
                else:
                    self.call_from_thread(self.notify, f"Removed {wt_id}")
                    self.call_from_thread(self._rebuild_table)
            except Exception as e:
                self.call_from_thread(self.notify, f"Error: {e}", severity="error")
        Thread(target=_remove, daemon=True).start()

    def _rebuild_table(self) -> None:
        """Force a full table rebuild on next refresh."""
        self.query_one(WorktreeTable).clear()
        self._table_initialized = False


# ── Helpers ─────────────────────────────────────────────────────

def _capture_tmux_pane(session_name: str) -> str | None:
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-e", "-S", "-500"],
            capture_output=True, text=True, check=False, timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.rstrip('\n')
    except Exception:
        pass
    return None


def _send_key_async(session_name: str, *args: str) -> None:
    subprocess.Popen(
        ["tmux", "send-keys", "-t", session_name, *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _send_to_tmux(session_name: str, text: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, text, "Enter"],
        capture_output=True, check=False,
    )


def _trim_chrome(content: str) -> str:
    """Remove Claude's input chrome from the bottom of the terminal view."""
    # Strip ANSI for analysis
    plain = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', content)
    plain = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', plain)

    lines = content.split('\n')
    plain_lines = plain.split('\n')

    cut_at = len(lines)

    def is_chrome(s: str) -> bool:
        s = s.strip()
        if not s:
            return True
        # Prompt cursor (› or ❯ followed by space/cursor)
        if re.match(r'^[›❯]\s*', s) and len(s) < 10:
            return True
        # Status bar keywords
        if re.search(r'accept edits|shift.tab|esc to interrupt|/effort|esc to cancel|tab to amend|enter to select|to navigate', s, re.IGNORECASE):
            return True
        # Permission prompt
        if 'Do you want to' in s:
            return True
        # Numbered options at prompt (short lines)
        if re.match(r'^[›❯\)\s]*\d+\.\s+\S', s) and len(s) < 60:
            return True
        # Separator lines (any combo of ─ _ - = ~ and box-drawing)
        cleaned = re.sub(r'[\s─━│┌┐└┘├┤┬┴┼╌╍═║▔▁▏▕_\-=~]', '', s)
        if not cleaned:
            return True
        return False

    # Walk from bottom up
    for i in range(len(plain_lines) - 1, max(len(plain_lines) - 20, 0), -1):
        if is_chrome(plain_lines[i]):
            cut_at = i
        else:
            break

    return '\n'.join(lines[:cut_at]).rstrip()


def _parse_options(content: str) -> list[tuple[str, str]]:
    """Parse numbered options from Claude's active prompt only.

    Only looks at the bottom of the terminal and requires an active
    prompt indicator (Esc to cancel, Enter to select, etc.) to avoid
    picking up numbered items from Claude's output history.
    """
    # Strip all ANSI escape sequences
    plain = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', content)
    plain = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', plain)
    plain = re.sub(r'\x1b[()][AB012]', '', plain)
    plain = re.sub(r'\x1b[78DEHM]', '', plain)
    plain = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\r]', '', plain)
    plain = re.sub(r'[─│┌┐└┘├┤┬┴┼╌╍═║╔╗╚╝╠╣╦╩╬▀▄█▌▐░▒▓■□●○◐◑◒◓]', '', plain)

    # Only parse if there's an active prompt indicator on screen
    prompt_indicators = [
        r'Esc to cancel',
        r'Tab to amend',
        r'Enter to select',
        r'Enter to continue',
        r'to navigate',
        r'Do you want to proceed\?',
        r'Do you want to allow',
    ]
    if not any(re.search(p, plain) for p in prompt_indicators):
        return []

    # Only look at the bottom portion of the terminal (last 20 lines)
    lines = plain.splitlines()
    bottom = '\n'.join(lines[-20:]) if len(lines) > 20 else plain

    # Check for standard Claude permission prompt
    if re.search(r'Do you want to (proceed|allow)', bottom):
        options = []
        if re.search(r'1\.\s*Yes\b', bottom):
            options.append(("1", "Yes"))
        # Option 2 could be "Yes, allow..." or "No"
        m2 = re.search(r'2\.\s*(.+?)(?:\s{3,}|\n|$)', bottom)
        if m2:
            label = m2.group(1).strip()
            label = re.split(r'\s{3,}', label)[0].strip()
            if len(label) < 50:
                options.append(("2", label))
        # Option 3 if present
        m3 = re.search(r'3\.\s*(.+?)(?:\s{3,}|\n|$)', bottom)
        if m3:
            label = m3.group(1).strip()
            label = re.split(r'\s{3,}', label)[0].strip()
            if len(label) < 50:
                options.append(("3", label))
        if options:
            return options

    # Generic numbered option parsing — bottom of screen only
    options = []
    for line in bottom.splitlines():
        line = line.strip()
        m = re.match(r'^[›❯\)\s]*(\d+)\.\s+(?:\[[ x]\]\s+)?(.+)$', line)
        if m:
            num = m.group(1)
            label = m.group(2).strip()
            # Truncate at column boundaries (bash commands appear after gaps)
            label = re.split(r'\s{3,}', label)[0].strip()
            # Filter out noise
            if (label
                and 2 < len(label) < 50
                and 'hidden' not in label.lower()
                and not re.match(r'^[\s\-_=*~./]+$', label)):
                options.append((num, label))

    return options


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
    plain = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', content)
    plain = re.sub(r'\x1b\][^\x07]*\x07', '', plain)

    indicators = [
        r'Do you want to proceed\?',
        r'Do you want to allow',
        r'Select .+:',
        r'›\s*\d+\.',
        r'❯\s*\d+\.',
        r'\)\s*\d+\.',
        r'Esc to cancel',
        r'Tab to amend',
        r'Enter to continue',
        r'\[Y/n\]',
        r'\[y/N\]',
    ]
    return any(re.search(p, plain) for p in indicators)


def _system_notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            capture_output=True, check=False, timeout=3,
        )
    except Exception:
        pass


def _get_mcp_servers(root: Path) -> str | None:
    import json

    home = Path.home()
    all_servers: dict[str, str] = {}

    needs_auth: set[str] = set()
    auth_cache = home / ".claude" / "mcp-needs-auth-cache.json"
    if auth_cache.exists():
        try:
            data = json.loads(auth_cache.read_text())
            needs_auth = set(data.keys())
        except Exception:
            pass

    mcp_json = root / ".mcp.json"
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
            for name in data.get("mcpServers", {}):
                all_servers[name] = "connected"
        except Exception:
            pass

    claude_json = home / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
            root_str = str(root)
            for proj_key, proj_val in data.get("projects", {}).items():
                if isinstance(proj_val, dict) and root_str in proj_key:
                    for name in proj_val.get("mcpServers", {}):
                        all_servers[name] = "connected"
            for full_name in data.get("claudeAiMcpEverConnected", []):
                status = "auth" if full_name in needs_auth else "connected"
                short = full_name.replace("claude.ai ", "")
                all_servers[short] = status
        except Exception:
            pass

    settings = home / ".claude" / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            for plugin, enabled in data.get("enabledPlugins", {}).items():
                if enabled:
                    all_servers[plugin.split("@")[0]] = "connected"
        except Exception:
            pass

    if not all_servers:
        return None

    connected = [n for n, s in all_servers.items() if s == "connected"]
    needs_auth_list = [n for n, s in all_servers.items() if s == "auth"]

    lines = []
    if connected:
        lines.append(f"[green]●[/green] [dim]connected[/dim]  {', '.join(connected)}")
    if needs_auth_list:
        lines.append(f"[yellow]△[/yellow] [dim]needs auth[/dim] {', '.join(needs_auth_list)}")
    return "\n".join(lines)


def _status_styled(status: str) -> str:
    return {
        "idle": "[#00B894]● IDLE[/#00B894]",
        "busy": "[#7C6FF7]● BUSY[/#7C6FF7]",
        "done": "[#FDCB6E]● DONE[/#FDCB6E]",
        "claiming": "[dim]○ CLAIM[/dim]",
        "error": "[#FF6B6B]● ERROR[/#FF6B6B]",
    }.get(status, status)
