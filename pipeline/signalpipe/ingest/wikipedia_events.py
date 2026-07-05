"""Wikipedia Portal:Current events day pages via the parse API.

Page naming (verified live): "Portal:Current_events/2026_June_9" — year,
full month name, unpadded day, underscore-separated. One fetch per day page
(today + yesterday by default, UTC) so early-UTC runs aren't empty.

Each event is a leaf <li> bullet whose own text (before any nested <ul>)
ends with external citation links. raw_url = first non-Wikimedia citation;
fallback = the first linked Wikipedia article. Topic-header bullets (bare
article links wrapping nested lists) and the edit/history/watch chrome are
dropped. Relevance filtering is scoring's job, not ours.
"""

from __future__ import annotations

import datetime
import hashlib
import html as html_mod
import json
import re
import sys
from typing import List, Optional
from urllib.parse import quote, urlsplit

from .fetch_http import PoliteClient

API_URL = (
    "https://en.wikipedia.org/w/api.php?action=parse&page=%s"
    "&format=json&prop=text"
)
WIKIMEDIA_SUFFIXES = (
    "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org",
    "wikinews.org", "wikisource.org",
)

_HREF_RE = re.compile(r'href="([^"]+)"')
_CITATION_RE = re.compile(r'<a [^>]*class="external text"[^>]*>.*?</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def page_title(day: datetime.date) -> str:
    return "Portal:Current_events/%d_%s_%d" % (day.year, day.strftime("%B"), day.day)


def api_url(day: datetime.date) -> str:
    return API_URL % quote(page_title(day), safe="")


def _is_wikimedia(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in WIKIMEDIA_SUFFIXES)


def _bullet_items(page_html: str, day: datetime.date) -> List[dict]:
    items = []
    day_iso = datetime.datetime(
        day.year, day.month, day.day, tzinfo=datetime.timezone.utc
    ).isoformat()
    for seg in page_html.split("<li>")[1:]:
        # Own text only: stop at the first nested list or the close tag.
        cut = len(seg)
        for stop in ("<ul>", "<ul ", "</li>"):
            i = seg.find(stop)
            if i != -1:
                cut = min(cut, i)
        own = seg[:cut]

        citation_url = None
        wiki_url = None
        for href in _HREF_RE.findall(own):
            href = html_mod.unescape(href)
            if href.startswith("/wiki/") and wiki_url is None:
                wiki_url = "https://en.wikipedia.org" + href
            elif href.startswith("http") and not _is_wikimedia(href):
                if citation_url is None:
                    citation_url = href
        raw_url = citation_url or wiki_url
        if not raw_url:
            continue

        # Title: drop the "(Source)" citation anchors, then all markup.
        text = _CITATION_RE.sub("", own)
        text = _TAG_RE.sub("", text)
        text = " ".join(html_mod.unescape(text).split()).strip(" .,;–—")
        if not text:
            continue
        if citation_url is None and len(text) < 40:
            continue  # topic header, not an event
        title = text[:140]

        items.append(
            {
                "guid": "wiki-%s-%s" % (
                    day.strftime("%Y%m%d"),
                    hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
                ),
                "raw_url": raw_url,
                "title": title,
                "author": None,
                "published_at": day_iso,
                "points": None,
                "comments": None,
                "extra": {
                    "surface": "wikipedia-current-events",
                    "date": day.isoformat(),
                    "wiki_url": wiki_url,
                },
            }
        )
    return items


def fetch_items(client: PoliteClient, source_row, days: int = 2,
                today: Optional[datetime.date] = None) -> List[dict]:
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    items = []
    errors = []
    for back in range(max(1, days)):
        day = today - datetime.timedelta(days=back)
        res = client.fetch(api_url(day), conditional=False)
        if res.status != 200 or not res.content:
            errors.append("%s: %s" % (day, res.error or ("HTTP %s" % res.status)))
            continue
        try:
            data = json.loads(res.content)
        except ValueError as e:
            errors.append("%s: bad JSON (%s)" % (day, e))
            continue
        if "error" in data:
            # Today's page may not exist yet right after UTC midnight.
            errors.append("%s: %s" % (day, data["error"].get("code")))
            continue
        text = (data.get("parse") or {}).get("text") or {}
        page_html = text.get("*") if isinstance(text, dict) else text
        if not page_html:
            errors.append("%s: empty parse text" % day)
            continue
        items.extend(_bullet_items(page_html, day))
    if errors and not items:
        raise RuntimeError("wikipedia current events failed: %s" % "; ".join(errors))
    for err in errors:
        print("wiki-current-events: %s" % err, file=sys.stderr)
    return items
