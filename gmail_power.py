#!/usr/bin/env python3
"""
Gmail powers for LAFB_Bot via Composio v3 REST API.
Imported by main.py. Uses only verified tools:
- GMAIL_GET_PROFILE
- GMAIL_FETCH_EMAILS
- GMAIL_CREATE_EMAIL_DRAFT (drafts only - never sends)
- GMAIL_MOVE_TO_TRASH  (safe: recoverable for 30 days)

MULTI-MAILBOX: all functions take account_id (a Composio connected
account id). If omitted, the first/original mailbox is used. Call
discover_mailboxes() to list every mailbox Liaqat has connected.

Design rules:
- Safe by default: nothing is deleted without user seeing it first.
- Never permanent-delete (GMAIL_DELETE_MESSAGE is intentionally NOT used).
- Honest errors: if Composio/Gmail fails, the user hears the truth.
"""

import os
import re
import logging
import requests

logger = logging.getLogger(__name__)


def extract_email_address(sender: str) -> str:
    """'Liaqat Ali <rite2olive@gmail.com>' -> 'rite2olive@gmail.com'"""
    m = re.search(r"<([\w.+-]+@[\w-]+\.[\w.]+)>", sender or "")
    if m:
        return m.group(1)
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", sender or "")
    return m.group(0) if m else (sender or "").strip()

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
# These identify Liaqat's connected Gmail account (created & approved via
# Composio's secure OAuth flow - the password never touches this bot).
CONNECTED_ACCOUNT_ID = "ca_8fen8njaqNCw"
COMPOSIO_USER_ID = "liaqat"
AUTH_CONFIG_ID = "ac_3QpFnKtTqTYk"


def gmail_available() -> bool:
    return bool(COMPOSIO_API_KEY)


def list_connected_accounts() -> list:
    """IDs of all ACTIVE connected accounts in this Composio workspace."""
    url = f"{COMPOSIO_BASE}/connected_accounts"
    r = requests.get(url, headers={"X-API-Key": COMPOSIO_API_KEY}, timeout=30)
    data = r.json()
    items = data.get("items", data if isinstance(data, list) else [])
    return [it.get("id") for it in items if it.get("status") == "ACTIVE" and it.get("id")]


_MAILBOX_CACHE: list = []


def discover_mailboxes(refresh: bool = False) -> list:
    """All connected Gmail mailboxes: [{id, email}]. Cached unless refresh."""
    if _MAILBOX_CACHE and not refresh:
        return _MAILBOX_CACHE
    boxes = []
    for aid in list_connected_accounts():
        email = aid
        try:
            d = _execute_tool("GMAIL_GET_PROFILE", {}, account_id=aid)
            rd = d.get("response_data", d)
            email = rd.get("emailAddress", aid)
        except Exception:
            pass  # keep raw id if profile fails - mailbox still usable
        boxes.append({"id": aid, "email": email})
    _MAILBOX_CACHE[:] = boxes
    return boxes


def create_connection_link() -> str:
    """Fresh OAuth link to connect ANOTHER Gmail mailbox (10-min expiry)."""
    url = f"{COMPOSIO_BASE}/connected_accounts/link"
    r = requests.post(
        url,
        json={"auth_config_id": AUTH_CONFIG_ID, "user_id": COMPOSIO_USER_ID},
        headers={"X-API-Key": COMPOSIO_API_KEY},
        timeout=30,
    )
    d = r.json()
    return d.get("redirect_url") or d.get("connectionLink") or ""


def _execute_tool(tool_slug: str, arguments: dict, account_id: str = "") -> dict:
    """Execute a Composio tool. Returns the tool's data dict. Raises on failure.
    NOTE (verified live): results live under data['messages'] / data directly,
    NOT under data['response_data']."""
    url = f"{COMPOSIO_BASE}/tools/execute/{tool_slug}"
    payload = {
        "connected_account_id": account_id or CONNECTED_ACCOUNT_ID,
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


def get_profile(account_id: str = "") -> str:
    """One-line mailbox stats: address + counts."""
    d = _execute_tool("GMAIL_GET_PROFILE", {}, account_id=account_id)
    rd = d.get("response_data", d)  # profile uses response_data, fall back to d
    return (
        f"📧 {rd.get('emailAddress', '?')} — "
        f"{rd.get('messagesTotal', '?')} messages, "
        f"{rd.get('threadsTotal', '?')} threads"
    )


def find_big_newsletters(limit: int = 25, account_id: str = "") -> list:
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
        d = _execute_tool("GMAIL_FETCH_EMAILS", args, account_id=account_id)
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


def read_messages(count: int = 5, query: str = "", account_id: str = "") -> list:
    """Read the latest messages (or search results) with text preview."""
    args = {"max_results": count, "verbose": True, "include_payload": True}
    if query:
        args["query"] = query
    d = _execute_tool("GMAIL_FETCH_EMAILS", args, account_id=account_id)
    msgs = d.get("messages", []) if isinstance(d, dict) else []
    out = []
    for m in msgs:
        labels = m.get("labelIds", []) or []
        if "CHAT" in labels or "TRASH" in labels:
            continue
        body = (m.get("preview", {}) or {}).get("body", "") or ""
        out.append(
            {
                "id": m.get("messageId", ""),
                "thread": m.get("threadId", ""),
                "from": (m.get("sender") or "?")[:60],
                "subject": (m.get("subject") or "(no subject)")[:80],
                "date": (m.get("messageTimestamp") or "")[:16],
                "body": body[:400],
            }
        )
    return out


def create_draft(to: str, subject: str, body: str, thread_id: str = "", account_id: str = "") -> str:
    """Create a Gmail draft (NEVER sends). Verified params: recipient_email,
    subject, body. Returns confirmation string."""
    args = {
        "recipient_email": extract_email_address(to),
        "subject": subject,
        "body": body,
    }
    if thread_id:
        args["thread_id"] = thread_id
    d = _execute_tool("GMAIL_CREATE_EMAIL_DRAFT", args, account_id=account_id)
    return "Draft created in your Gmail Drafts folder (nothing sent - YOU press send)."


def trash_message(message_id: str, account_id: str = "") -> bool:
    """Move ONE message to trash (recoverable 30 days). Returns True on success."""
    d = _execute_tool("GMAIL_MOVE_TO_TRASH", {"message_id": message_id}, account_id=account_id)
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
        "Reply with numbers to trash, e.g.: 1 3 5  — or /gmail scan again.\n"
        "Multiple mailboxes? /mailboxes lists and switches them."
    )
    return "\n".join(lines[:40])
