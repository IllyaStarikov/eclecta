"""Mastodon trending links (public, no auth).

GET /api/v1/trends/links returns the instance's trending EXTERNAL urls —
the real clustering signal (actual articles, not toots). `history[0]` is
today's bucket; its `uses` count (a string!) becomes points. One source row
covers every configured instance; the same URL trending on several instances
dedupes by guid, keeping the highest use count.
"""

from __future__ import annotations

import json
import sys
from typing import List

from .fetch_http import PoliteClient

TRENDS_URL = "https://%s/api/v1/trends/links?limit=20"


def _to_int(value):
    """Mastodon history counts arrive as strings; tolerate junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_items(client: PoliteClient, source_row, instances=None) -> List[dict]:
    instances = list(instances or ["mastodon.social"])
    by_guid = {}
    errors = []
    for instance in instances:
        res = client.fetch(TRENDS_URL % instance, conditional=False)
        if res.status != 200 or not res.content:
            errors.append("%s: %s" % (instance, res.error or ("HTTP %s" % res.status)))
            continue
        try:
            entries = json.loads(res.content)
        except ValueError as e:
            errors.append("%s: bad JSON (%s)" % (instance, e))
            continue
        if not isinstance(entries, list):
            errors.append("%s: unexpected payload shape" % instance)
            continue
        for entry in entries:
            url = (entry.get("url") or "").strip()
            title = (entry.get("title") or "").strip()
            if not url or not title:
                continue
            history = entry.get("history") or [{}]
            uses = _to_int(history[0].get("uses"))
            accounts = _to_int(history[0].get("accounts"))
            item = {
                # The trending URL is the identity (spec: guid = the URL).
                "guid": url,
                "raw_url": url,
                "title": title,
                "author": entry.get("author_name") or None,
                "published_at": entry.get("published_at"),
                "points": uses,
                "comments": None,
                "extra": {
                    "surface": "mastodon",
                    "instance": instance,
                    "provider": entry.get("provider_name"),
                    "accounts": accounts,
                },
            }
            prev = by_guid.get(url)
            if prev is None or (uses or 0) > (prev.get("points") or 0):
                by_guid[url] = item
    if errors and not by_guid:
        # Every instance failed: surface it so error_count increments.
        raise RuntimeError("all mastodon instances failed: %s" % "; ".join(errors))
    for err in errors:
        print("mastodon: instance failed (%s)" % err, file=sys.stderr)
    return list(by_guid.values())
