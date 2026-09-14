#!/usr/bin/env python3
"""
Amazon powers for LAFB_Bot - via the Gmail connection (verified tools only).

Design (honest, safe):
- We do NOT connect to Amazon Seller Central (SP-API needs Amazon developer
  approval - verified live: Composio has 0 such tools).
- Instead: Amazon already EMAILS every account event (orders, payments,
  inventory, FBA, policy alerts). We read those emails through the existing
  Gmail connection and turn them into a clean Telegram digest.
- Read-only. We never delete, never send, never touch the seller account.
"""

import re
import logging
from datetime import datetime, timedelta

import gmail_power

logger = logging.getLogger(__name__)

# --- senders that count as "Amazon account emails" -------------------------
AMAZON_SENDER_RE = re.compile(
    r"(amazon\.[a-z.]+|amazon\.com|@amazon\.|sellercentral|amazonfba|"
    r"payments amazon|disbursement|no-reply@marketplace)",
    re.IGNORECASE,
)

# Classify by sender + subject keywords
URGENT_PATTERNS = re.compile(
    r"(low inventory|out of stock|removal order|stranded|suppressed|"
    r"suspended|violation|policy notice|action required|payment.*failed|"
    r"disbursement.*(delay|hold)|case (opened|closed)|performance notice|"
    r"account (review|deactivat|health))",
    re.IGNORECASE,
)

CATEGORY_RULES = [
    ("payments", re.compile(r"(payment|disbursement|invoice|settlement|balance|statement|charge)", re.I)),
    ("inventory", re.compile(r"(inventory|restock|replenish|stock|removal order|stranded)", re.I)),
    ("fba", re.compile(r"(fba|fulfillment|inbound|shipment (created|received)|receiving)", re.I)),
    ("orders", re.compile(r"(order|sold|shipment|refund|return|claim)", re.I)),
    ("advertising", re.compile(r"(advertis|sponsored|campaign|ppc|ad console)", re.I)),
    ("alerts", re.compile(r"(alert|notice|suspend|violation|performance|health|review|required)", re.I)),
    ("news", re.compile(r"(newsletter|tips|best practice|webinar|update|news)", re.I)),
]

URGENT_EMOJI = "\U0001F6A8"  # 🚨
CAT_EMOJI = {
    "payments": "\U0001F4B0",   # 💰
    "inventory": "\U0001F4E6",  # 📦
    "orders": "\U0001F6D2",     # 🛒
    "fba": "\U0001F69B",        # 🚛
    "advertising": "\U0001F4E3",# 📣
    "alerts": "\u26A0\uFE0F",   # ⚠️
    "news": "\U0001F4F0",       # 📰
    "other": "\U0001F4E7",      # 📧
}


def classify(subject: str, sender: str) -> str:
    text = f"{subject} {sender}"
    for cat, rx in CATEGORY_RULES:
        if rx.search(text):
            return cat
    return "other"


def is_amazon(sender: str, subject: str = "") -> bool:
    return bool(AMAZON_SENDER_RE.search(sender) or "amazon" in (subject or "").lower())


def fetch_amazon_emails(days: int = 2, max_scan: int = 150, account_id: str = "") -> list:
    """Fetch recent emails and keep Amazon ones. We scan broadly (verified:
    query filters return empty on this connection) and filter locally."""
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    all_items = []
    seen_ids = set()
    page_token = None
    for _ in range(3):  # up to 3 pages like the cleanup scan
        args = {"max_results": 50, "verbose": True, "include_payload": False}
        if page_token:
            args["page_token"] = page_token
        d = gmail_power._execute_tool("GMAIL_FETCH_EMAILS", args, account_id=account_id)
        msgs = d.get("messages", []) if isinstance(d, dict) else []
        for m in msgs:
            mid = m.get("messageId", "")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            labels = m.get("labelIds", []) or []
            if "CHAT" in labels or "TRASH" in labels:
                continue
            sender = m.get("sender") or ""
            subject = m.get("subject") or "(no subject)"
            ts = (m.get("messageTimestamp") or "")[:16]
            if ts and ts[:10] < since:  # older than our window
                continue
            if is_amazon(sender, subject):
                all_items.append(
                    {
                        "id": mid,
                        "from": sender[:50],
                        "subject": subject[:90],
                        "date": ts,
                        "cat": classify(subject, sender),
                        "urgent": bool(URGENT_PATTERNS.search(subject)),
                    }
                )
        page_token = d.get("nextPageToken") if isinstance(d, dict) else None
        if not page_token or not msgs or len(seen_ids) >= max_scan:
            break
    return all_items


def build_digest(items: list) -> str:
    """Turn Amazon emails into a compact Telegram digest."""
    if not items:
        return (
            "📦 Amazon check: no Amazon emails in the last couple of days.\n"
            "Either all quiet, or Amazon mail lands in a different mailbox "
            "(\u201C/mailboxes\u201D to check others)."
        )
    urgent = [i for i in items if i["urgent"]]
    cats = {}
    for it in items:
        cats.setdefault(it["cat"], []).append(it)

    lines = []
    if urgent:
        lines.append(f"{URGENT_EMOJI} NEEDS ATTENTION ({len(urgent)}):")
        for it in urgent[:5]:
            lines.append(f"  \u2022 {it['subject']}")
        lines.append("")
    lines.append(f"🛒 Amazon activity - last days ({len(items)} emails):")
    for cat in ["orders", "payments", "inventory", "fba", "advertising", "alerts", "news", "other"]:
        bucket = cats.get(cat, [])
        if not bucket:
            continue
        lines.append(f"\n{CAT_EMOJI[cat]} {cat.upper()} ({len(bucket)}):")
        for it in bucket[:4]:
            lines.append(f"  \u2022 {it['subject'][:70]}")
        if len(bucket) > 4:
            lines.append(f"  \u2026 and {len(bucket) - 4} more")
    lines.append("\n\U0001F4A1 Reply \u201Camazon read <words from a subject>\u201D and I\u2019ll show the full email.")
    return "\n".join(lines)


def find_email_by_words(items: list, words: str) -> dict:
    """Match a digest line back to its email by keywords."""
    words_l = words.lower().strip()
    best, best_score = None, 0
    for it in items:
        hay = (it["subject"] + " " + it["from"]).lower()
        score = sum(1 for w in words_l.split() if w in hay)
        if score > best_score:
            best, best_score = it, score
    return best if best_score >= 1 else (items[0] if items and not words_l else None)


def read_email_body(item: dict, account_id: str = "") -> str:
    """Read ONE Amazon email's body via the existing read_messages query path."""
    msg_id = item.get("id", "")
    if not msg_id:
        return "I could not open that email."
    d = gmail_power._execute_tool(
        "GMAIL_FETCH_EMAILS",
        {"message_ids": [msg_id], "verbose": True, "include_payload": True},
        account_id=account_id,
    ) if hasattr(gmail_power, "_execute_tool") else {}
    msgs = d.get("messages", []) if isinstance(d, dict) else []
    if msgs:
        body = (msgs[0].get("preview", {}) or {}).get("body", "") or ""
        return body[:2500] if body else "(Email has no readable text - likely an image-only email.)"
    # Fallback: search by subject
    found = gmail_power.read_messages(5, query=item["subject"][:60], account_id=account_id)
    if found:
        return (found[0].get("body") or "")[:2500] or "(No readable text in that email.)"
    return "I could not find that email anymore."
