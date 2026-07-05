"""dev.to top articles via the public Forem API (no auth).

/api/articles?top=1 = highest-reacted articles of the last day. Reactions
map to points, comments_count to comments. tag_list is usually a list but
the Forem API has historically also shipped it as a comma string — handle
both.
"""

from __future__ import annotations

import json
from typing import List

from .fetch_http import PoliteClient

ARTICLES_URL = "https://dev.to/api/articles?top=1&per_page=50"


def _tags(article) -> List[str]:
    tags = article.get("tag_list") or article.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return list(tags)


def fetch_items(client: PoliteClient, source_row) -> List[dict]:
    res = client.fetch(ARTICLES_URL, conditional=False)
    if res.status != 200 or not res.content:
        raise RuntimeError(res.error or ("HTTP %s" % res.status))
    items = []
    for article in json.loads(res.content):
        aid = article.get("id")
        title = (article.get("title") or "").strip()
        url = article.get("url")
        if not aid or not title or not url:
            continue
        items.append(
            {
                "guid": "devto-%s" % aid,
                "raw_url": url,
                "title": title,
                "author": (article.get("user") or {}).get("username"),
                "published_at": article.get("published_timestamp")
                or article.get("published_at"),
                "points": article.get("positive_reactions_count"),
                "comments": article.get("comments_count"),
                "extra": {
                    "surface": "devto",
                    "tags": _tags(article),
                    "reading_time_minutes": article.get("reading_time_minutes"),
                },
            }
        )
    return items
