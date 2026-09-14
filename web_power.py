#!/usr/bin/env python3
"""
Web-search powers for LAFB_Bot - FREE, no API key needed.

Design (honest):
- Google's AI search grounding requires a PAID plan (verified live: 429
  quota error on the free key). We do NOT pretend to have full-web search.
- Instead we use Google News RSS (free, no key, verified working) to pull
  REAL, current headlines on any topic, then Gemini summarizes them.
- Honest limits: this searches NEWS/headlines, not deep web pages.

Verified live (Sep 2026):
- https://news.google.com/rss/search?q=<query> returns current headlines
- DuckDuckGo Instant Answer API works but is thin; kept as a small bonus.
"""

import re
import html
import logging
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}


def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)  # strip any HTML tags
    return re.sub(r"\s+", " ", text).strip()


def search_news(query: str, limit: int = 8) -> list:
    """Fetch current news headlines for a query via Google News RSS.
    Returns list of {title, source, date, link}. Empty list = no results."""
    q = requests.utils.quote(query.strip()[:120])
    url = (
        "https://news.google.com/rss/search?"
        f"q={q}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"RSS fetch error: {e}")
        return []

    items = []
    try:
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            title = _clean(item.findtext("title", ""))
            if not title or title == "Google News":
                continue
            link = (item.findtext("link") or "").strip()
            date = (item.findtext("pubDate") or "").strip()
            src_m = re.search(r"([^-]+)$", title)  # RSS titles end with " - Source"
            source = _clean(src_m.group(1)) if src_m else ""
            title_main = _clean(re.sub(r"\s*-\s*[^-]+$", "", title))
            items.append(
                {
                    "title": title_main[:110],
                    "source": source[:40],
                    "date": date[:16],
                    "link": link,
                }
            )
            if len(items) >= limit:
                break
    except ET.ParseError as e:
        logger.error(f"RSS parse error: {e}")
        return []
    return items


def format_results(query: str, items: list) -> str:
    if not items:
        return (
            f"\U0001F50D No fresh news found for \"{query}\".\n"
            "Try different words, or note my honest limit: I search NEWS "
            "headlines, not the whole web."
        )
    lines = [f"\U0001F50D Fresh news on \u201C{query}\u201D ({len(items)} found):", ""]
    for i, it in enumerate(items, 1):
        src = f" \u2014 {it['source']}" if it["source"] else ""
        lines.append(f"{i}. {it['title']}{src}")
        if it["date"]:
            lines.append(f"   \U0001F4C5 {it['date']}")
    lines.append("")
    lines.append("\U0001F4A1 Send \u201Cread <number>\u201D for more on one story, or ask me to explain any of them.")
    return "\n".join(lines)


def duck_bonus(query: str) -> str:
    """DuckDuckGo Instant Answer - only sometimes has content. Bonus only."""
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1},
            headers=HEADERS,
            timeout=10,
        )
        d = r.json()
        abstract = (d.get("Abstract") or "").strip()
        if abstract:
            src = d.get("AbstractSource") or "DuckDuckGo"
            return f"\U0001F4D8 Quick fact ({src}): {abstract[:400]}"
    except Exception:
        pass
    return ""
