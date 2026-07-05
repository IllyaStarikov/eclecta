"""Bluesky trending topics via the public AppView (no auth).

app.bsky.unspecced.getTrendingTopics is UNSPECCED — Bluesky reserves the
right to change or drop it without notice, so every access is defensive:
any schema surprise degrades to zero items with a stderr warning, never an
exception. Topics are phrases, not links; raw_url is a bsky.app search
self-link (extra marks it as an aggregator self-link so downstream knows
there is no article behind it).
"""

from __future__ import annotations

import json
import sys
from typing import List
from urllib.parse import quote

from .fetch_http import PoliteClient

TRENDS_URL = (
    "https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics"
)
SEARCH_URL = "https://bsky.app/search?q=%s"


def fetch_items(client: PoliteClient, source_row) -> List[dict]:
    res = client.fetch(TRENDS_URL, conditional=False)
    if res.status != 200 or not res.content:
        print(
            "bsky: trends fetch failed (%s)" % (res.error or ("HTTP %s" % res.status)),
            file=sys.stderr,
        )
        return []
    try:
        data = json.loads(res.content)
        topics = data.get("topics")
    except (ValueError, AttributeError) as e:
        print("bsky: unexpected payload (%s)" % e, file=sys.stderr)
        return []
    if not isinstance(topics, list):
        print("bsky: no topics list in response", file=sys.stderr)
        return []

    items = []
    for t in topics:
        if not isinstance(t, dict):
            continue
        topic = (t.get("topic") or "").strip()
        title = (t.get("displayName") or topic).strip()
        if not topic or not title:
            continue
        link = t.get("link") or ""
        items.append(
            {
                "guid": "bsky-%s" % topic.lower(),
                "raw_url": SEARCH_URL % quote(topic),
                "title": title,
                "author": None,
                "published_at": None,
                "points": None,
                "comments": None,
                "extra": {
                    "surface": "bluesky",
                    # Self-link to a search page, not an article — clustering
                    # must lean on the title key, not the URL.
                    "aggregator_self_link": True,
                    "feed_link": ("https://bsky.app%s" % link) if link else None,
                },
            }
        )
    return items
