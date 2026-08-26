"""Tests for pipeline marker application, prompts and digests."""

from lane import tickets as tk
from lane.pipeline import _apply_markers, build_digest, build_prompt
from lane.stream import Marker


def test_build_prompt_fresh():
    assert build_prompt(tk.Ticket(id="BDLS-1")) == "/auto-ticket BDLS-1"


def test_build_prompt_resume_with_answers_and_approval():
    t = tk.Ticket(id="BDLS-1", answers="1) premium tier 2) yes", approved=True)
    assert build_prompt(t) == "/auto-ticket BDLS-1 APPROVED ANSWERS: 1) premium tier 2) yes"


def test_apply_status_markers_updates_progress():
    t = tk.Ticket(id="BDLS-1")
    terminal = _apply_markers(t, [
        Marker("status", {"phase": "build", "note": "handler", "done": ["spec"], "remaining": ["tests", "pr"]}),
        Marker("status", {"phase": "review", "note": "running checks"}),
    ])
    assert terminal is None
    assert t.phase == "review"
    assert t.note == "running checks"
    assert t.done == ["spec"]
    assert t.remaining == ["tests", "pr"]


def test_apply_terminal_markers():
    t = tk.Ticket(id="BDLS-1")
    assert _apply_markers(t, [Marker("awaiting-answers", {"questions": ["a?", "b?"]})]) == "awaiting-answers"
    assert t.questions == ["a?", "b?"]

    t2 = tk.Ticket(id="BDLS-2")
    assert _apply_markers(t2, [Marker("pr-open", {"url": "https://github.com/o/r/pull/1", "qa_label": True})]) == "pr-open"
    assert t2.pr_url == "https://github.com/o/r/pull/1"
    assert t2.qa_label is True

    t3 = tk.Ticket(id="BDLS-3")
    assert _apply_markers(t3, [Marker("failed", {"phase": "review", "error": "lint unfixable"})]) == "failed"
    assert t3.error == "lint unfixable"


def test_digest_covers_active_states_only():
    store = tk.TicketStore(tickets=[
        tk.Ticket(id="BDLS-1", state=tk.RUNNING, phase="build", wt_id="wt-01", note="wiring"),
        tk.Ticket(id="BDLS-2", state=tk.QUEUED),
        tk.Ticket(id="BDLS-3", state=tk.AWAITING_ANSWERS),
        tk.Ticket(id="BDLS-4", state=tk.DONE),
    ])
    digest = build_digest(store)
    assert digest is not None
    assert "BDLS-1" in digest and "build" in digest
    assert "BDLS-2" in digest and "queued" in digest
    assert "BDLS-3" in digest and "waiting on you" in digest
    assert "BDLS-4" not in digest


def test_digest_none_when_idle():
    assert build_digest(tk.TicketStore(tickets=[tk.Ticket(id="BDLS-1", state=tk.DONE)])) is None


def test_ticket_store_roundtrip(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".lane").mkdir()
    store = tk.TicketStore(tickets=[tk.Ticket(id="BDLS-1")])
    tk.write_tickets(store, tmp_path)

    with tk.with_tickets_lock(tmp_path) as locked:
        t = locked.find("bdls-1")
        assert t is not None
        t.state = tk.RUNNING

    assert tk.read_tickets(tmp_path).find("BDLS-1").state == tk.RUNNING
