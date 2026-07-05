"""GDELT 2.0 DOC API (artlist mode).

GDELT is quirky: it rate-limits hard (one request per 5s — we pin the host
interval to 6s), answers 429s/HTML error strings with a 200-shaped face, and
occasionally emits malformed JSON. A non-JSON 200 degrades to zero items with
a stderr warning (never an exception); we only raise when EVERY query fails at
the HTTP layer, so error_count still increments on a real outage.

Each registry source row carries its full query URL (built by registry
expand() from ingest.gdelt_queries); a bare endpoint URL falls back to
fetching all configured queries.
"""

from __future__ import annotations

import datetime
import json
import sys
from typing import List, Optional
from urllib.parse import quote, urlsplit

from .fetch_http import PoliteClient

DOC_API = (
    "https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
    "&mode=artlist&format=json&maxrecords=50&timespan=1d"
)
DEFAULT_QUERIES = [
    "(artificial intelligence OR machine learning) sourcelang:eng",
    "(quantum computing OR semiconductor) sourcelang:eng",
]


def query_url(query: str) -> str:
    return DOC_API % quote(query, safe="")


def _seendate_iso(value) -> Optional[str]:
    # artlist seendate: "20260610T083000Z"
    try:
        return (
            datetime.datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=datetime.timezone.utc)
            .isoformat()
        )
    except (TypeError, ValueError):
        return None


def _urls_for_source(source_row, queries) -> List[str]:
    url = source_row["url"] or ""
    if "api.gdeltproject.org" in url and "query=" in urlsplit(url).query:
        return [url]
    return [query_url(q) for q in (queries or DEFAULT_QUERIES)]


def fetch_items(client: PoliteClient, source_row, queries=None) -> List[dict]:
    # GDELT's published etiquette: one request per 5 seconds.
    client.host_intervals.setdefault("api.gdeltproject.org", 6.0)
    by_guid = {}
    http_errors = []
    warnings = []
    urls = _urls_for_source(source_row, queries)
    for url in urls:
        res = client.fetch(url, conditional=False)
        if res.status != 200 or not res.content:
            http_errors.append(res.error or ("HTTP %s" % res.status))
            continue
        try:
            data = json.loads(res.content)
        except ValueError:
            # 429 throttle messages and HTML errors arrive as plain text.
            snippet = res.content[:80].decode("utf-8", "ignore").strip()
            warnings.append("non-JSON payload (%s)" % snippet)
            continue
        articles = data.get("articles") if isinstance(data, dict) else None
        if not isinstance(articles, list):
            warnings.append("no articles list in payload")
            continue
        for art in articles:
            if not isinstance(art, dict):
                continue
            art_url = (art.get("url") or "").strip()
            title = (art.get("title") or "").strip()
            if not art_url or not title:
                continue
            guid = "gdelt-%s" % art_url
            if guid in by_guid:
                continue  # same article matched by several queries
            by_guid[guid] = {
                "guid": guid,
                "raw_url": art_url,
                "title": title,
                "author": None,
                "published_at": _seendate_iso(art.get("seendate")),
                "points": None,
                "comments": None,
                "extra": {
                    "surface": "gdelt",
                    "domain": art.get("domain"),
                    "sourcecountry": art.get("sourcecountry"),
                    "language": art.get("language"),
                },
            }
    if http_errors and len(http_errors) == len(urls):
        raise RuntimeError("gdelt: all queries failed: %s" % "; ".join(http_errors))
    for msg in http_errors + warnings:
        print("gdelt: query degraded (%s)" % msg, file=sys.stderr)
    return list(by_guid.values())
