"""Parse Claude Code stream-json output for auto-ticket status markers.

The auto-ticket skill emits single-line markers in its assistant text:

    ::at-status::{"ticket":"BDLS-1234","phase":"build","note":"...","done":[...],"remaining":[...]}
    ::at-awaiting-answers::{"ticket":"BDLS-1234","questions":["..."]}
    ::at-awaiting-approval::{"ticket":"BDLS-1234","reason":"...","spec_summary":"..."}
    ::at-pr-open::{"ticket":"BDLS-1234","url":"...","qa_label":true}
    ::at-failed::{"ticket":"BDLS-1234","phase":"...","error":"..."}

Each stream-json event is one JSON object per line; assistant text lives at
message.content[].text. Markers are extracted from complete lines only, so a
partially written trailing line is left for the next read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MARKER_RE = re.compile(r"^::at-([a-z-]+)::(\{.*\})\s*$")
PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

STATUS = "status"
AWAITING_ANSWERS = "awaiting-answers"
AWAITING_APPROVAL = "awaiting-approval"
PR_OPEN = "pr-open"
FAILED = "failed"

TERMINAL_MARKERS = (AWAITING_ANSWERS, AWAITING_APPROVAL, PR_OPEN, FAILED)


@dataclass
class Marker:
    name: str
    payload: dict


@dataclass
class StreamRead:
    markers: list[Marker]
    new_offset: int
    result_text: str | None = None


def _texts_from_event(event: dict) -> list[str]:
    texts: list[str] = []
    message = event.get("message")
    if isinstance(message, dict):
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
    if event.get("type") == "result" and isinstance(event.get("result"), str):
        texts.append(event["result"])
    return texts


def _markers_from_text(text: str) -> list[Marker]:
    markers: list[Marker] = []
    for line in text.splitlines():
        m = MARKER_RE.match(line.strip())
        if not m:
            continue
        try:
            payload = json.loads(m.group(2))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            markers.append(Marker(name=m.group(1), payload=payload))
    return markers


def read_stream(log_path: str | Path, offset: int = 0) -> StreamRead:
    """Read new complete lines from a stream-json log and extract markers."""
    path = Path(log_path)
    if not path.exists():
        return StreamRead(markers=[], new_offset=offset)

    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read()

    last_newline = raw.rfind(b"\n")
    if last_newline == -1:
        return StreamRead(markers=[], new_offset=offset)
    complete = raw[: last_newline + 1]
    new_offset = offset + len(complete)

    markers: list[Marker] = []
    result_text: str | None = None
    for line in complete.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for text in _texts_from_event(event):
            markers.extend(_markers_from_text(text))
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            result_text = event["result"]

    return StreamRead(markers=markers, new_offset=new_offset, result_text=result_text)


def find_pr_url(text: str | None) -> str | None:
    if not text:
        return None
    m = PR_URL_RE.search(text)
    return m.group(0) if m else None
