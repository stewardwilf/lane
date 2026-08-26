"""Ticket pipeline state store — a queue of Linear tickets moving through the auto-ticket lifecycle."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Generator

from lane.config import lane_dir
from lane.state import now_iso

TICKETS_FILE = "tickets.json"
TICKETS_LOCK = "tickets.lock"

# Lifecycle states. The build phase within RUNNING (spec/build/review/ship/qa)
# is tracked separately on Ticket.phase from ::at-status:: markers.
QUEUED = "queued"
RUNNING = "running"
AWAITING_ANSWERS = "awaiting-answers"
AWAITING_APPROVAL = "awaiting-approval"
PR_OPEN = "pr-open"
NEEDS_HUMAN = "needs-human"
DONE = "done"

ACTIVE_STATES = (QUEUED, RUNNING, AWAITING_ANSWERS, AWAITING_APPROVAL)
PARKED_STATES = (AWAITING_ANSWERS, AWAITING_APPROVAL)


@dataclass
class Ticket:
    id: str
    state: str = QUEUED
    phase: str | None = None
    note: str | None = None
    done: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    wt_id: str | None = None
    pid: int | None = None
    log_path: str | None = None
    log_offset: int = 0
    questions: list[str] = field(default_factory=list)
    answers: str | None = None
    approved: bool = False
    approval_reason: str | None = None
    pr_url: str | None = None
    qa_label: bool = False
    error: str | None = None
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    stuck_notified: bool = False
    queued_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Ticket:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def touch(self) -> None:
        self.updated_at = now_iso()


@dataclass
class TicketStore:
    version: int = 1
    tickets: list[Ticket] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"version": self.version, "tickets": [t.to_dict() for t in self.tickets]}

    @classmethod
    def from_dict(cls, d: dict) -> TicketStore:
        return cls(
            version=d.get("version", 1),
            tickets=[Ticket.from_dict(t) for t in d.get("tickets", [])],
        )

    def find(self, ticket_id: str) -> Ticket | None:
        for t in self.tickets:
            if t.id.upper() == ticket_id.upper():
                return t
        return None


def tickets_path(root: Path | None = None) -> Path:
    return lane_dir(root) / TICKETS_FILE


def tickets_lock_path(root: Path | None = None) -> Path:
    return lane_dir(root) / TICKETS_LOCK


def read_tickets(root: Path | None = None) -> TicketStore:
    tp = tickets_path(root)
    if not tp.exists():
        return TicketStore()
    return TicketStore.from_dict(json.loads(tp.read_text()))


def write_tickets(store: TicketStore, root: Path | None = None) -> None:
    tp = tickets_path(root)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps(store.to_dict(), indent=2) + "\n")


@contextmanager
def with_tickets_lock(root: Path | None = None) -> Generator[TicketStore, None, None]:
    lp = tickets_lock_path(root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            store = read_tickets(root)
            yield store
            write_tickets(store, root)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
