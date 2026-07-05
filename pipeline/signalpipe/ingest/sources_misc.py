"""API/scrape fetchers without a dedicated module: Hugging Face daily papers
and GitHub Trending. (Techmeme is a plain RSS source; Product Hunt is
config-gated off by default — needs an OAuth token.)"""

from __future__ import annotations

import json
import re
from typing import List

from .fetch_http import PoliteClient

HF_DAILY = "https://huggingface.co/api/daily_papers"
GH_TRENDING = "https://github.com/trending?since=daily"

_GH_STARS_TODAY_RE = re.compile(r"([\d,]+)\s+stars?\s+today")


def fetch_hf_daily_papers(client: PoliteClient, source_row) -> List[dict]:
    res = client.fetch(HF_DAILY, conditional=False)
    if res.status != 200 or not res.content:
        raise RuntimeError(res.error or ("HTTP %s" % res.status))
    items = []
    for row in json.loads(res.content)[:60]:
        paper = row.get("paper") or {}
        pid = paper.get("id")
        title = (paper.get("title") or "").strip().replace("\n", " ")
        if not pid or not title:
            continue
        items.append(
            {
                "guid": "hf-%s" % pid,
                # arXiv abstract = the primary source; HF page = commentary.
                "raw_url": "https://arxiv.org/abs/%s" % pid,
                "title": title,
                "author": None,
                "published_at": row.get("publishedAt"),
                "points": paper.get("upvotes"),
                "comments": None,
                "extra": {
                    "discussion_url": "https://huggingface.co/papers/%s" % pid,
                    "surface": "hf-papers",
                },
            }
        )
    return items


def fetch_github_trending(client: PoliteClient, source_row) -> List[dict]:
    """Scrape github.com/trending (no official API). Best-effort: layout
    changes degrade to zero items, never to bad data."""
    res = client.fetch(GH_TRENDING, conditional=False)
    if res.status != 200 or not res.content:
        raise RuntimeError(res.error or ("HTTP %s" % res.status))
    html = res.content.decode("utf-8", "ignore")
    items = []
    # Split per repo card; tolerate layout drift.
    chunks = html.split('<article class="Box-row"')[1:]
    for chunk in chunks[:25]:
        repo = None
        if 'href="/' in chunk:
            repo = chunk.split('href="/', 1)[1].split('"', 1)[0]
        if not repo or repo.count("/") != 1:
            continue
        stars_today = None
        sm = _GH_STARS_TODAY_RE.search(chunk)
        if sm:
            stars_today = int(sm.group(1).replace(",", ""))
        # First <p> = description
        desc = ""
        if "<p" in chunk:
            after_p = chunk.split("<p", 1)[1]
            if ">" in after_p:
                desc = re.sub(r"<[^>]+>", "", after_p.split(">", 1)[1].split("</p>", 1)[0])
                desc = " ".join(desc.split())
        items.append(
            {
                "guid": "ghtrend-%s" % repo,
                "raw_url": "https://github.com/%s" % repo,
                "title": "%s — %s" % (repo, desc) if desc else repo,
                "author": repo.split("/")[0],
                "published_at": None,
                "points": stars_today,
                "comments": None,
                "extra": {"surface": "github-trending"},
            }
        )
    return items
