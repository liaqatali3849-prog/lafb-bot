#!/usr/bin/env python3
"""
Gmail powers for LAFB_Bot via Composio v3 REST API.
Imported by main.py. Uses only verified tools:
- GMAIL_GET_PROFILE
- GMAIL_FETCH_EMAILS
- GMAIL_MOVE_TO_TRASH  (safe: recoverable for 30 days)

Design rules:
- Safe by default: nothing is deleted without user seeing it first.
- Never permanent-delete (GMAIL_DELETE_MESSAGE is intentionally NOT used).
- Honest errors: if Composio/Gmail fails, the user hears the truth.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
# These identify Liaqat's connected Gmail account (created & approved via
# Composio's secure OAuth flow - the password never touches this bot).
CONNECTED_ACCOUNT_ID = "ca_8fen8njaqNCw"
COMPOSIO_USER_ID = "liaqat"


def gmail_available() -> bool:
    return bool(COMPOSIO_API_KEY) and bool(CONNECTED_ACCOUNT_ID)


def _execute_tool(tool_slug: str, arguments: dict) -> dict:
    """Execute a Composio tool. Returns the tool's data dict. Raises on failure.
    NOTE (verified live): results live under data['messages'] / data directly,
    NOT under data['response_data']."""
    url = f"{COMPOSIO_BASE}/tools/execute/{tool_slug}"
    payload = {
        "connected_account_id": CONNECTED_ACCOUNT_ID,
        "user_id": COMPOSIO_USER_ID,
        "arguments": arguments,
    }
    r = requests.post(
        url,
        json=payload,
        headers={"X-API-Key": COMPOSIO_API_KEY},
        timeout=90,
    )
    data = r.json()
    if not data.get("successful", False):
        err = data.get("error") or data
        raise RuntimeError(str(err)[:200])
    return data.get("data", {})


def get_profile() -> str:
    """One-line mailbox stats: address + counts."""
    d = _execute_tool("GMAIL_GET_PROFILE", {})
    rd = d.get("response_data", d)  # profile uses response_data, fall back to d
    return (
        f"📧 {rd.get('emailAddress', '?')} — "
        f"{rd.get('messagesTotal', '?')} messages, "
        f"{rd.get('threadsTotal', '?')} threads"
    )


def find_big_newsletters(limit: int = 25) -> list:
    """
    Fetch recent messages (verified shape: data.messages) and flag those with
    attachments as cleanup candidates. Returns list of dicts.
    NOTE (verified live): query filters return empty on this connection,
    so we fetch broadly and filter locally.
    """
    all_items = []
    seen_ids = set()
    page_token = None
    for _ in range(3):  # up to 3 pages (~150 emails sample)
        args = {"max_results": 50, "verbose": True, "include_payload": False}
        if page_token:
            args["page_token"] = page_token
        d = _execute_tool("GMAIL_FETCH_EMAILS", args)
        msgs = d.get("messages", []) if isinstance(d, dict) else []
        for m in msgs:
            mid = m.get("messageId", "")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            labels = m.get("labelIds", []) or []
            if "CHAT" in labels or "TRASH" in labels:
                continue
            all_items.append(
                {
                    "id": mid,
                    "from": (m.get("sender") or "?")[:40],
                    "subject": (m.get("subject") or "(no subject)")[:60],
                    "date": (m.get("messageTimestamp") or "")[:10],
                    "attachments": len(m.get("attachmentList", []) or []),
                }
            )
        page_token = d.get("nextPageToken") if isinstance(d, dict) else None
        if not page_token or not msgs:
            break

    with_att = [i for i in all_items if i["attachments"] > 0]
    return with_att[:limit] if with_att else all_items[:limit]


def trash_message(message_id: str) -> bool:
    """Move ONE message to trash (recoverable 30 days). Returns True on success."""
    d = _execute_tool("GMAIL_MOVE_TO_TRASH", {"message_id": message_id})
    return bool(d)


def format_report(profile_line: str, items: list) -> str:
    lines = [profile_line, "", "🗂️ Old emails with attachments (candidates for cleanup):"]
    if not items:
        lines.append("None found in the first scan — your mailbox looks lean!")
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}. {it['subject']} — from {it['from']} on {it['date']} ({it['attachments']} att.)"
        )
    lines.append(
        "\n♻️ These are TRASH candidates (recoverable for 30 days, never permanent).\n"
        "Reply with numbers to trash, e.g.: 1 3 5  — or /gmail scan again."
    )
    return "\n".join(lines[:40])
