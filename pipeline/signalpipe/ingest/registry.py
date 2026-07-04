"""Source registry: sources.json is THE registry (all access types);
sources.opml is GENERATED from it (rss/atom entries, grouped by category) for
human review, feed-reader import, and serving at /opml.

Operation 1k machinery lives here:
  seed    — import sources.json into the DB, regenerate the OPML
  stats   — counts by category / tier / verified
  probe   — verify candidate homepages/feeds actually expose a live feed
            (rel=alternate discovery first, then common-path probing)
  import  — merge a verified-candidates JSON into sources.json (deduped)
  expand  — built-in expanders: Techmeme lb.opml, arXiv categories,
            Reddit subs, aggregator API sources

Note: code/opml_to_markdown.py has OPML parsing too, but `code/` shadows the
stdlib `code` module and isn't a package — we keep a 20-line parser here
instead of importing across that boundary.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote
from xml.sax.saxutils import quoteattr

try:
    # Remote OPML is untrusted input — defusedxml guards XXE/billion-laughs.
    import defusedxml.ElementTree as ET
    from xml.etree.ElementTree import ParseError
except ImportError:  # stdlib expat ≥2.4 has billion-laughs limits; acceptable fallback
    import xml.etree.ElementTree as ET
    from xml.etree.ElementTree import ParseError

from .. import db as db_mod
from ..canonical import canonicalize, registered_domain
from ..models import ProbeResult, SourceSpec
from . import gdelt as gdelt_mod
from .fetch_http import PoliteClient

# seed() must not resurrect sources the pipeline auto-disabled at runtime:
# rows with enabled=0 AND error_count >= this stay disabled even when the
# registry spec says enabled (specs default enabled=true, so every seed —
# and every bulk-import wave calls seed — would otherwise re-enable broken
# sources into a permanent disable/re-enable churn loop). 3 matches the
# `stats` "failing(3+)" notion; the auto-disable itself trips at 10.
PRESERVE_DISABLED_ERRORS = 3

# Feeds known dead/parked/redirecting (research Part A) — never import these.
DEAD_FEEDS = {
    "https://openai.com/blog/rss.xml",
    "https://deepmind.com/blog/rss.xml",
    "https://blog.golang.org/feed.atom",
    "https://ai.googleblog.com/atom.xml",
    "https://www.tomshardware.com/rss.html",
    "https://paperswithcode.com/feed.xml",
}

FEED_LINK_RE = re.compile(
    r"<link[^>]+rel=[\"']alternate[\"'][^>]*>", re.I | re.S
)
TYPE_RE = re.compile(r"type=[\"']application/(rss|atom)\+xml[\"']", re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:64] or "source"


# ---------------------------------------------------------------------------
# registry file I/O
# ---------------------------------------------------------------------------

def load_specs(cfg) -> List[SourceSpec]:
    path = cfg.sources_json
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    specs = []
    for raw in data.get("sources", []):
        spec = SourceSpec(
            slug=raw.get("slug") or slugify(raw.get("name", "")),
            name=raw.get("name", ""),
            type=raw.get("type", "rss"),
            url=raw.get("url", ""),
            homepage=raw.get("homepage"),
            category=raw.get("category", "uncategorized"),
            topics=list(raw.get("topics", [])),
            reputation=float(raw.get("reputation", 1.0)),
            tier=int(raw.get("tier", 2)),
            cadence_min=int(raw.get("cadence_min", 60)),
            paywalled=bool(raw.get("paywalled", False)),
            enabled=bool(raw.get("enabled", True)),
            mode=raw.get("mode"),
            why=raw.get("why"),
            api_notes=raw.get("api_notes"),
        )
        specs.append(spec)
    return specs


def save_specs(cfg, specs: List[SourceSpec]) -> None:
    out = {"sources": []}
    for s in sorted(specs, key=lambda x: (x.category, x.tier, x.slug)):
        out["sources"].append(
            {
                "slug": s.slug,
                "name": s.name,
                "category": s.category,
                "type": s.type,
                "url": s.url,
                "homepage": s.homepage,
                "topics": s.topics,
                "reputation": s.reputation,
                "tier": s.tier,
                "cadence_min": s.cadence_min,
                "paywalled": s.paywalled,
                "enabled": s.enabled,
                "mode": s.mode,
                "why": s.why,
                "api_notes": s.api_notes,
            }
        )
    cfg.sources_json.parent.mkdir(parents=True, exist_ok=True)
    cfg.sources_json.write_text(json.dumps(out, indent=1) + "\n")
    write_opml(cfg, specs)


def write_opml(cfg, specs: List[SourceSpec]) -> None:
    """Generate the reviewable OPML from rss/atom registry entries."""
    by_cat: Dict[str, List[SourceSpec]] = {}
    for s in specs:
        if s.type in ("rss", "atom") and s.enabled:
            by_cat.setdefault(s.category, []).append(s)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        "    <title>Signal source registry (generated from sources.json)</title>",
        "    <dateModified>%s</dateModified>" % _now_iso(),
        "  </head>",
        "  <body>",
    ]
    for cat in sorted(by_cat):
        lines.append("    <outline text=%s>" % quoteattr(cat))
        for s in sorted(by_cat[cat], key=lambda x: (x.tier, x.slug)):
            lines.append(
                "      <outline type=\"rss\" text=%s title=%s xmlUrl=%s htmlUrl=%s/>"
                % (
                    quoteattr(s.name),
                    quoteattr(s.name),
                    quoteattr(s.url),
                    quoteattr(s.homepage or s.url),
                )
            )
        lines.append("    </outline>")
    lines += ["  </body>", "</opml>", ""]
    cfg.sources_opml.write_text("\n".join(lines))


MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def parse_markdown_list(text: str) -> List[Tuple[str, str]]:
    """Extract (name, url) link pairs from markdown bullet/table lines.
    Used by bulk_import for GitHub 'awesome list' README harvesting."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for line in text.splitlines():
        ls = line.strip()
        if not ls.startswith(("-", "*", "|")):
            continue
        for name, url in MD_LINK_RE.findall(ls):
            if url in seen:
                continue
            seen.add(url)
            out.append((name.strip(), url))
    return out


OUTLINE_TAG_RE = re.compile(rb"<outline\b[^>]*?xmlUrl\s*=\s*\"[^\"]*\"[^>]*>", re.I)
_ATTR_RES = {
    name: re.compile(
        (name + r'\s*=\s*"([^"]*)"').encode("ascii"), re.I
    )
    for name in ("xmlUrl", "htmlUrl", "title", "text")
}


def _parse_opml_regex(content: bytes) -> List[Tuple[str, str, Optional[str]]]:
    """Last-resort OPML extraction for malformed XML (raw HTML/ampersands in
    attribute values — common in app-exported OPML). Pulls xmlUrl outlines
    with a tag-level regex; attribute order doesn't matter."""
    out = []
    for tag in OUTLINE_TAG_RE.findall(content):
        def attr(name):
            m = _ATTR_RES[name].search(tag)
            return m.group(1).decode("utf-8", "ignore") if m else None
        xml_url = attr("xmlUrl")
        if not xml_url:
            continue
        title = attr("title") or attr("text") or xml_url
        out.append((title.replace("&amp;", "&"), xml_url, attr("htmlUrl")))
    return out


def parse_opml(content: bytes) -> List[Tuple[str, str, Optional[str]]]:
    """Parse OPML bytes -> [(title, xmlUrl, htmlUrl)] for rss outlines.
    Tolerates real-world malformations: bare ampersands (escape + retry),
    then raw HTML in attribute values (regex extraction fallback)."""
    out = []
    try:
        root = ET.fromstring(content)
    except ParseError:
        fixed = re.sub(rb"&(?!#?\w+;)", b"&amp;", content)
        try:
            root = ET.fromstring(fixed)
        except ParseError:
            return _parse_opml_regex(content)

    def walk(el):
        for child in el:
            if child.tag.lower() == "outline":
                xml_url = child.get("xmlUrl")
                if xml_url:
                    out.append(
                        (
                            child.get("title") or child.get("text") or xml_url,
                            xml_url,
                            child.get("htmlUrl"),
                        )
                    )
                walk(child)

    body = root.find("body")
    walk(body if body is not None else root)
    return out


# ---------------------------------------------------------------------------
# DB seed + stats
# ---------------------------------------------------------------------------

def seed(cfg) -> int:
    specs = load_specs(cfg)
    if not specs:
        print("no sources in %s — nothing to seed" % cfg.sources_json)
        return 1
    errors = []
    for s in specs:
        err = s.validate()
        if err:
            errors.append("%s: %s" % (s.slug, err))
    if errors:
        print("invalid specs:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1

    conn = db_mod.connect_rw(cfg.db_path)
    try:
        n_new, n_upd = 0, 0
        with db_mod.write_tx(conn):
            for s in specs:
                row = conn.execute(
                    "SELECT id, enabled, error_count FROM sources "
                    "WHERE slug=?", (s.slug,)
                ).fetchone()
                enabled = int(s.enabled)
                if (row is not None and enabled
                        and not row["enabled"]
                        and int(row["error_count"] or 0)
                        >= PRESERVE_DISABLED_ERRORS):
                    # `enabled` is also runtime state: keep auto-disabled
                    # sources disabled. The spec may still explicitly
                    # disable (enabled flows through); never flips 0 -> 1
                    # for a failing source.
                    enabled = 0
                params = (
                    s.name, s.category, s.type, s.url, s.homepage,
                    json.dumps(s.topics), s.reputation, s.tier, s.cadence_min,
                    int(s.paywalled), enabled, s.mode, s.why,
                    s.api_notes,
                )
                if row:
                    conn.execute(
                        "UPDATE sources SET name=?, category=?, type=?, url=?, "
                        "homepage=?, topics=?, reputation=?, tier=?, "
                        "cadence_min=?, paywalled=?, enabled=?, mode=?, why=?, "
                        "api_notes=? WHERE slug=?",
                        params + (s.slug,),
                    )
                    n_upd += 1
                else:
                    conn.execute(
                        "INSERT INTO sources(name, category, type, url, "
                        "homepage, topics, reputation, tier, cadence_min, "
                        "paywalled, enabled, mode, why, api_notes, slug, "
                        "added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        params + (s.slug, _now_iso()),
                    )
                    n_new += 1
        db_mod.log_health(
            conn, "sources", "info",
            "seed: %d new, %d updated (%d total in registry)"
            % (n_new, n_upd, len(specs)),
        )
        print("seeded: %d new, %d updated, %d total" % (n_new, n_upd, len(specs)))
    finally:
        conn.close()
    cfg.update_tracking(["signalpipe/sources.json", "signalpipe/sources.opml"])
    return 0


def stats(cfg) -> int:
    if not cfg.db_path.exists():
        # Fall back to registry-file stats pre-DB.
        specs = load_specs(cfg)
        print("registry file: %d sources (db not created yet)" % len(specs))
        return 0
    conn = db_mod.connect_ro(cfg.db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        enabled = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE enabled=1"
        ).fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE enabled=1 AND verified_at IS NOT NULL"
        ).fetchone()[0]
        failing = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE error_count >= 3"
        ).fetchone()[0]
        print(
            "sources: %d total, %d enabled, %d verified, %d failing(3+)"
            % (total, enabled, verified, failing)
        )
        print("%-28s %5s %5s %5s  tier1/2/3" % ("category", "total", "enab", "verif"))
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n, SUM(enabled) AS e, "
            "SUM(CASE WHEN verified_at IS NOT NULL AND enabled=1 THEN 1 ELSE 0 END) AS v, "
            "SUM(CASE WHEN tier=1 THEN 1 ELSE 0 END) AS t1, "
            "SUM(CASE WHEN tier=2 THEN 1 ELSE 0 END) AS t2, "
            "SUM(CASE WHEN tier=3 THEN 1 ELSE 0 END) AS t3 "
            "FROM sources GROUP BY category ORDER BY n DESC"
        ).fetchall()
        for r in rows:
            print(
                "%-28s %5d %5d %5d  %d/%d/%d"
                % (r["category"], r["n"], r["e"], r["v"], r["t1"], r["t2"], r["t3"])
            )
        target = int(cfg.data.get("operation_1k", {}).get("target_verified", 1000))
        print("operation-1k: %d / %d verified" % (verified, target))
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# probe / verify
# ---------------------------------------------------------------------------

def _looks_like_feed(content: bytes) -> Tuple[bool, Optional[str], Optional[str], int, Optional[str]]:
    """(ok, kind, title, entries, latest_iso) via feedparser."""
    import calendar

    import feedparser

    parsed = feedparser.parse(content)
    version = (parsed.get("version") or "").lower()
    entries = parsed.get("entries") or []
    if not version and not entries:
        return False, None, None, 0, None
    kind = "atom" if "atom" in version else "rss"
    title = (parsed.get("feed") or {}).get("title")
    latest = None
    for e in entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            iso = datetime.datetime.fromtimestamp(
                calendar.timegm(t), datetime.timezone.utc
            ).isoformat()
            if latest is None or iso > latest:
                latest = iso
    return True, kind, title, len(entries), latest


def _discover_feed_links(homepage_html: bytes, base_url: str) -> List[str]:
    """rel=alternate feed discovery from homepage HTML (most reliable)."""
    from urllib.parse import urljoin

    found = []
    try:
        text = homepage_html.decode("utf-8", "ignore")
    except Exception:
        return found
    for tag in FEED_LINK_RE.findall(text):
        if not TYPE_RE.search(tag):
            continue
        m = HREF_RE.search(tag)
        if m:
            found.append(urljoin(base_url, m.group(1)))
    return found


def probe_url(client: PoliteClient, url: str, probe_paths: List[str]) -> ProbeResult:
    """Probe a homepage or direct feed URL for a live, valid feed."""
    res = client.fetch(url, conditional=False)
    if res.status == 0 or res.content is None:
        return ProbeResult(candidate_url=url, error=res.error or "fetch failed")

    ok, kind, title, n, latest = _looks_like_feed(res.content)
    if ok and n > 0:
        return ProbeResult(
            candidate_url=url, feed_url=res.final_url or url, ok=True,
            kind=kind, title=title, entries=n, latest_entry=latest,
        )

    # Not a feed: treat as homepage. 1) rel=alternate discovery
    base = res.final_url or url
    for cand in _discover_feed_links(res.content, base)[:3]:
        r2 = client.fetch(cand, conditional=False)
        if r2.status == 200 and r2.content:
            ok, kind, title, n, latest = _looks_like_feed(r2.content)
            if ok and n > 0:
                return ProbeResult(
                    candidate_url=url, feed_url=r2.final_url or cand, ok=True,
                    kind=kind, title=title, entries=n, latest_entry=latest,
                )
    # 2) common-path probing
    base_root = base.rstrip("/")
    for path in probe_paths:
        cand = base_root + path
        r2 = client.fetch(cand, conditional=False)
        if r2.status == 200 and r2.content:
            ok, kind, title, n, latest = _looks_like_feed(r2.content)
            if ok and n > 0:
                return ProbeResult(
                    candidate_url=url, feed_url=r2.final_url or cand, ok=True,
                    kind=kind, title=title, entries=n, latest_entry=latest,
                )
    return ProbeResult(candidate_url=url, error="no valid feed found")


def _existing_keys(cfg) -> Tuple[set, set]:
    """(canonical feed URLs, homepage registered domains) already registered."""
    feed_keys = set()
    domains = set()
    for s in load_specs(cfg):
        cu = canonicalize(s.url)
        if cu:
            feed_keys.add(cu)
        home = s.homepage or s.url
        if home:
            domains.add(registered_domain(home))
    return feed_keys, domains


def probe_candidates(
    cfg, candidates: List[dict], max_workers: int = 8
) -> Tuple[List[dict], List[dict]]:
    """Probe candidate dicts -> (verified, rejected). Candidate dict:
    {name, homepage, feed_url?, topics?, tier?, category?, why?, paywalled?}
    Already-registered feeds/domains are rejected as duplicates."""
    feed_keys, domains = _existing_keys(cfg)
    probe_paths = cfg.data.get("operation_1k", {}).get("probe_paths", ["/feed/"])
    verified: List[dict] = []
    rejected: List[dict] = []

    # ONE shared client across all worker threads: PoliteClient's per-host
    # rate limiter is instance state, so per-thread instances would let all
    # workers hammer the same host concurrently (429s from arXiv/GitHub).
    shared_client = PoliteClient(cfg)

    def work(cand: dict):
        url = cand.get("feed_url") or cand.get("homepage")
        if not url:
            return cand, ProbeResult(candidate_url="", error="no url")
        result = probe_url(shared_client, url, probe_paths)
        # A guessed feed_url that 404s shouldn't sink the candidate: fall
        # back to homepage rel=alternate discovery + common-path probing.
        home = cand.get("homepage")
        if not result.ok and cand.get("feed_url") and home and home != url:
            result = probe_url(shared_client, home, probe_paths)
        return cand, result

    with shared_client, concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers
    ) as ex:
        for cand, result in ex.map(work, candidates):
            name = cand.get("name") or result.title or result.candidate_url
            if not result.ok:
                rejected.append(
                    {"name": name, "url": result.candidate_url, "reason": result.error}
                )
                continue
            cu = canonicalize(result.feed_url)
            if not cu or cu in feed_keys or (result.feed_url in DEAD_FEEDS):
                rejected.append(
                    {"name": name, "url": result.feed_url, "reason": "duplicate/dead"}
                )
                continue
            dom = registered_domain(cand.get("homepage") or result.feed_url)
            if not cand.get("feed_url") and dom in domains:
                rejected.append(
                    {"name": name, "url": result.feed_url,
                     "reason": "domain already registered (%s)" % dom}
                )
                continue
            feed_keys.add(cu)
            domains.add(dom)
            verified.append(
                {
                    "name": name,
                    "slug": cand.get("slug") or slugify(name),
                    "type": result.kind or "rss",
                    "url": result.feed_url,
                    "homepage": cand.get("homepage"),
                    "category": cand.get("category", "uncategorized"),
                    "topics": cand.get("topics", []),
                    "tier": int(cand.get("tier", 3)),
                    "reputation": float(cand.get("reputation", 0.8)),
                    "cadence_min": int(cand.get("cadence_min", 180)),
                    "paywalled": bool(cand.get("paywalled", False)),
                    "why": cand.get("why"),
                    "entries": result.entries,
                    "latest_entry": result.latest_entry,
                }
            )
    return verified, rejected


def merge_into_registry(cfg, verified: List[dict]) -> int:
    """Merge verified candidate dicts into sources.json (dedupe by slug/feed)."""
    specs = load_specs(cfg)
    by_slug = {s.slug: s for s in specs}
    feed_keys = {canonicalize(s.url) for s in specs}
    added = 0
    for v in verified:
        cu = canonicalize(v["url"])
        if cu in feed_keys:
            continue
        slug = v["slug"]
        while slug in by_slug:
            slug = slug + "-2"
        spec = SourceSpec(
            slug=slug,
            name=v["name"],
            type=v["type"] if v["type"] in ("rss", "atom") else "rss",
            url=v["url"],
            homepage=v.get("homepage"),
            category=v.get("category", "uncategorized"),
            topics=[t for t in v.get("topics", []) if t],
            reputation=float(v.get("reputation", 0.8)),
            tier=int(v.get("tier", 3)),
            cadence_min=int(v.get("cadence_min", 180)),
            paywalled=bool(v.get("paywalled", False)),
            why=v.get("why"),
        )
        err = spec.validate()
        if err:
            continue
        specs.append(spec)
        by_slug[slug] = spec
        feed_keys.add(cu)
        added += 1
    if added:
        save_specs(cfg, specs)
    return added


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def probe_cmd(cfg, candidates: Optional[pathlib.Path], url: Optional[str],
              import_ok: bool) -> int:
    if url:
        probe_paths = cfg.data.get("operation_1k", {}).get("probe_paths", ["/feed/"])
        with PoliteClient(cfg) as client:
            r = probe_url(client, url, probe_paths)
        print(json.dumps(r.__dict__, indent=2))
        return 0 if r.ok else 1
    if not candidates:
        print("need --candidates file or --url", file=sys.stderr)
        return 2
    cands = json.loads(candidates.read_text())
    if isinstance(cands, dict):
        cands = cands.get("candidates") or cands.get("sources") or []
    verified, rejected = probe_candidates(cfg, cands)
    print("verified %d / rejected %d of %d candidates"
          % (len(verified), len(rejected), len(cands)))
    for r in rejected:
        print("  REJECT %-40s %s" % ((r.get("name") or "?")[:40], r.get("reason")))
    out_path = candidates.with_suffix(".verified.json")
    out_path.write_text(json.dumps(verified, indent=1))
    print("verified candidates -> %s" % out_path)
    if import_ok and verified:
        added = merge_into_registry(cfg, verified)
        print("imported %d into %s" % (added, cfg.sources_json))
        seed(cfg)
    return 0


def import_cmd(cfg, path: pathlib.Path) -> int:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("sources") or data.get("candidates") or []
    added = merge_into_registry(cfg, data)
    print("imported %d new sources" % added)
    if added:
        return seed(cfg)
    return 0


def expand(cfg) -> int:
    """Built-in expanders: Techmeme lb.opml + arXiv categories + Reddit subs.
    Idempotent — duplicates are skipped by feed-URL/domain dedupe."""
    specs = load_specs(cfg)
    by_slug = {s.slug for s in specs}
    feed_keys = {canonicalize(s.url) for s in specs}
    added = 0

    # arXiv categories (cs./stat. -> ml-research; physics/math -> science)
    for cat in cfg.ingest.get("arxiv_categories", []):
        slug = "arxiv-" + cat.lower().replace(".", "-")
        url = "https://rss.arxiv.org/rss/%s" % cat
        if slug in by_slug or canonicalize(url) in feed_keys:
            continue
        is_cs = cat.startswith(("cs.", "stat.", "eess."))
        specs.append(SourceSpec(
            slug=slug, name="arXiv %s" % cat, type="rss", url=url,
            homepage="https://arxiv.org/list/%s/recent" % cat,
            category="research" if is_cs else "physics",
            topics=["ml-research"] if is_cs else ["science"], reputation=1.2,
            tier=1 if cat in ("cs.AI", "cs.LG", "cs.CL") else 2,
            cadence_min=720, why="Primary preprint feed (%s)" % cat,
        ))
        by_slug.add(slug)
        feed_keys.add(canonicalize(url))
        added += 1

    # Reddit subs (each sub = one source; fetcher dispatches on slug prefix)
    for sub in cfg.ingest.get("reddit_subs", []):
        slug = "reddit-" + sub.lower()
        url = "https://www.reddit.com/r/%s/top.json?t=day&limit=50" % sub
        if slug in by_slug:
            continue
        specs.append(SourceSpec(
            slug=slug, name="r/%s" % sub, type="json", url=url,
            homepage="https://www.reddit.com/r/%s" % sub,
            category="aggregators", topics=["ai"], reputation=1.0, tier=2,
            cadence_min=180, mode=cfg.ingest.get("reddit_mode", "public_json"),
            why="Community-scored signal (r/%s)" % sub,
        ))
        by_slug.add(slug)
        added += 1

    # Google News topic sections + query feeds (fetcher resolves the
    # news.google.com redirect wrappers; slug prefix "gnews-").
    gnews_specs = []
    for topic in cfg.ingest.get("gnews_topics", ["TECHNOLOGY", "SCIENCE", "BUSINESS"]):
        gnews_specs.append(SourceSpec(
            slug="gnews-%s" % slugify(topic),
            name="Google News %s" % topic.capitalize(),
            type="json",
            url="https://news.google.com/rss/headlines/section/topic/"
                "%s?hl=en-US&gl=US&ceid=US:en" % topic.upper(),
            homepage="https://news.google.com/",
            category="news", topics=["news"], reputation=1.0, tier=2,
            cadence_min=180,
            why="Google News %s section — broad mainstream coverage of the "
                "beat, cross-outlet" % topic.lower(),
        ))
    gnews_queries = cfg.ingest.get("gnews_queries", {"ai": '"artificial intelligence"'})
    if isinstance(gnews_queries, list):  # tolerate a bare list of queries
        gnews_queries = {slugify(q): q for q in gnews_queries}
    for key in sorted(gnews_queries):
        gnews_specs.append(SourceSpec(
            slug="gnews-q-%s" % key,
            name="Google News query: %s" % gnews_queries[key],
            type="json",
            url="https://news.google.com/rss/search?q=%s"
                "&hl=en-US&gl=US&ceid=US:en" % quote(gnews_queries[key]),
            homepage="https://news.google.com/",
            category="news", topics=["news"], reputation=1.0, tier=2,
            cadence_min=180,
            why="Google News query feed (%s) — topic firehose across every "
                "indexed outlet" % gnews_queries[key],
        ))

    # GDELT DOC API queries (slug prefix "gdelt-"; each row carries its
    # full artlist URL so one source = one query).
    gdelt_specs = []
    gdelt_names = ("tech", "science")
    for i, q in enumerate(cfg.ingest.get("gdelt_queries", gdelt_mod.DEFAULT_QUERIES)):
        name = gdelt_names[i] if i < len(gdelt_names) else "q%d" % (i + 1)
        gdelt_specs.append(SourceSpec(
            slug="gdelt-%s" % name,
            name="GDELT %s" % name,
            type="api",
            url=gdelt_mod.query_url(q),
            homepage="https://www.gdeltproject.org/",
            category="news",
            topics=["ai"] if name == "tech" else ["science"],
            reputation=0.8, tier=3, cadence_min=360,
            why="GDELT global news monitor (%s) — breadth play: surfaces "
                "coverage from outlets no curated feed carries" % q,
        ))

    # Remaining aggregator/news API sources (fetchers dispatch on slug prefix).
    api_specs = [
        SourceSpec(
            slug="mastodon-trends", name="Mastodon trending links", type="json",
            url="https://mastodon.social/api/v1/trends/links",
            homepage="https://mastodon.social/explore/links",
            category="aggregators", topics=["ai", "news"], reputation=1.2,
            tier=1, cadence_min=120,
            why="Fediverse trending EXTERNAL links — community-vetted "
                "articles, the strongest cross-instance clustering signal",
        ),
        SourceSpec(
            slug="bsky-trends", name="Bluesky trending topics", type="json",
            url="https://public.api.bsky.app/xrpc/"
                "app.bsky.unspecced.getTrendingTopics",
            homepage="https://bsky.app/",
            category="aggregators", topics=["news"], reputation=0.8,
            tier=3, cadence_min=240,
            why="Bluesky trend phrases — early chatter detector; unspecced "
                "API, fetcher degrades to empty on schema drift",
        ),
        SourceSpec(
            slug="wiki-current-events", name="Wikipedia Current events",
            type="api",
            url="https://en.wikipedia.org/w/api.php?action=parse&format=json"
                "&prop=text",
            homepage="https://en.wikipedia.org/wiki/Portal:Current_events",
            category="news", topics=["news"], reputation=1.2, tier=1,
            cadence_min=360,
            why="Editor-curated daily events with primary-source citations — "
                "highest precision news surface available without an API key",
        ),
        SourceSpec(
            slug="devto-top", name="dev.to top articles", type="json",
            url="https://dev.to/api/articles?top=1&per_page=50",
            homepage="https://dev.to/",
            category="aggregators", topics=["devtools"], reputation=0.8,
            tier=3, cadence_min=360,
            enabled=bool(cfg.ingest.get("enable_devto", True)),
            why="dev.to daily top by reactions — practitioner dev content "
                "that rarely surfaces on HN/Lobsters",
        ),
        SourceSpec(
            slug="stackoverflow-hot", name="Stack Overflow hot questions",
            type="json",
            url="https://api.stackexchange.com/2.3/questions"
                "?order=desc&sort=hot&site=stackoverflow&pagesize=50",
            homepage="https://stackoverflow.com/",
            category="aggregators", topics=["devtools"], reputation=0.8,
            tier=3, cadence_min=360,
            enabled=bool(cfg.ingest.get("enable_stackexchange", True)),
            why="SO hot list — spikes here flag breaking-change pain in real "
                "tooling (anon quota 300/day; we use ~4)",
        ),
    ]
    for spec in gnews_specs + gdelt_specs + api_specs:
        if spec.slug in by_slug:
            continue
        specs.append(spec)
        by_slug.add(spec.slug)
        feed_keys.add(canonicalize(spec.url))
        added += 1

    # Techmeme leaderboard OPML -> ~100 vetted tech-news feeds
    with PoliteClient(cfg) as client:
        res = client.fetch("https://www.techmeme.com/lb.opml", conditional=False)
    if res.status == 200 and res.content:
        try:
            outlines = parse_opml(res.content)
        except ParseError as e:
            print("lb.opml parse error: %s" % e, file=sys.stderr)
            outlines = []
        for title, xml_url, html_url in outlines:
            cu = canonicalize(xml_url)
            if not cu or cu in feed_keys or xml_url in DEAD_FEEDS:
                continue
            slug = slugify("tm-" + title)
            while slug in by_slug:
                slug = slug + "-2"
            specs.append(SourceSpec(
                slug=slug, name=title, type="rss", url=xml_url,
                homepage=html_url, category="tech_news",
                topics=["startups"], reputation=1.0, tier=2, cadence_min=120,
                why="Techmeme leaderboard source",
            ))
            by_slug.add(slug)
            feed_keys.add(cu)
            added += 1
    else:
        print("could not fetch Techmeme lb.opml (%s)" % (res.error or res.status),
              file=sys.stderr)

    if added:
        save_specs(cfg, specs)
        print("expand: added %d sources (registry now %d)" % (added, len(specs)))
        return seed(cfg)
    print("expand: nothing new")
    return 0
