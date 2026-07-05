"""Hacker News via the Algolia API.

`search?tags=front_page` returns the ranked front page with points,
num_comments, url, objectID, created_at_i in ONE call (~10k req/hr budget —
we use a handful per cycle). Discussion URL = news.ycombinator.com/item?id=N.
Self-posts (Ask/Show HN without url) use the discussion URL as raw_url.
"""

from __future__ import annotations

import datetime
import json
from typing import List

from .fetch_http import PoliteClient

ALGOLIA = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50&page=%d"
ITEM_URL = "https://news.ycombinator.com/item?id=%s"


def fetch_items(client: PoliteClient, source_row, pages: int = 2) -> List[dict]:
    items = []
    for page in range(max(1, pages)):
        res = client.fetch(ALGOLIA % page, conditional=False)
        if res.status != 200 or not res.content:
            raise RuntimeError(res.error or ("HTTP %s" % res.status))
        data = json.loads(res.content)
        for hit in data.get("hits", []):
            object_id = hit.get("objectID")
            title = (hit.get("title") or "").strip()
            if not object_id or not title:
                continue
            discussion = ITEM_URL % object_id
            url = hit.get("url") or discussion
            created = hit.get("created_at_i")
            published = (
                datetime.datetime.fromtimestamp(
                    created, datetime.timezone.utc
                ).isoformat()
                if created
                else None
            )
            items.append(
                {
                    "guid": "hn-%s" % object_id,
                    "raw_url": url,
                    "title": title,
                    "author": hit.get("author"),
                    "published_at": published,
                    "points": hit.get("points"),
                    "comments": hit.get("num_comments"),
                    "extra": {"discussion_url": discussion, "surface": "hn"},
                }
            )
    return items
