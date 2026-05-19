"""Textual TUI dashboard for lane."""

from __future__ import annotations

import os
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
from textual.widgets import Footer, Static, RichLog, DataTable, Input, Label

from lane.state import read_state, PoolState, Worktree, write_state
from lane.recovery import check_stale_workers, auto_recover
from lane.runner import kill_agent, is_tmux_session_alive


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
        error = sum(1 for w in state.worktrees if w.status == "error")
        total = len(state.worktrees)
        base = state.config.base_branch

        self.update(
            f" [bold]lane[/bold]  ·  "
            f"pool={total}  "
            f"[blue]busy={busy}[/blue]  "
            f"[green]idle={idle}[/green]  "
            f"[red]errors={error}[/red]  "
            f"·  base={base}"
        )


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


class TerminalPane(Static):
    """Right pane — shows live tmux capture-pane output (what Claude actually looks like)."""

    DEFAULT_CSS = """
    TerminalPane {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    _last_content: str = ""

    def refresh_from_tmux(self, session_name: str | None) -> None:
        if not session_name:
            if self._last_content:
                return  # Keep showing the last output
            self.update("[dim]Select a busy worktree to see agent output[/dim]")
            return

        content = _capture_tmux_pane(session_name)
        if content is not None:
            self._last_content = content
            self.update(content)

    def clear_content(self) -> None:
        self._last_content = ""
        self.update("")


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

    def compose(self) -> ComposeResult:
        with Vertical(id="task-dialog"):
            yield Label("[bold]New task[/bold]  [dim]describe the work for the agent[/dim]", id="task-label")
            yield Input(placeholder="e.g. Fix the broken login redirect", id="task-input")

    def on_mount(self) -> None:
        self.query_one("#task-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LaneDashboard(App):
    TITLE = "lane dashboard"

    CSS = """
    Screen { layout: vertical; }
    StatusBar {
        height: 3; padding: 1 2;
        background: $surface;
        border-bottom: solid $primary-background;
    }
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
    TerminalPane { height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        Binding("a", "attach", "Attach", show=True),
        Binding("s", "stop", "Stop", show=True),
        Binding("r", "release", "Release", show=True),
        Binding("n", "new_task", "New task", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    root: Path
    _poll_timer: Timer | None = None
    _selected_wt_id: str | None = None
    _state: PoolState | None = None
    _table_initialized: bool = False

    def __init__(self, root: Path, **kwargs):
        super().__init__(**kwargs)
        self.root = root
        self._table_initialized = False

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield WorktreeTable()
                yield DetailPanel(id="detail")
            with Vertical(id="right"):
                yield Static("", id="pane-header")
                yield TerminalPane(id="term-pane")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_state()
        self._poll_timer = self.set_interval(0.5, self._refresh_state)

    def _refresh_state(self) -> None:
        try:
            state = read_state(self.root)
        except SystemExit:
            return

        stale = check_stale_workers(state, self.root)
        if stale:
            Thread(target=auto_recover, args=(self.root, stale), daemon=True).start()

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
                    prev_selected = self._selected_wt_id
                    table.clear()
                    self._table_initialized = False
                    self._refresh_state()
                    if prev_selected:
                        try:
                            idx = next(i for i, w in enumerate(state.worktrees) if w.id == prev_selected)
                            table.move_cursor(row=idx)
                        except (StopIteration, Exception):
                            pass
                    return

        # Update right pane — live tmux capture for selected worktree
        if self._selected_wt_id:
            wt = next((w for w in state.worktrees if w.id == self._selected_wt_id), None)
            self.query_one(DetailPanel).update_from_worktree(wt)
            self._update_pane_header(wt)
            term = self.query_one(TerminalPane)
            if wt and wt.tmux_session:
                term.refresh_from_tmux(wt.tmux_session)
            elif wt and wt.log_path:
                # Fallback: show log content for finished tasks
                term.refresh_from_tmux(None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self._selected_wt_id = str(event.row_key.value)
            self.query_one(TerminalPane).clear_content()

            if self._state:
                wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
                self.query_one(DetailPanel).update_from_worktree(wt)
                self._update_pane_header(wt)

    def _update_pane_header(self, wt: Worktree | None) -> None:
        header = self.query_one("#pane-header", Static)
        if wt and wt.tmux_session and wt.status == "busy":
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· press [bold]a[/bold] to interact[/dim]")
        elif wt and wt.status == "idle":
            header.update(f" [bold]{wt.id}[/bold] [dim]· idle[/dim]")
        elif wt:
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· {wt.status}[/dim]")
        else:
            header.update(" [dim]select a worktree[/dim]")

    # ── Actions ─────────────────────────────────────────────────

    def action_new_task(self) -> None:
        self.push_screen(TaskInputScreen(), self._on_task_submitted)

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

    def _select_worktree(self, wt_id: str) -> None:
        self._selected_wt_id = wt_id
        self.query_one(TerminalPane).clear_content()
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
            self.notify("No tmux session for this worktree", severity="warning")
            return
        if wt.status != "busy":
            self.notify(f"{wt.id} is not running", severity="warning")
            return

        with self.suspend():
            os.system(f"tmux attach-session -t {wt.tmux_session}")

    def action_stop(self) -> None:
        if not self._selected_wt_id or not self._state:
            return
        wt = next((w for w in self._state.worktrees if w.id == self._selected_wt_id), None)
        if not wt or wt.status not in ("busy", "error"):
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

        def _release():
            from lane.cli import _do_release
            try:
                _do_release(self.root, self._selected_wt_id, quiet=True)
                self.call_from_thread(self.notify, f"Released {self._selected_wt_id}")
            except Exception as e:
                self.call_from_thread(self.notify, f"Release failed: {e}", severity="error")

        Thread(target=_release, daemon=True).start()


def _capture_tmux_pane(session_name: str) -> str | None:
    """Capture the current visible content of a tmux pane."""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-e"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return None


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


def _status_styled(status: str) -> str:
    return {
        "idle": "[green]IDLE[/green]",
        "busy": "[blue]BUSY[/blue]",
        "claiming": "[yellow]CLAIM[/yellow]",
        "releasing": "[yellow]RELEASE[/yellow]",
        "error": "[red]ERROR[/red]",
    }.get(status, status)
