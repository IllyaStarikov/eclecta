"""Generic RSS/Atom fetcher (the workhorse for ~95% of sources).

Fetch with PoliteClient (conditional GET + body-hash short-circuit), parse
with feedparser. `bozo` is a warning, not a failure — most malformed feeds
still yield usable entries.
"""

from __future__ import annotations

import calendar
import datetime
from typing import List, Optional

from .fetch_http import PoliteClient


def _entry_time(entry) -> Optional[str]:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            calendar.timegm(t), datetime.timezone.utc
        ).isoformat()
    except (ValueError, OverflowError):
        return None


def fetch_feed_items(client: PoliteClient, source_row) -> List[dict]:
    """Returns normalized raw-item dicts (see ingest/__init__.py)."""
    import feedparser

    res = client.fetch(source_row["url"])
    if res.status == 304 or res.unchanged:
        return []
    if res.status != 200 or not res.content:
        raise RuntimeError(res.error or ("HTTP %s" % res.status))

    parsed = feedparser.parse(res.content)
    if parsed.get("bozo"):
        # Log-worthy but non-fatal; caller records it as a warning.
        pass

    items = []
    for e in parsed.get("entries", [])[:100]:
        link = e.get("link")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        guid = e.get("id") or link
        author = None
        if e.get("author"):
            author = e.get("author")
        items.append(
            {
                "guid": guid,
                "raw_url": link,
                "title": title,
                "author": author,
                "published_at": _entry_time(e),
                "points": None,
                "comments": None,
                "extra": {"bozo": bool(parsed.get("bozo"))},
            }
        )
    return items
