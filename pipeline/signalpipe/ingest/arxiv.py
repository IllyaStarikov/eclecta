"""arXiv per-category feeds.

arXiv etiquette: 1 request / 3 seconds, single connection — the PoliteClient
pins rss.arxiv.org/export.arxiv.org to a 3.5s interval, and the pipeline runs
sources sequentially, which together satisfy the policy. arXiv links are
primary sources, so they double as paywall-free read URLs downstream.
"""

from __future__ import annotations

import re
from typing import List

from . import rss as rss_mod
from .fetch_http import PoliteClient

# arXiv RSS announces replacements with a title suffix like
# "(arXiv:2501.12345v2 [cs.AI] UPDATED)" — anchor on that exact shape so a
# paper legitimately containing the word UPDATED is never dropped.
_UPDATED_RE = re.compile(r"\(arXiv:\S+(?:\s+\[[^\]]+\])?\s+UPDATED\)\s*$")


def fetch_items(client: PoliteClient, source_row) -> List[dict]:
    items = rss_mod.fetch_feed_items(client, source_row)
    for it in items:
        it["extra"]["surface"] = "arxiv"
    return [i for i in items if not _UPDATED_RE.search(i["title"])]
