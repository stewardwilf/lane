"""Ticket pipeline — dispatch queued tickets to worktrees as headless Claude runs,
monitor their stream-json output, park on questions/approvals, resume on answers,
release on PR-open, and post Slack digests.

Environment configuration:

    LANE_TICKET_PROMPT   prompt template, default "/auto-ticket {ticket}"
    LANE_CLAUDE_BIN      claude binary, default "claude"
    LANE_POLL_SECONDS    scheduler tick, default 10
    LANE_DIGEST_SECONDS  digest interval, default 120
    LANE_STUCK_MINUTES   no-output threshold before a stuck alert, default 15
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from lane import slack_notify, stream, tickets as tk
from lane.runner import is_pid_alive
from lane.state import now_iso, read_state, with_state_lock

SPEC_DIR_TEMPLATE = ".claude/auto-ticket/{ticket}"
SAVED_SPECS_DIR = "specs"


def _prompt_template() -> str:
    return os.environ.get("LANE_TICKET_PROMPT", "/auto-ticket {ticket}")


def _claude_bin() -> str:
    return os.environ.get("LANE_CLAUDE_BIN", "claude")


def _poll_seconds() -> int:
    return int(os.environ.get("LANE_POLL_SECONDS", "10"))


def _digest_seconds() -> int:
    return int(os.environ.get("LANE_DIGEST_SECONDS", "120"))


def _stuck_seconds() -> int:
    return int(os.environ.get("LANE_STUCK_MINUTES", "15")) * 60


def build_prompt(ticket: tk.Ticket) -> str:
    prompt = _prompt_template().format(ticket=ticket.id)
    if ticket.approved:
        prompt += " APPROVED"
    if ticket.answers:
        prompt += f" ANSWERS: {ticket.answers}"
    return prompt


# ── spec artifact preservation across park/resume ───────────────

def _saved_spec_dir(root: Path, ticket_id: str) -> Path:
    from lane.config import lane_dir
    return lane_dir(root) / SAVED_SPECS_DIR / ticket_id


def save_spec_artifacts(root: Path, wt_abs: Path, ticket_id: str) -> None:
    src = wt_abs / SPEC_DIR_TEMPLATE.format(ticket=ticket_id)
    if not src.exists():
        return
    dst = _saved_spec_dir(root, ticket_id)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def restore_spec_artifacts(root: Path, wt_abs: Path, ticket_id: str) -> None:
    src = _saved_spec_dir(root, ticket_id)
    if not src.exists():
        return
    dst = wt_abs / SPEC_DIR_TEMPLATE.format(ticket=ticket_id)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def _sync_commands(root: Path, wt_abs: Path) -> None:
    """Copy repo-local skills into the worktree so unmerged skill edits are live."""
    src = root / ".claude" / "commands"
    if not src.exists():
        return
    dst = wt_abs / ".claude" / "commands"
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.md"):
        shutil.copy2(f, dst / f.name)


# ── dispatch ─────────────────────────────────────────────────────

def dispatch_ticket(root: Path, ticket_id: str) -> str | None:
    """Claim an idle worktree and start a headless run. Returns an error message or None."""
    claimed_id: str | None = None
    with with_state_lock(root) as pool:
        for wt in pool.worktrees:
            if wt.status == "idle":
                wt.status = "claiming"
                claimed_id = wt.id
                break
    if claimed_id is None:
        return "no idle worktrees"

    pool = read_state(root)
    wt = next((w for w in pool.worktrees if w.id == claimed_id), None)
    if wt is None:
        return f"worktree {claimed_id} disappeared"
    wt_abs = root / wt.path

    from lane.cli import _sync_claude_settings
    _sync_claude_settings(root, [wt])
    _sync_commands(root, wt_abs)

    store = tk.read_tickets(root)
    ticket = store.find(ticket_id)
    if ticket is None:
        _unclaim(root, claimed_id)
        return f"ticket {ticket_id} not found"

    restore_spec_artifacts(root, wt_abs, ticket.id)

    log_file = root / pool.config.logs_dir / f"ticket-{ticket.id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("")

    cmd = [
        _claude_bin(),
        "-p", build_prompt(ticket),
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    with open(log_file, "a") as out:
        proc = subprocess.Popen(
            cmd,
            cwd=str(wt_abs),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    with with_state_lock(root) as pool:
        for w in pool.worktrees:
            if w.id == claimed_id:
                w.status = "busy"
                w.task = f"ticket {ticket.id}"
                w.task_id = ticket.id
                w.pid = proc.pid
                w.tmux_session = None
                w.log_path = str(log_file)
                w.started_at = now_iso()
                break

    with tk.with_tickets_lock(root) as store:
        t = store.find(ticket_id)
        if t:
            t.state = tk.RUNNING
            t.phase = "spec"
            t.wt_id = claimed_id
            t.pid = proc.pid
            t.log_path = str(log_file)
            t.log_offset = 0
            t.stuck_notified = False
            t.started_at = now_iso()
            t.touch()
    return None


def _unclaim(root: Path, wt_id: str) -> None:
    with with_state_lock(root) as pool:
        for w in pool.worktrees:
            if w.id == wt_id and w.status == "claiming":
                w.status = "idle"
                break


# ── marker application ───────────────────────────────────────────

def _apply_markers(ticket: tk.Ticket, markers: list[stream.Marker]) -> str | None:
    """Apply markers to a ticket; returns the last terminal marker name seen."""
    terminal: str | None = None
    for m in markers:
        if m.name == stream.STATUS:
            ticket.phase = m.payload.get("phase") or ticket.phase
            ticket.note = m.payload.get("note") or ticket.note
            if isinstance(m.payload.get("done"), list):
                ticket.done = m.payload["done"]
            if isinstance(m.payload.get("remaining"), list):
                ticket.remaining = m.payload["remaining"]
        elif m.name == stream.AWAITING_ANSWERS:
            ticket.questions = [q for q in m.payload.get("questions", []) if isinstance(q, str)]
            terminal = m.name
        elif m.name == stream.AWAITING_APPROVAL:
            ticket.approval_reason = m.payload.get("reason")
            ticket.note = m.payload.get("spec_summary") or ticket.note
            terminal = m.name
        elif m.name == stream.PR_OPEN:
            ticket.pr_url = m.payload.get("url")
            ticket.qa_label = bool(m.payload.get("qa_label"))
            terminal = m.name
        elif m.name == stream.FAILED:
            ticket.error = m.payload.get("error")
            ticket.phase = m.payload.get("phase") or ticket.phase
            terminal = m.name
        ticket.touch()
    return terminal


# ── monitoring ───────────────────────────────────────────────────

def poll_running(root: Path) -> None:
    store = tk.read_tickets(root)
    for ticket in [t for t in store.tickets if t.state == tk.RUNNING]:
        read = stream.read_stream(ticket.log_path or "", ticket.log_offset)
        terminal: str | None = None
        with tk.with_tickets_lock(root) as locked:
            t = locked.find(ticket.id)
            if t is None:
                continue
            terminal = _apply_markers(t, read.markers)
            t.log_offset = read.new_offset
            ticket = t

        alive = is_pid_alive(ticket.pid or -1)
        if alive:
            _check_stuck(root, ticket)
            continue

        _finalize(root, ticket, terminal, read.result_text)


def _check_stuck(root: Path, ticket: tk.Ticket) -> None:
    if ticket.stuck_notified or not ticket.log_path:
        return
    log = Path(ticket.log_path)
    if not log.exists():
        return
    idle = time.time() - log.stat().st_mtime
    if idle < _stuck_seconds():
        return
    slack_notify.post_message(
        f":warning: `{ticket.id}` has produced no output for {int(idle // 60)}m "
        f"(phase: {ticket.phase or '?'}, worktree {ticket.wt_id}). "
        f"Inspect with `lane logs-ticket {ticket.id}` or `lane stop {ticket.wt_id}`."
    )
    with tk.with_tickets_lock(root) as locked:
        t = locked.find(ticket.id)
        if t:
            t.stuck_notified = True
            t.touch()


def _finalize(root: Path, ticket: tk.Ticket, terminal: str | None, result_text: str | None) -> None:
    pool = read_state(root)
    wt = next((w for w in pool.worktrees if w.id == ticket.wt_id), None)
    wt_abs = root / wt.path if wt else None

    if terminal is None and stream.find_pr_url(result_text):
        ticket.pr_url = stream.find_pr_url(result_text)
        terminal = stream.PR_OPEN

    if terminal in (stream.AWAITING_ANSWERS, stream.AWAITING_APPROVAL) and wt_abs:
        save_spec_artifacts(root, wt_abs, ticket.id)

    if terminal == stream.AWAITING_ANSWERS:
        new_state = tk.AWAITING_ANSWERS
        _release_worktree(root, ticket.wt_id)
        _notify_questions(ticket)
    elif terminal == stream.AWAITING_APPROVAL:
        new_state = tk.AWAITING_APPROVAL
        _release_worktree(root, ticket.wt_id)
        _notify_approval(ticket)
    elif terminal == stream.PR_OPEN:
        new_state = tk.PR_OPEN
        _release_worktree(root, ticket.wt_id)
        slack_notify.post_message(
            f":white_check_mark: `{ticket.id}` PR open: {ticket.pr_url}"
            + (" (QA run labelled)" if ticket.qa_label else "")
        )
    else:
        new_state = tk.NEEDS_HUMAN
        _hold_worktree(root, ticket.wt_id)
        detail = ticket.error or _log_tail(ticket.log_path)
        slack_notify.post_message(
            f":rotating_light: `{ticket.id}` needs a human (phase: {ticket.phase or '?'}, "
            f"worktree {ticket.wt_id} kept for inspection).\n{detail}"
        )

    with tk.with_tickets_lock(root) as locked:
        t = locked.find(ticket.id)
        if t:
            t.state = new_state
            t.pid = None
            t.questions = ticket.questions
            t.approval_reason = ticket.approval_reason
            t.pr_url = ticket.pr_url
            t.qa_label = ticket.qa_label
            t.error = ticket.error
            t.slack_channel = ticket.slack_channel
            t.slack_thread_ts = ticket.slack_thread_ts
            if new_state != tk.NEEDS_HUMAN:
                t.wt_id = None
            t.touch()


def _log_tail(log_path: str | None, lines: int = 5) -> str:
    if not log_path or not Path(log_path).exists():
        return "(no log)"
    tail = Path(log_path).read_text(errors="replace").splitlines()[-lines:]
    return "```" + "\n".join(line[:300] for line in tail) + "```"


def _release_worktree(root: Path, wt_id: str | None) -> None:
    if not wt_id:
        return
    from lane.cli import _do_release
    _do_release(root, wt_id)


def _hold_worktree(root: Path, wt_id: str | None) -> None:
    if not wt_id:
        return
    with with_state_lock(root) as pool:
        for w in pool.worktrees:
            if w.id == wt_id:
                w.status = "done"
                w.pid = None
                break


# ── slack question/approval round-trips ──────────────────────────

def _notify_questions(ticket: tk.Ticket) -> None:
    numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(ticket.questions)) or "(no questions parsed — check the log)"
    channel = slack_notify.open_dm()
    ch, ts = slack_notify.post_message(
        f":question: `{ticket.id}` is parked on questions:\n{numbered}\n\n"
        f"Reply in this thread and lane will resume the ticket. "
        f"(Or run `lane answer {ticket.id} \"...\"`.)",
        channel=channel,
    )
    ticket.slack_channel = ch
    ticket.slack_thread_ts = ts


def _notify_approval(ticket: tk.Ticket) -> None:
    channel = slack_notify.open_dm()
    ch, ts = slack_notify.post_message(
        f":lock: `{ticket.id}` is parked on the risk gate: {ticket.approval_reason or 'unspecified'}\n"
        f"{ticket.note or ''}\n\n"
        f"Reply `approve` in this thread to proceed. (Or run `lane approve {ticket.id}`.)",
        channel=channel,
    )
    ticket.slack_channel = ch
    ticket.slack_thread_ts = ts


def check_replies(root: Path) -> None:
    store = tk.read_tickets(root)
    for ticket in [t for t in store.tickets if t.state in tk.PARKED_STATES]:
        if not (ticket.slack_channel and ticket.slack_thread_ts):
            continue
        replies = slack_notify.fetch_thread_replies(ticket.slack_channel, ticket.slack_thread_ts)
        if not replies:
            continue
        text = "\n".join(r.get("text", "") for r in replies).strip()
        if not text:
            continue
        if ticket.state == tk.AWAITING_APPROVAL:
            if "approve" not in text.lower():
                continue
            approve_ticket(root, ticket.id)
            slack_notify.post_message(":+1: approved, requeued", channel=ticket.slack_channel, thread_ts=ticket.slack_thread_ts)
        else:
            answer_ticket(root, ticket.id, text)
            slack_notify.post_message(":+1: answers received, requeued", channel=ticket.slack_channel, thread_ts=ticket.slack_thread_ts)


def answer_ticket(root: Path, ticket_id: str, answers: str) -> str | None:
    with tk.with_tickets_lock(root) as store:
        t = store.find(ticket_id)
        if t is None:
            return f"ticket {ticket_id} not found"
        t.answers = answers
        t.state = tk.QUEUED
        t.slack_channel = None
        t.slack_thread_ts = None
        t.touch()
    return None


def approve_ticket(root: Path, ticket_id: str) -> str | None:
    with tk.with_tickets_lock(root) as store:
        t = store.find(ticket_id)
        if t is None:
            return f"ticket {ticket_id} not found"
        t.approved = True
        t.state = tk.QUEUED
        t.slack_channel = None
        t.slack_thread_ts = None
        t.touch()
    return None


# ── digest ───────────────────────────────────────────────────────

def build_digest(store: tk.TicketStore) -> str | None:
    active = [t for t in store.tickets if t.state in tk.ACTIVE_STATES]
    if not active:
        return None
    lines = ["*lane pipeline*"]
    for t in active:
        if t.state == tk.RUNNING:
            done = f" · done: {', '.join(t.done[-3:])}" if t.done else ""
            remaining = f" · next: {', '.join(t.remaining[:3])}" if t.remaining else ""
            lines.append(f"• `{t.id}` {t.phase or 'starting'} ({t.wt_id}) — {t.note or '...'}{done}{remaining}")
        elif t.state == tk.QUEUED:
            lines.append(f"• `{t.id}` queued")
        else:
            lines.append(f"• `{t.id}` {t.state} — waiting on you")
    return "\n".join(lines)


# ── scheduler loop ───────────────────────────────────────────────

def tick(root: Path) -> None:
    poll_running(root)
    check_replies(root)

    store = tk.read_tickets(root)
    for ticket in [t for t in store.tickets if t.state == tk.QUEUED]:
        err = dispatch_ticket(root, ticket.id)
        if err == "no idle worktrees":
            break


def run_loop(root: Path, once: bool = False) -> None:
    last_digest = 0.0
    while True:
        tick(root)

        if time.time() - last_digest >= _digest_seconds():
            digest = build_digest(tk.read_tickets(root))
            if digest:
                slack_notify.post_message(digest)
            last_digest = time.time()

        if once:
            return
        time.sleep(_poll_seconds())
