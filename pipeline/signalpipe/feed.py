"""Parameterized RSS 2.0 generation.

Hand-rolled (lxml-free string assembly with strict escaping) for exact control
over content:encoded CDATA. Reader-compat decisions per research Part G:
  - <guid isPermaLink="false">tag:starikov.co,2026:signal/<cluster_id></guid>
    byte-stable across re-scores so readers never duplicate items.
  - <link> = best free read_url (canonical recorded in <source> + dc:source).
  - <description> = plaintext one-liner; <content:encoded> = rich CDATA block
    with absolute URLs and no custom CSS/JS.
  - archive_url is INTERNAL ONLY and never rendered here.

Query params: channel|topic, min_score, min_relevance, since (ISO | 7d | 24h),
limit, sources (comma slugs). Curated items serve first; if none match (e.g.
pre-LLM), scored-but-uncurated clusters fall back so the feed is useful from
Phase 3 on.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape, quoteattr

# XML 1.0 forbids most C0 controls (tab/newline/CR are the only legal ones);
# feedparser passes stray bytes through, so scrub them before embedding.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_safe(s: str) -> str:
    return escape(_XML_ILLEGAL.sub("", s or ""))

GUID_FMT = "tag:starikov.co,2026:signal/%d"
_REL_RE = re.compile(r"^(\d+)([hdwm])$")


def parse_since(raw: Optional[str]) -> Optional[str]:
    """'7d' / '24h' / '30m' / '2w' / ISO date -> ISO cutoff (UTC)."""
    if not raw:
        return None
    raw = raw.strip().lower()
    m = _REL_RE.match(raw)
    now = datetime.datetime.now(datetime.timezone.utc)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "m": datetime.timedelta(minutes=n),
            "h": datetime.timedelta(hours=n),
            "d": datetime.timedelta(days=n),
            "w": datetime.timedelta(weeks=n),
        }[unit]
        return (now - delta).isoformat()
    try:
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def _rfc822(iso: Optional[str]) -> str:
    if iso:
        try:
            dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return email.utils.format_datetime(dt)
        except ValueError:
            pass
    return email.utils.format_datetime(
        datetime.datetime.now(datetime.timezone.utc)
    )


def _surfaces_for(conn: sqlite3.Connection, cluster_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT s.url, s.points, s.comments, src.name, src.slug, src.homepage "
        "FROM surfaces s JOIN sources src ON src.id = s.source_id "
        "WHERE s.cluster_id=? ORDER BY s.points IS NULL, s.points DESC",
        (cluster_id,),
    ).fetchall()


def query_items(
    conn: sqlite3.Connection,
    cfg,
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Resolve feed params -> list of render-ready item dicts."""
    limit = min(
        int(params.get("limit") or cfg.server.get("feed_default_limit", 50)),
        int(cfg.server.get("feed_max_limit", 200)),
    )
    channel = params.get("channel") or params.get("topic")
    if channel == "everything":
        channel = None
    # Unknown channel = no results (NOT "no filter"): the raw value would
    # otherwise be interpolated into a LIKE pattern where % / _ are live
    # wildcards, silently bypassing channel gating.
    if channel and channel not in cfg.channels:
        return []
    since = parse_since(params.get("since"))
    min_score = params.get("min_score")
    min_rel = params.get("min_relevance")
    sources_filter = [
        s.strip() for s in (params.get("sources") or "").split(",") if s.strip()
    ]

    def source_clause(alias: str) -> str:
        if not sources_filter:
            return ""
        marks = ",".join("?" for _ in sources_filter)
        return (
            " AND EXISTS (SELECT 1 FROM surfaces sf JOIN sources so "
            "ON so.id=sf.source_id WHERE sf.cluster_id=%s.id "
            "AND so.slug IN (%s))" % (alias, marks)
        )

    items: List[Dict[str, Any]] = []

    # --- curated items first -------------------------------------------------
    sql = (
        "SELECT c.id, c.title, c.canonical_url, c.last_seen, c.first_seen, "
        "c.score, c.surface_count, cu.relevance_score, cu.why_it_matters, "
        "cu.notes, cu.summary, cu.channels, cu.novelty, cu.audience, "
        "cu.curated_at, a.source_url, a.read_url, a.read_kind, a.paywalled, "
        "a.excerpt "
        "FROM clusters c "
        "JOIN curations cu ON cu.cluster_id = c.id "
        "LEFT JOIN articles a ON a.cluster_id = c.id "
        "WHERE cu.status='done' AND cu.skip=0 AND cu.relevance_score >= ?"
    )
    if min_rel is not None:
        _eff_rel = int(min_rel)
    else:
        import datetime as _dt

        from . import adaptive
        _eff_rel = adaptive.effective_min_relevance(
            conn, cfg.funnel.get("adaptive", {}),
            _dt.datetime.now(_dt.timezone.utc),
            base=int(cfg.funnel.get("min_relevance_for_feed", 6)))
    args: List[Any] = [_eff_rel]
    if channel:
        sql += " AND cu.channels LIKE ?"
        args.append('%%"%s"%%' % channel)
    if since:
        sql += " AND cu.curated_at >= ?"
        args.append(since)
    if min_score is not None:
        sql += " AND c.score >= ?"
        args.append(float(min_score))
    sql += source_clause("c")
    args.extend(sources_filter)
    sql += " ORDER BY cu.curated_at DESC LIMIT ?"
    args.append(limit)

    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        d["curated"] = True
        d["notes_list"] = json.loads(d.get("notes") or "[]")
        d["channel_list"] = json.loads(d.get("channels") or "[]")
        d["surfaces"] = [dict(s) for s in _surfaces_for(conn, d["id"])]
        d["link"] = d.get("read_url") or d.get("canonical_url") or ""
        items.append(d)

    if items:
        return items

    # --- fallback: scored-but-uncurated (pre-LLM phase or thin params) -------
    from . import topics as topics_mod

    topics_data = topics_mod.build_or_load(cfg)
    sql = (
        "SELECT c.id, c.title, c.canonical_url, c.last_seen, c.first_seen, "
        "c.score, c.surface_count "
        "FROM clusters c WHERE c.score IS NOT NULL AND c.score >= ?"
    )
    args = [float(min_score) if min_score is not None else 5.0]
    if since:
        sql += " AND c.last_seen >= ?"
        args.append(since)
    sql += source_clause("c")
    args.extend(sources_filter)
    sql += " ORDER BY c.score DESC LIMIT ?"
    args.append(limit)

    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        if channel and channel not in topics_mod.match_channels(
            d["title"], topics_data
        ):
            continue
        d["curated"] = False
        d["notes_list"] = []
        d["channel_list"] = sorted(
            topics_mod.match_channels(d["title"], topics_data)
        )
        d["surfaces"] = [dict(s) for s in _surfaces_for(conn, d["id"])]
        d["link"] = d.get("canonical_url") or (
            d["surfaces"][0]["url"] if d["surfaces"] else ""
        )
        d["paywalled"] = 0
        d["read_kind"] = None
        d["source_url"] = d.get("canonical_url")
        items.append(d)
    return items


# ---------------------------------------------------------------------------
# XML rendering
# ---------------------------------------------------------------------------

def _cdata(html: str) -> str:
    # CDATA cannot contain "]]>" — split the sequence if present.
    return "<![CDATA[%s]]>" % html.replace("]]>", "]]]]><![CDATA[>")


def render_rss(
    items: List[Dict[str, Any]],
    item_html: Dict[int, str],
    cfg,
    self_url: str,
) -> str:
    s = cfg.server
    now = _rfc822(None)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>%s</title>" % escape(s.get("feed_title", "Signal")),
        "<link>%s</link>" % escape(s.get("feed_link", "http://127.0.0.1:8765/")),
        "<description>%s</description>" % escape(s.get("feed_description", "")),
        "<language>en-us</language>",
        "<lastBuildDate>%s</lastBuildDate>" % now,
        '<atom:link href=%s rel="self" type="application/rss+xml"/>'
        % quoteattr(self_url),
        "<generator>signalpipe</generator>",
    ]
    for it in items:
        title = it["title"]
        if not it.get("curated"):
            title = "[uncurated %.1f] %s" % (it.get("score") or 0.0, title)
        desc = (it.get("why_it_matters") or it.get("excerpt") or "")[:500]
        pub = it.get("curated_at") or it.get("last_seen")
        out.append("<item>")
        out.append("<title>%s</title>" % _xml_safe(title))
        if it.get("link"):
            out.append("<link>%s</link>" % escape(it["link"]))
        out.append(
            '<guid isPermaLink="false">%s</guid>' % escape(GUID_FMT % it["id"])
        )
        out.append("<pubDate>%s</pubDate>" % _rfc822(pub))
        out.append("<description>%s</description>" % _xml_safe(desc))
        if it.get("source_url") and it.get("source_url") != it.get("link"):
            out.append("<dc:source>%s</dc:source>" % escape(it["source_url"]))
        html = item_html.get(it["id"])
        if html:
            out.append("<content:encoded>%s</content:encoded>" % _cdata(html))
        out.append("</item>")
    out.append("</channel>")
    out.append("</rss>")
    return "\n".join(out)
