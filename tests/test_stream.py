"""Tests for stream-json marker parsing."""

import json

from lane.stream import find_pr_url, read_stream


def _assistant_event(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _result_event(text: str) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": text})


def test_reads_status_marker(tmp_path):
    log = tmp_path / "run.log"
    marker = '::at-status::{"ticket":"BDLS-1","phase":"build","note":"wiring handler","done":["spec"],"remaining":["tests"]}'
    log.write_text(_assistant_event(f"Working.\n{marker}\n") + "\n")

    read = read_stream(log, 0)

    assert len(read.markers) == 1
    m = read.markers[0]
    assert m.name == "status"
    assert m.payload["phase"] == "build"
    assert m.payload["done"] == ["spec"]
    assert read.new_offset > 0


def test_ignores_incomplete_trailing_line(tmp_path):
    log = tmp_path / "run.log"
    complete = _assistant_event('::at-status::{"ticket":"BDLS-1","phase":"spec"}') + "\n"
    partial = '{"type":"assistant","message":{"content":[{"type":"te'
    log.write_text(complete + partial)

    read = read_stream(log, 0)

    assert len(read.markers) == 1
    assert read.new_offset == len(complete.encode())

    log.write_text(complete + _assistant_event('::at-pr-open::{"ticket":"BDLS-1","url":"https://github.com/o/r/pull/9"}') + "\n")
    second = read_stream(log, read.new_offset)
    assert [m.name for m in second.markers] == ["pr-open"]


def test_terminal_marker_in_result_event(tmp_path):
    log = tmp_path / "run.log"
    marker = '::at-awaiting-answers::{"ticket":"BDLS-1","questions":["Which tier?","Copy for the CTA?"]}'
    log.write_text(_result_event(f"{marker}\nParked.") + "\n")

    read = read_stream(log, 0)

    assert [m.name for m in read.markers] == ["awaiting-answers"]
    assert read.markers[0].payload["questions"] == ["Which tier?", "Copy for the CTA?"]
    assert read.result_text is not None


def test_invalid_marker_json_is_skipped(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(_assistant_event("::at-status::{not json}") + "\n")

    assert read_stream(log, 0).markers == []


def test_non_json_lines_are_skipped(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("plain stderr noise\n" + _assistant_event('::at-status::{"phase":"ship"}') + "\n")

    assert len(read_stream(log, 0).markers) == 1


def test_missing_file_returns_offset_unchanged(tmp_path):
    read = read_stream(tmp_path / "nope.log", 42)
    assert read.markers == []
    assert read.new_offset == 42


def test_find_pr_url():
    assert find_pr_url("done: https://github.com/org/repo/pull/123 ok") == "https://github.com/org/repo/pull/123"
    assert find_pr_url("no url here") is None
    assert find_pr_url(None) is None
