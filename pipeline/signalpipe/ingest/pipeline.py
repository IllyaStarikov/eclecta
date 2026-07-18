"""Ingest orchestrator: poll due sources -> normalize -> canonicalize ->
cluster -> store. One source failing never stalls the cycle; consecutive
failures increment sources.error_count (auto-disable surfaces in health).
"""

from __future__ import annotations

import datetime
import json
import sys
import time
from typing import List, Optional

from .. import db as db_mod
from ..canonical import canonicalize, is_aggregator
from ..dedup import assign_cluster, refresh_surface_counts
from . import arxiv as arxiv_mod
from . import bluesky as bluesky_mod
from . import devto as devto_mod
from . import gdelt as gdelt_mod
from . import googlenews as googlenews_mod
from . import hn as hn_mod
from . import lobsters as lobsters_mod
from . import mastodon as mastodon_mod
from . import reddit as reddit_mod
from . import rss as rss_mod
from . import sources_misc
from . import stackexchange as stackexchange_mod
from . import wikipedia_events as wikipedia_events_mod
from .fetch_http import PoliteClient

AUTO_DISABLE_ERRORS = 10  # consecutive failures before a source is disabled


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _fetch_for_source(client: PoliteClient, source_row, cfg) -> List[dict]:
    slug = source_row["slug"]
    stype = source_row["type"]
    if slug == "hacker-news":
        return hn_mod.fetch_items(client, source_row, pages=int(cfg.ingest.get("hn_pages", 2)))
    if slug == "lobsters":
        return lobsters_mod.fetch_items(client, source_row, pages=int(cfg.ingest.get("lobsters_pages", 2)))
    if slug.startswith("reddit-"):
        return reddit_mod.fetch_items(client, source_row, mode=cfg.ingest.get("reddit_mode", "public_json"))
    if slug == "hf-daily-papers":
        return sources_misc.fetch_hf_daily_papers(client, source_row)
    if slug == "github-trending":
        return sources_misc.fetch_github_trending(client, source_row)
    if slug.startswith("arxiv-"):
        return arxiv_mod.fetch_items(client, source_row)
    if slug.startswith("mastodon-"):
        return mastodon_mod.fetch_items(
            client, source_row,
            instances=cfg.ingest.get("mastodon_instances", ["mastodon.social"]))
    if slug.startswith("bsky-"):
        return bluesky_mod.fetch_items(client, source_row)
    if slug.startswith("gnews-"):
        return googlenews_mod.fetch_items(
            client, source_row,
            resolve_top=int(cfg.ingest.get("gnews_resolve_top", 25)))
    if slug.startswith("wiki-"):
        return wikipedia_events_mod.fetch_items(
            client, source_row, days=int(cfg.ingest.get("wiki_events_days", 2)))
    if slug.startswith("gdelt-"):
        return gdelt_mod.fetch_items(
            client, source_row, queries=cfg.ingest.get("gdelt_queries"))
    if slug.startswith("devto-"):
        return devto_mod.fetch_items(client, source_row)
    if slug.startswith("stackoverflow-"):
        return stackexchange_mod.fetch_items(client, source_row)
    if stype in ("rss", "atom"):
        return rss_mod.fetch_feed_items(client, source_row)
    raise RuntimeError("no fetcher for type=%s slug=%s" % (stype, slug))


def _store_items(conn, source_row, items: List[dict], cfg) -> dict:
    """Insert/update items, assign clusters, upsert surfaces. Returns stats."""
    n_new, n_upd = 0, 0
    now = _now_iso()
    for it in items:
        canon = None
        if it["raw_url"] and not is_aggregator(it["raw_url"]):
            canon = canonicalize(it["raw_url"])
        elif it["raw_url"]:
            # Aggregator self-link (Ask HN, reddit self-post): identity is the
            # discussion URL itself.
            canon = canonicalize(it["raw_url"])

        existing = conn.execute(
            "SELECT id, cluster_id FROM items WHERE source_id=? AND guid=?",
            (source_row["id"], it["guid"]),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE items SET points=?, comments=? WHERE id=?",
                (it.get("points"), it.get("comments"), existing["id"]),
            )
            cluster_id = existing["cluster_id"]
            n_upd += 1
        else:
            cluster_id = assign_cluster(
                conn, it["title"], canon, it.get("published_at"), cfg.dedup
            )
            conn.execute(
                "INSERT INTO items(cluster_id, source_id, guid, raw_url, "
                "canonical_url, title, author, published_at, ingested_at, "
                "points, comments, extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cluster_id,
                    source_row["id"],
                    it["guid"],
                    it["raw_url"],
                    canon,
                    it["title"],
                    it.get("author"),
                    it.get("published_at"),
                    now,
                    it.get("points"),
                    it.get("comments"),
                    json.dumps(it.get("extra") or {}),
                ),
            )
            n_new += 1

        if cluster_id is not None:
            discussion = (it.get("extra") or {}).get("discussion_url") or it["raw_url"]
            conn.execute(
                "INSERT INTO surfaces(cluster_id, source_id, url, points, "
                "comments, seen_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(cluster_id, source_id) DO UPDATE SET "
                "points=excluded.points, comments=excluded.comments, "
                "seen_at=excluded.seen_at",
                (
                    cluster_id,
                    source_row["id"],
                    discussion,
                    it.get("points"),
                    it.get("comments"),
                    now,
                ),
            )
    return {"new": n_new, "updated": n_upd}


def run(cfg, only: Optional[str] = None, limit: Optional[int] = None) -> int:
    started = time.time()
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        if only:
            rows = conn.execute(
                "SELECT * FROM sources WHERE slug=?", (only,)
            ).fetchall()
            if not rows:
                print("unknown source slug %r — run `sources seed` first?" % only,
                      file=sys.stderr)
                return 1
        else:
            # Due = never fetched, or last_fetch older than its cadence.
            rows = conn.execute(
                "SELECT * FROM sources WHERE enabled=1 AND (last_fetch IS NULL "
                "OR datetime(last_fetch, '+' || cadence_min || ' minutes') "
                "<= datetime('now')) ORDER BY last_fetch IS NOT NULL, last_fetch"
            ).fetchall()
        if limit:
            rows = rows[:limit]
        if not rows:
            print("no sources due")
            return 0

        totals = {"sources": 0, "errors": 0, "new": 0, "updated": 0}
        client = PoliteClient(cfg, conn)
        try:
            for src in rows:
                # Fetch AND store inside the per-source guard: a store-phase
                # failure (malformed connector item violating NOT NULL,
                # 'database is locked' from a concurrent manual writer) must
                # be recorded on that source and never abort the rest of the
                # cycle. write_tx rolls back its partial work on exception
                # before the error-handling transaction below begins.
                try:
                    items = _fetch_for_source(client, src, cfg)
                    with db_mod.write_tx(conn):
                        stats = _store_items(conn, src, items, cfg)
                        conn.execute(
                            "UPDATE sources SET last_fetch=?, last_error=NULL, "
                            "error_count=0, "
                            "verified_at=COALESCE(verified_at, ?) "
                            "WHERE id=?",
                            (_now_iso(), _now_iso(), src["id"]),
                        )
                except Exception as e:  # noqa: BLE001 — per-source isolation
                    totals["errors"] += 1
                    err_count = int(src["error_count"]) + 1
                    disable = err_count >= AUTO_DISABLE_ERRORS
                    with db_mod.write_tx(conn):
                        conn.execute(
                            "UPDATE sources SET last_error=?, error_count=?, "
                            "enabled=? WHERE id=?",
                            (str(e)[:300], err_count,
                             0 if disable else src["enabled"], src["id"]),
                        )
                    if disable:
                        db_mod.log_health(
                            conn, "ingest", "warn",
                            "auto-disabled %s after %d consecutive errors"
                            % (src["slug"], err_count),
                        )
                    continue

                totals["sources"] += 1
                totals["new"] += stats["new"]
                totals["updated"] += stats["updated"]
        finally:
            client.close()

        with db_mod.write_tx(conn):
            refresh_surface_counts(conn)
        db_mod.checkpoint(conn)

        dur = time.time() - started
        msg = (
            "ingest: %(sources)d sources ok, %(errors)d errors, "
            "%(new)d new items, %(updated)d updated" % totals
        )
        print("%s (%.1fs)" % (msg, dur))
        db_mod.log_health(conn, "ingest", "info", msg, json.dumps(totals))
        _fp = cfg.config_fingerprint()
        db_mod.record_run(conn, "ingest", _fp["hash"], json.dumps(totals),
                          json.dumps(_fp["tunables"]))
        cfg.write_last_run("ingest", totals)
        return 0
    finally:
        conn.close()
