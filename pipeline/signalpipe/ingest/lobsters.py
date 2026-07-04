"""Lobsters via the JSON API: /hottest.json (+pages). `url` is the article,
`comments_url` the discussion — both explicit, no parsing needed."""

from __future__ import annotations

import json
from typing import List

from .fetch_http import PoliteClient

HOTTEST = "https://lobste.rs/hottest.json?page=%d"


def fetch_items(client: PoliteClient, source_row, pages: int = 2) -> List[dict]:
    items = []
    for page in range(1, max(1, pages) + 1):
        res = client.fetch(HOTTEST % page, conditional=False)
        if res.status != 200 or not res.content:
            raise RuntimeError(res.error or ("HTTP %s" % res.status))
        for story in json.loads(res.content):
            short_id = story.get("short_id")
            title = (story.get("title") or "").strip()
            if not short_id or not title:
                continue
            comments_url = story.get("comments_url")
            url = story.get("url") or comments_url
            items.append(
                {
                    "guid": "lob-%s" % short_id,
                    "raw_url": url,
                    "title": title,
                    "author": (story.get("submitter_user") or ""),
                    "published_at": story.get("created_at"),
                    "points": story.get("score"),
                    "comments": story.get("comment_count"),
                    "extra": {
                        "discussion_url": comments_url,
                        "tags": story.get("tags", []),
                        "surface": "lobsters",
                    },
                }
            )
    return items
