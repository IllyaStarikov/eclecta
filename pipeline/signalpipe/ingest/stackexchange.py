"""Stack Overflow hot questions via the Stack Exchange 2.3 API (anon).

Anonymous quota is 300 requests/day per IP; we spend a handful. The API may
attach a `backoff` field (seconds) to any response — honor it by logging and
returning early with whatever this response carried (we never issue a second
request in the same fetch anyway). Titles arrive HTML-escaped.
"""

from __future__ import annotations

import datetime
import html as html_mod
import json
import sys
from typing import List

from .fetch_http import PoliteClient

HOT_URL = (
    "https://api.stackexchange.com/2.3/questions"
    "?order=desc&sort=hot&site=stackoverflow&pagesize=50"
)


def fetch_items(client: PoliteClient, source_row) -> List[dict]:
    res = client.fetch(HOT_URL, conditional=False)
    if res.status != 200 or not res.content:
        raise RuntimeError(res.error or ("HTTP %s" % res.status))
    data = json.loads(res.content)
    backoff = data.get("backoff")
    if backoff:
        print(
            "stackexchange: API requested backoff of %ss (quota %s/%s) — "
            "parsing this response and stopping"
            % (backoff, data.get("quota_remaining"), data.get("quota_max")),
            file=sys.stderr,
        )
    items = []
    for q in data.get("items", []):
        qid = q.get("question_id")
        title = html_mod.unescape((q.get("title") or "").strip())
        link = q.get("link")
        if not qid or not title or not link:
            continue
        created = q.get("creation_date")
        published = (
            datetime.datetime.fromtimestamp(
                created, datetime.timezone.utc
            ).isoformat()
            if created
            else None
        )
        items.append(
            {
                "guid": "so-%s" % qid,
                "raw_url": link,
                "title": title,
                "author": (q.get("owner") or {}).get("display_name"),
                "published_at": published,
                "points": q.get("score"),
                "comments": q.get("answer_count"),
                "extra": {
                    "surface": "stackexchange",
                    "site": "stackoverflow",
                    "answer_count": q.get("answer_count"),
                    "view_count": q.get("view_count"),
                    "is_answered": q.get("is_answered"),
                    "tags": q.get("tags", []),
                },
            }
        )
    return items
