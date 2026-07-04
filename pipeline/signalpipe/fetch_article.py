"""Fetch + extract finalist articles; resolve paywalls to the best FREE read.

Extraction: trafilatura primary (best-in-class on the ScrapingHub benchmark),
readability-lxml fallback. Short output on a known-paywall domain (or with
subscribe boilerplate) routes into the free-source chain:

  1. primary          — an arXiv/GitHub/official-source surface of the SAME
                        cluster (deterministic, fully ethical, best content)
  2. publication-free — full text already present in the feed/page
  3. freedium         — Medium-hosted posts only
  4. canonical-fallback — paywalled with no free alternative: read_url stays
                        canonical, item is badged. archive.today is OFF by
                        default; when enabled it is stored in articles.archive_url
                        (INTERNAL ONLY — never rendered into feed/digest).
"""

from __future__ import annotations

import datetime
import json
import re
import time
from typing import Optional, Tuple
from urllib.parse import urlsplit

from . import db as db_mod
from .canonical import registered_domain
from .ingest.fetch_http import PoliteClient

PRIMARY_DOMAINS = (
    "arxiv.org", "github.com", "gitlab.com", "openreview.net",
    "huggingface.co", "kernel.org", "python.org", "rust-lang.org", "go.dev",
)
_SUBSCRIBE_RE = re.compile(
    r"subscribe|sign in to continue|create a free account|already a member|"
    r"this article is for (paying )?(subscribers|members)",
    re.I,
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _extract(html_bytes: bytes, url: str) -> Tuple[Optional[str], dict]:
    """(text, meta) via trafilatura, falling back to readability-lxml."""
    meta = {}
    try:
        import trafilatura

        doc = trafilatura.bare_extraction(
            html_bytes, url=url, include_comments=False, favor_precision=True
        )
        if doc:
            # trafilatura >=1.8 returns a Document object or dict by version.
            get = doc.get if isinstance(doc, dict) else lambda k, d=None: getattr(doc, k, d)
            text = get("text") or get("raw_text")
            meta = {
                "author": get("author"),
                "date": get("date"),
                "lang": get("language"),
                "sitename": get("sitename"),
            }
            if text:
                return text, meta
    except Exception:  # noqa: BLE001 — extraction must never kill the run
        pass
    try:
        from readability import Document

        doc = Document(html_bytes.decode("utf-8", "ignore"))
        summary_html = doc.summary()
        text = re.sub(r"<[^>]+>", " ", summary_html)
        text = re.sub(r"\s+", " ", text).strip()
        return (text or None), meta
    except Exception:  # noqa: BLE001
        return None, meta


def _primary_surface(conn, cluster_id: int) -> Optional[str]:
    """An arXiv/GitHub/etc. canonical URL among the cluster's own items."""
    rows = conn.execute(
        "SELECT canonical_url FROM items WHERE cluster_id=? "
        "AND canonical_url IS NOT NULL",
        (cluster_id,),
    ).fetchall()
    for r in rows:
        dom = registered_domain(r["canonical_url"])
        if any(dom == d or dom.endswith("." + d) for d in PRIMARY_DOMAINS):
            return r["canonical_url"]
    return None


def _looks_paywalled(cfg, url: str, text: Optional[str]) -> bool:
    host = registered_domain(url)
    domains = set(cfg.paywall.get("paywall_domains", []))
    on_list = any(host == d or host.endswith("." + d) for d in domains)
    min_words = int(cfg.paywall.get("min_words_not_paywalled", 150))
    words = len((text or "").split())
    if on_list and words < max(min_words * 3, 400):
        return True
    if words < min_words and text and _SUBSCRIBE_RE.search(text):
        return True
    if on_list and not text:
        return True
    return False


def _is_medium(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "medium.com" or host.endswith(".medium.com")


def run(cfg, limit: Optional[int] = None,
        cluster_ids: Optional[list] = None) -> int:
    started = time.time()
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        if cluster_ids:
            # Backfill: re-fetch exactly these clusters (used by backfill.py to
            # recover historical text), ignoring the score gate and the
            # "no article row yet" filter. INSERT OR REPLACE cleanly overwrites
            # an empty/thin prior row.
            rows = []
            for i in range(0, len(cluster_ids), 400):
                part = cluster_ids[i:i + 400]
                rows.extend(conn.execute(
                    "SELECT c.id, c.canonical_url, c.title FROM clusters c "
                    "WHERE c.id IN (%s)" % ",".join("?" * len(part)),
                    part,
                ).fetchall())
        else:
            n = int(limit or cfg.funnel.get("daily_finalists", 40)) * 2
            min_score = float(cfg.funnel.get("min_score_to_curate", 3.5))
            rows = conn.execute(
                "SELECT c.id, c.canonical_url, c.title FROM clusters c "
                "LEFT JOIN articles a ON a.cluster_id = c.id "
                "WHERE c.score >= ? AND a.cluster_id IS NULL "
                "ORDER BY c.score DESC LIMIT ?",
                (min_score, n),
            ).fetchall()
        if not rows:
            print("no finalists need fetching")
            return 0

        stats = {"fetched": 0, "paywalled": 0, "failed": 0, "skipped": 0}
        client = PoliteClient(cfg, conn)
        try:
            for c in rows:
                source_url = c["canonical_url"]
                if not source_url:
                    with db_mod.write_tx(conn):
                        conn.execute(
                            "INSERT OR REPLACE INTO articles(cluster_id, "
                            "source_url, read_url, fetch_status) "
                            "VALUES(?,?,?,'skipped')",
                            (c["id"], "", ""),
                        )
                    stats["skipped"] += 1
                    continue

                res = client.fetch(source_url, conditional=False)
                text, meta = (None, {})
                if res.status == 200 and res.content:
                    text, meta = _extract(res.content, source_url)

                paywalled = _looks_paywalled(cfg, source_url, text)
                read_url, read_kind, archive_url = source_url, None, None

                if paywalled:
                    primary = _primary_surface(conn, c["id"])
                    if primary and primary != source_url:
                        read_url, read_kind = primary, "primary"
                        r2 = client.fetch(primary, conditional=False)
                        if r2.status == 200 and r2.content:
                            t2, m2 = _extract(r2.content, primary)
                            if t2 and len(t2.split()) > len((text or "").split()):
                                text, meta = t2, m2
                    elif _is_medium(source_url):
                        read_url = "https://freedium.cfd/%s" % source_url
                        read_kind = "freedium"
                    else:
                        read_kind = "canonical-fallback"
                        if cfg.paywall.get("allow_archive_today"):
                            # INTERNAL ONLY — excluded from all public output.
                            archive_url = "https://archive.ph/newest/%s" % source_url
                elif text:
                    read_kind = "publication-free"

                status = "failed" if (res.status != 200 and not text) else (
                    "paywalled" if paywalled else "ok"
                )
                words = len((text or "").split())
                excerpt = " ".join((text or "").split()[:70])
                with db_mod.write_tx(conn):
                    conn.execute(
                        "INSERT OR REPLACE INTO articles(cluster_id, source_url, "
                        "read_url, read_kind, paywalled, archive_url, "
                        "extracted_at, word_count, text, excerpt, lang, "
                        "fetch_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            c["id"], source_url, read_url, read_kind,
                            int(paywalled), archive_url, _now_iso(), words,
                            text, excerpt, (meta or {}).get("lang"), status,
                        ),
                    )
                if status == "failed":
                    stats["failed"] += 1
                elif paywalled:
                    stats["paywalled"] += 1
                else:
                    stats["fetched"] += 1
        finally:
            client.close()

        db_mod.checkpoint(conn)
        msg = (
            "fetch: %(fetched)d ok, %(paywalled)d paywalled, %(failed)d failed, "
            "%(skipped)d skipped" % stats
        )
        print("%s (%.1fs)" % (msg, time.time() - started))
        db_mod.log_health(conn, "fetch", "info", msg, json.dumps(stats))
        cfg.write_last_run("fetch", stats)
        return 0
    finally:
        conn.close()
