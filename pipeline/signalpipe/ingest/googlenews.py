"""Google News RSS (topic sections + query feeds).

Entry links are news.google.com/rss/articles/<id> redirect wrappers, NOT the
articles. Two-stage resolution for the top N entries per feed:

  1. offline — legacy article ids base64-embed the target URL in a protobuf
     blob; decode costs nothing and never hits the network.
  2. network — client.resolve() follows HTTP redirects. The 2026-era id
     format (AU_yq...) returns a 200 JS splash instead of a 302, so after a
     few consecutive network misses we stop trying for the rest of the feed.

Unresolved entries keep the google URL: news.google.com is in
AGGREGATOR_HOSTS, so title-key clustering catches them. Titles carry a
" - Publisher" suffix that would poison the title key — it's stripped when
it matches the entry's <source> tag (kept in extra/author).
"""

from __future__ import annotations

import base64
import binascii
import re
import sys
from typing import List, Optional
from urllib.parse import urlsplit

from .fetch_http import PoliteClient
from .rss import _entry_time

GOOGLE_HOSTS = ("news.google.com", "google.com", "www.google.com")
_URL_IN_BLOB_RE = re.compile(rb"https?://[\x21-\x7e]+")

# Consecutive network-resolve misses before we conclude the feed is all
# new-format ids (no HTTP redirect) and stop burning rate-limit budget.
RESOLVE_GIVE_UP_AFTER = 5


def _decode_embedded_url(link: str) -> Optional[str]:
    """Offline decode of legacy article ids (URL embedded in the base64)."""
    path = urlsplit(link).path
    if "/articles/" not in path:
        return None
    seg = path.rsplit("/", 1)[-1]
    try:
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
    except (binascii.Error, ValueError):
        return None
    m = _URL_IN_BLOB_RE.search(raw)
    if not m:
        return None
    url = m.group(0).decode("ascii")
    host = (urlsplit(url).hostname or "").lower()
    if not host or host in GOOGLE_HOSTS:
        return None
    return url


def _is_google(url: Optional[str]) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return host.endswith("google.com")


def fetch_items(client: PoliteClient, source_row, resolve_top: int = 25) -> List[dict]:
    import feedparser

    res = client.fetch(source_row["url"])
    if res.status == 304 or res.unchanged:
        return []
    if res.status != 200 or not res.content:
        raise RuntimeError(res.error or ("HTTP %s" % res.status))

    parsed = feedparser.parse(res.content)
    items = []
    misses = 0
    for idx, e in enumerate(parsed.get("entries", [])[:100]):
        link = e.get("link")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        publisher = ((e.get("source") or {}).get("title") or "").strip()
        if publisher and title.endswith(" - %s" % publisher):
            title = title[: -len(" - %s" % publisher)].rstrip()

        resolved = _decode_embedded_url(link)
        if resolved is None and idx < resolve_top and misses < RESOLVE_GIVE_UP_AFTER:
            final = client.resolve(link)
            if final and not _is_google(final):
                resolved = final
                misses = 0
            else:
                misses += 1
                if misses == RESOLVE_GIVE_UP_AFTER:
                    print(
                        "gnews: %d consecutive unresolvable links on %s — "
                        "keeping google URLs for the rest"
                        % (misses, source_row["slug"]),
                        file=sys.stderr,
                    )

        extra = {"surface": "google-news", "publisher": publisher or None}
        if resolved:
            extra["gnews_url"] = link
        items.append(
            {
                "guid": "gnews-%s" % (e.get("id") or link),
                "raw_url": resolved or link,
                "title": title,
                "author": publisher or None,
                "published_at": _entry_time(e),
                "points": None,
                "comments": None,
                "extra": extra,
            }
        )
    return items
