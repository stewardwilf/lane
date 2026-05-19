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
from textual.widgets import Footer, Static, RichLog, DataTable, Input, Label

from lane.state import read_state, PoolState, Worktree, write_state
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
            f"[blue]busy={busy}[/blue]",
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


class OutputPane(RichLog):
    """Right pane — shows live agent output."""

    _last_line: str = ""

    def write_clean(self, line: str) -> None:
        """Write a line, stripping noise from Claude's TUI output."""
        cleaned = _strip_terminal_codes(line)
        text = cleaned.strip()
        if not text:
            return
        # Skip Claude's spinner/thinking noise
        if _is_noise(text):
            return
        # Deduplicate consecutive identical lines
        if text == self._last_line:
            return
        self._last_line = text
        self.write(Text.from_ansi(cleaned))


class ReplyInput(Input):
    """Input bar for sending messages to the running Claude session."""

    DEFAULT_CSS = """
    ReplyInput {
        dock: bottom;
        margin: 0 0;
    }
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
    OutputPane { height: 1fr; padding: 0 1; }
    #reply-bar {
        height: auto;
        border-top: solid $primary-background;
        background: $surface;
        padding: 0 1;
    }
    #reply-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("a", "attach", "Attach", show=True, priority=True),
        Binding("c", "continue_task", "Continue", show=True, priority=True),
        Binding("s", "stop", "Stop", show=True, priority=True),
        Binding("r", "release", "Release", show=True, priority=True),
        Binding("n", "new_task", "New task", show=True, priority=True),
        Binding("i", "focus_reply", "Reply", show=True, priority=True),
        Binding("q", "quit", "Quit", show=True, priority=True),
    ]

    root: Path
    _poll_timer: Timer | None = None
    _selected_wt_id: str | None = None
    _state: PoolState | None = None
    _table_initialized: bool = False
    _log_offsets: dict[str, int]

    def __init__(self, root: Path, **kwargs):
        super().__init__(**kwargs)
        self.root = root
        self._table_initialized = False
        self._log_offsets = {}

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
                yield OutputPane(id="output-pane", highlight=True, markup=True, max_lines=5000)
                with Vertical(id="reply-bar"):
                    yield Static("[dim]i to reply · sends to selected worktree's Claude session[/dim]", id="reply-hint")
                    yield ReplyInput(placeholder="Type a message to Claude...", id="reply-input")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_state()
        self._poll_timer = self.set_interval(0.5, self._refresh_state)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter in the reply input."""
        if event.input.id != "reply-input":
            return

        message = event.value.strip()
        event.input.value = ""

        if not message:
            # Refocus the table so keybinds work again
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
            # Send to running Claude session
            _send_to_tmux(wt.tmux_session, message)
            self.notify(f"Sent to {wt.id}", timeout=2)
        elif wt.status == "done":
            # Start a new Claude session with this as the prompt
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
            # Dispatch as a new task
            self.notify(f"Dispatching to {wt.id}...", timeout=2)
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
            self._tail_output(wt)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            new_id = str(event.row_key.value)
            if new_id == self._selected_wt_id:
                return

            self._selected_wt_id = new_id
            pane = self.query_one(OutputPane)
            pane.clear()

            if self._state:
                wt = next((w for w in self._state.worktrees if w.id == new_id), None)
                self.query_one(DetailPanel).update_from_worktree(wt)
                self._update_pane_header(wt)
                self._update_reply_hint(wt)
                if wt and wt.log_path:
                    self._load_full_log(wt)

    def _update_pane_header(self, wt: Worktree | None) -> None:
        header = self.query_one("#pane-header", Static)
        if not wt:
            header.update(" [dim]select a worktree[/dim]")
        elif wt.status == "busy":
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· [bold]a[/bold] attach · [bold]i[/bold] reply[/dim]")
        elif wt.status == "done":
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· [bold]i[/bold] continue · [bold]r[/bold] release[/dim]")
        elif wt.status == "idle":
            header.update(f" [bold]{wt.id}[/bold] [dim]· idle · [bold]i[/bold] or [bold]n[/bold] to dispatch[/dim]")
        else:
            header.update(f" [bold]{wt.id}[/bold] · {wt.task or ''} [dim]· {wt.status}[/dim]")

    def _update_reply_hint(self, wt: Worktree | None) -> None:
        hint = self.query_one("#reply-hint", Static)
        if not wt or wt.status == "idle":
            hint.update("[dim]i to type · dispatches a new task[/dim]")
        elif wt.status == "busy":
            hint.update(f"[dim]i to reply · sends to [bold]{wt.id}[/bold] Claude session[/dim]")
        elif wt.status == "done":
            hint.update(f"[dim]i to continue · starts new Claude session in [bold]{wt.id}[/bold][/dim]")

    def _load_full_log(self, wt: Worktree) -> None:
        if not wt.log_path:
            return
        log_path = Path(wt.log_path)
        if not log_path.exists():
            return

        pane = self.query_one(OutputPane)
        try:
            content = log_path.read_text()
            self._log_offsets[wt.id] = len(content)
            for line in content.splitlines():
                if line.strip():
                    pane.write_clean(line)
        except Exception:
            pass

    def _tail_output(self, wt: Worktree | None) -> None:
        if not wt or not wt.log_path:
            return

        log_path = Path(wt.log_path)
        if not log_path.exists():
            return

        pane = self.query_one(OutputPane)
        offset = self._log_offsets.get(wt.id, 0)

        try:
            size = log_path.stat().st_size
            if size <= offset:
                return

            with open(log_path, "r") as f:
                f.seek(offset)
                new_content = f.read()
                self._log_offsets[wt.id] = size

            for line in new_content.splitlines():
                if line.strip():
                    pane.write_clean(line)
        except Exception:
            pass

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
        pane = self.query_one(OutputPane)
        pane.clear()
        self._log_offsets[wt_id] = 0
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
            os.system(f"tmux attach-session -t {wt.tmux_session}")

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


def _send_to_tmux(session_name: str, text: str) -> None:
    """Send keystrokes to a tmux session."""
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


_NOISE_PATTERNS = re.compile(
    r'^[*+·.●◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏▸▶►]+$'       # bare spinner chars
    r'|thinking with'                              # thinking indicators
    r'|Simmering|Coalescing|Crystallizing|Bubbling|Percolating'  # claude spinners
    r'|Warming|Brewing|Distilling|Fermenting|Steeping'
    r'|▸▸accepted'                                 # claude footer bar
    r'|esc\s*to\s*interrupt'                       # footer hints
    r'|shift\+tab'                                 # footer hints
    r'|to run in background'                       # footer hints
    r'|ctrl\+b'                                    # tmux hint leaking
    r'|^\s*›\s*$'                                  # bare prompt char
    r'|^\s*❯\s*$'                                  # bare prompt char
)


def _is_noise(text: str) -> bool:
    """Return True if this line is Claude UI noise (spinners, footer, thinking)."""
    return bool(_NOISE_PATTERNS.search(text))


def _strip_terminal_codes(text: str) -> str:
    """Strip terminal control sequences, keeping only readable text + basic ANSI colors."""
    # OSC sequences: \e]...BEL or \e]...ST
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)
    # CSI sequences that aren't SGR (colors): cursor movement, clear, scroll, etc.
    # Keep SGR (ending in 'm') for color rendering
    text = re.sub(r'\x1b\[[0-9;]*[A-HJKSTfhlnr]', '', text)
    # DEC private modes: \e[?...h \e[?...l
    text = re.sub(r'\x1b\[\?[0-9;]*[hl]', '', text)
    # Other escape sequences
    text = re.sub(r'\x1b[()][AB012]', '', text)
    text = re.sub(r'\x1b[78DEHM]', '', text)
    # Carriage returns (overwrite artifacts)
    text = re.sub(r'\r', '', text)
    # Stray control characters (except newline, tab)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]', '', text)
    return text


def _status_styled(status: str) -> str:
    return {
        "idle": "[green]IDLE[/green]",
        "busy": "[blue]BUSY[/blue]",
        "done": "[yellow]DONE[/yellow]",
        "claiming": "[dim]CLAIM[/dim]",
        "error": "[red]ERROR[/red]",
    }.get(status, status)
