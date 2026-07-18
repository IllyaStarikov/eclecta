"""Reddit fetcher with three modes (config: ingest.reddit_mode):

  public_json — unauthenticated /r/<sub>/top.json (~10 QPM IP limit; the
                PoliteClient enforces a 7s per-host interval). Default.
  rss         — /r/<sub>/top/.rss via feedparser; loses score/comments but
                immune to JSON endpoint enforcement.
  oauth       — registered script app (100 QPM). Not implemented yet; the
                error message documents the upgrade path.
"""

from __future__ import annotations

import datetime
import json
from typing import List

from .fetch_http import PoliteClient


def _sub_from_source(source_row) -> str:
    # slug convention: reddit-<sub>
    slug = source_row["slug"]
    return slug.split("reddit-", 1)[1] if "reddit-" in slug else slug


def fetch_items(client: PoliteClient, source_row, mode: str = "public_json") -> List[dict]:
    mode = (source_row["mode"] or mode or "public_json").lower()
    if mode == "oauth":
        raise RuntimeError(
            "reddit oauth mode not configured: register a script app at "
            "reddit.com/prefs/apps, then store creds and switch "
            "ingest.reddit_mode. Falling back is manual by design."
        )
    if mode == "rss":
        from . import rss as rss_mod

        # Reuse the generic feed path against the .rss endpoint.
        sub = _sub_from_source(source_row)
        row = dict(source_row)
        row["url"] = "https://www.reddit.com/r/%s/top/.rss?t=day" % sub
        # Drop the "/u/<user>" byline the .rss feed carries (see public_json).
        return [{**it, "author": None} for it in rss_mod.fetch_feed_items(client, row)]

    # public_json
    res = client.fetch(source_row["url"], conditional=False)
    if res.status != 200 or not res.content:
        raise RuntimeError(res.error or ("HTTP %s" % res.status))
    data = json.loads(res.content)
    items = []
    for child in (data.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        name = d.get("name")  # t3_xxxxx
        title = (d.get("title") or "").strip()
        if not name or not title:
            continue
        permalink = "https://www.reddit.com%s" % d.get("permalink", "")
        url = permalink if d.get("is_self") else (d.get("url") or permalink)
        created = d.get("created_utc")
        published = (
            datetime.datetime.fromtimestamp(
                created, datetime.timezone.utc
            ).isoformat()
            if created
            else None
        )
        items.append(
            {
                "guid": name,
                "raw_url": url,
                "title": title,
                # PII minimization (GDPR): the Reddit poster's username is social
                # UGC that is never displayed or used for signal — drop it.
                "author": None,
                "published_at": published,
                "points": d.get("score"),
                "comments": d.get("num_comments"),
                "extra": {
                    "discussion_url": permalink,
                    "subreddit": d.get("subreddit"),
                    "upvote_ratio": d.get("upvote_ratio"),
                    "surface": "reddit",
                },
            }
        )
    return items
