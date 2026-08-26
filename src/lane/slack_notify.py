"""Slack notifications for the ticket pipeline — stdlib only, no SDK dependency.

Configured entirely via environment variables:

    LANE_SLACK_BOT_TOKEN   xoxb- bot token (chat:write, im:write, im:history, channels:history)
    LANE_SLACK_CHANNEL     channel ID for status digests (e.g. C0123456789)
    LANE_SLACK_DM_USER     user ID to DM questions/approvals to (e.g. U0123456789)

Without a token, every call degrades to printing to stdout so the pipeline
works Slack-less (answers then come via `lane answer` / `lane approve`).
"""

from __future__ import annotations

import json
import os
import urllib.request

SLACK_API = "https://slack.com/api"


def _token() -> str | None:
    return os.environ.get("LANE_SLACK_BOT_TOKEN")


def digest_channel() -> str | None:
    return os.environ.get("LANE_SLACK_CHANNEL")


def dm_user() -> str | None:
    return os.environ.get("LANE_SLACK_DM_USER")


def enabled() -> bool:
    return bool(_token())


def _call(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{SLACK_API}/{method}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def post_message(text: str, channel: str | None = None, thread_ts: str | None = None) -> tuple[str | None, str | None]:
    """Post a message. Returns (channel, ts) or (None, None) when Slack is off or the call fails."""
    if not enabled():
        print(f"[lane slack-off] {text}")
        return (None, None)
    target = channel or digest_channel()
    if not target:
        print(f"[lane slack-off] no channel configured: {text}")
        return (None, None)
    payload: dict = {"channel": target, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        r = _call("chat.postMessage", payload)
    except Exception as e:
        print(f"[lane] slack post failed: {e}")
        return (None, None)
    if not r.get("ok"):
        print(f"[lane] slack post failed: {r.get('error')}")
        return (None, None)
    return (r.get("channel"), r.get("ts"))


def open_dm() -> str | None:
    """Open (or fetch) the DM channel with the configured user."""
    user = dm_user()
    if not enabled() or not user:
        return None
    try:
        r = _call("conversations.open", {"users": user})
    except Exception as e:
        print(f"[lane] slack conversations.open failed: {e}")
        return None
    if not r.get("ok"):
        print(f"[lane] slack conversations.open failed: {r.get('error')}")
        return None
    return (r.get("channel") or {}).get("id")


def fetch_thread_replies(channel: str, thread_ts: str) -> list[dict]:
    """Human replies in a thread (excludes the parent and any bot messages)."""
    if not enabled():
        return []
    try:
        r = _call("conversations.replies", {"channel": channel, "ts": thread_ts, "limit": 50})
    except Exception as e:
        print(f"[lane] slack conversations.replies failed: {e}")
        return []
    if not r.get("ok"):
        return []
    replies = []
    for msg in r.get("messages", []):
        if msg.get("ts") == thread_ts:
            continue
        if msg.get("bot_id"):
            continue
        replies.append(msg)
    return replies
