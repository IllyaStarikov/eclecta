"""One-time historical backfill: recover a polished month of editions.

The live pipeline only began curating June 9, but raw clusters span the prior
month. This module (driven via `python3 -m signalpipe backfill ...`, ALWAYS
against a COPY DB via --config — never the live one) does two things:

  fetch   re-fetch article text for the top-scored clusters per day that lack
          it (May was ingested as headlines and never fetched).

  curate  an ALL-OPUS, hindsight-aware curation over each day's top-scored
          clusters that now have text, stamping `curated_at = first_seen` so
          the existing curated_at-windowed digest engine (digest.py) lands each
          story on its real day. One Opus call per item (SYSTEM_CURATE judges
          AND writes — no local arena, no judge/write split: it's all Opus).

  merge   fold the backfill's new rows (re-fetched text, curations, digests,
          de-dup ledger) from the copy back into the LIVE DB in one short
          transaction, so /stats and future de-dup are durable. INSERT OR
          IGNORE never clobbers a live curation/digest the worker wrote.

Hindsight lives in three layers: selection by deterministic score (what
demonstrably mattered that day), a retrospective prompt prefix, and — above all
— the weekly/monthly synthesis, where Opus sees the whole period at once.

Scoring is recency-decayed (18h half-life), so May's absolute scores are tiny
and NOT comparable to June's; we therefore rank WITHIN each day and ignore the
global min_score_to_curate gate.
"""

from __future__ import annotations

import datetime
import json
import time
from typing import List

from . import curate as curate_mod
from . import db as db_mod
from . import topics
from .llm import LLMError, SpendCapExceeded, adapter
from .llm.schemas import CURATION_SCHEMA, SYSTEM_CURATE

# tier="write" routes to the subscription backend (not local); model_override
# then forces Opus. The cap_kind stays "daily" so backfill spend is gated by
# the (raised) daily cap of the COPY's isolated spend ledger.
OPUS_TIER = "write"

_HINDSIGHT = (
    "RETROSPECTIVE PASS: you are curating this story weeks after it broke, with "
    "the benefit of hindsight. Judge its LASTING significance for this reader, "
    "not just its day-one novelty; be ruthless about items that looked "
    "important briefly but led nowhere.\n\n"
)


def _opus_model(cfg) -> str:
    return cfg.model_for("digest")  # claude-opus-4-8


def _days(since: str, until: str):
    d = datetime.date.fromisoformat(since)
    end = datetime.date.fromisoformat(until)
    while d < end:
        yield d
        d += datetime.timedelta(days=1)


# --------------------------------------------------------------------------
# fetch — re-fetch missing text for the top-scored clusters per day
# --------------------------------------------------------------------------

def select_refetch_ids(conn, since: str, until: str, top_n: int) -> List[int]:
    ids: List[int] = []
    for d in _days(since, until):
        ds, de = d.isoformat(), (d + datetime.timedelta(days=1)).isoformat()
        rows = conn.execute(
            "SELECT c.id FROM clusters c "
            "LEFT JOIN articles a ON a.cluster_id = c.id "
            "WHERE c.first_seen >= ? AND c.first_seen < ? "
            "AND c.canonical_url IS NOT NULL "
            "AND (a.cluster_id IS NULL OR a.text IS NULL OR a.text = '') "
            "ORDER BY c.score DESC LIMIT ?",
            (ds, de, top_n),
        ).fetchall()
        ids.extend(r["id"] for r in rows)
    return ids


def fetch(cfg, since: str, until: str, top_n: int = 40) -> int:
    from . import fetch_article

    conn = db_mod.connect_ro(cfg.db_path)
    try:
        ids = select_refetch_ids(conn, since, until, top_n)
    finally:
        conn.close()
    print("backfill fetch: %d clusters need text across %s..%s (top-%d/day)"
          % (len(ids), since, until, top_n))
    if not ids:
        return 0
    return fetch_article.run(cfg, cluster_ids=ids)


# --------------------------------------------------------------------------
# curate — all-Opus hindsight pass, stamped to first_seen
# --------------------------------------------------------------------------

def _persist(conn, c, out: dict, model: str, when_iso: str) -> bool:
    """Write one curation, stamped curated_at=first_seen, taxonomy set.
    Returns True if skipped."""
    skip = bool(out.get("skip"))
    tax = topics.match_taxonomy(c["title"] or "", out.get("channels") or [])
    with db_mod.write_tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO curations(cluster_id, status) "
            "VALUES(?, 'pending')", (c["id"],))
        conn.execute(
            "UPDATE curations SET status=?, tier_used='opus-backfill', "
            "backend_used='subscription', model_used=?, relevance_score=?, "
            "why_it_matters=?, notes=?, summary=?, channels=?, novelty=?, "
            "audience=?, skip=?, skip_reason=?, category=?, subcategories=?, "
            "curated_at=? WHERE cluster_id=?",
            (
                "skipped" if skip else "done", model,
                int(out.get("relevance_score") or 0),
                out.get("why_it_matters"),
                json.dumps(out.get("notes") or []),
                out.get("summary"),
                json.dumps(out.get("channels") or []),
                out.get("novelty"), out.get("audience"),
                int(skip), out.get("skip_reason"),
                tax["category"], json.dumps(tax["subcategories"]),
                when_iso, c["id"],
            ),
        )
    return skip


def curate(cfg, since: str, until: str, top_n: int = 30,
           dry_run: bool = False) -> int:
    started = time.time()
    model = _opus_model(cfg)
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        stats = {"done": 0, "skipped": 0, "failed": 0, "days": 0,
                 "candidates": 0}
        for d in _days(since, until):
            ds, de = d.isoformat(), (d + datetime.timedelta(days=1)).isoformat()
            rows = conn.execute(
                "SELECT c.id, c.title, c.canonical_url, c.first_seen, c.score, "
                "c.surface_count FROM clusters c "
                "JOIN articles a ON a.cluster_id = c.id "
                "LEFT JOIN curations cu ON cu.cluster_id = c.id "
                "WHERE c.first_seen >= ? AND c.first_seen < ? "
                "AND a.text IS NOT NULL AND a.text != '' "
                "AND (a.lang IS NULL OR a.lang = '' OR a.lang = 'en') "
                "AND cu.cluster_id IS NULL "
                "ORDER BY c.score DESC LIMIT ?",
                (ds, de, top_n),
            ).fetchall()
            if not rows:
                continue
            stats["days"] += 1
            stats["candidates"] += len(rows)
            if dry_run:
                print("  %s: %d candidates (top score %.1f)"
                      % (ds, len(rows), rows[0]["score"] or 0))
                continue
            # Stamp every item to first_seen midnight; the edition for the next
            # weekday (window [D, D+1)) then picks it up.
            when_iso = d.isoformat() + "T00:00:00+00:00"
            for c in rows:
                prompt = curate_mod._build_prompt(conn, c)
                try:
                    out = adapter.complete(
                        OPUS_TIER, SYSTEM_CURATE, _HINDSIGHT + prompt,
                        CURATION_SCHEMA, cfg=cfg, conn=conn,
                        model_override=model, effort="low")
                    stats["skipped" if _persist(conn, c, out, model, when_iso)
                          else "done"] += 1
                except SpendCapExceeded as e:
                    print("STOP: %s" % e)
                    db_mod.log_health(conn, "backfill", "warn", str(e))
                    return 1
                except LLMError as e:
                    stats["failed"] += 1
                    db_mod.log_health(conn, "backfill", "warn",
                                      "item %d: %s" % (c["id"], str(e)[:200]))
            print("  %s: done=%d skipped=%d failed=%d (cumulative)"
                  % (ds, stats["done"], stats["skipped"], stats["failed"]))

        msg = ("backfill curate: %(done)d done, %(skipped)d skipped, "
               "%(failed)d failed over %(days)d days (%(candidates)d candidates)"
               % stats)
        print("%s (%.1fs)" % (msg, time.time() - started))
        if not dry_run:
            db_mod.log_health(conn, "backfill", "info", msg, json.dumps(stats))
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# merge — fold the copy's new rows back into the LIVE DB (one short tx)
# --------------------------------------------------------------------------

def merge(cfg, src_db: str) -> int:
    """Merge backfill rows from the copy (src_db) into the LIVE cfg.db_path.
    INSERT OR IGNORE never overwrites a curation/digest the live worker wrote;
    re-fetched text (INSERT OR REPLACE) is bounded to backfill-curated clusters
    so recent live articles are untouched. ATTACH lives outside the write tx
    (SQLite requirement); the inserts are one short transaction."""
    live = db_mod.connect_rw(cfg.db_path)
    counts = {}
    try:
        live.execute("ATTACH DATABASE ? AS bf", (src_db,))
        try:
            with db_mod.write_tx(live):
                cur = live.execute(
                    "INSERT OR REPLACE INTO articles "
                    "SELECT a.* FROM bf.articles a "
                    "JOIN bf.curations cu ON cu.cluster_id = a.cluster_id "
                    "WHERE cu.tier_used='opus-backfill' "
                    "AND a.text IS NOT NULL AND a.text != ''")
                counts["articles"] = cur.rowcount
                cur = live.execute(
                    "INSERT OR IGNORE INTO curations "
                    "SELECT * FROM bf.curations WHERE tier_used='opus-backfill'")
                counts["curations"] = cur.rowcount
                cols = ("kind,period_key,window_start,window_end,generated_at,"
                        "model_used,title,blurb,body_md,body_html,cluster_ids,"
                        "staged_path,promoted,published_at,publish_error,"
                        "cost_usd")
                cur = live.execute(
                    "INSERT OR IGNORE INTO digests(%s) "
                    "SELECT %s FROM bf.digests" % (cols, cols))
                counts["digests"] = cur.rowcount
                cur = live.execute(
                    "INSERT OR IGNORE INTO published_ledger "
                    "SELECT * FROM bf.published_ledger")
                counts["ledger"] = cur.rowcount
        finally:
            live.execute("DETACH DATABASE bf")
        print("backfill merge -> live: %s" % counts)
        db_mod.log_health(live, "backfill", "info",
                          "merge -> live: %s" % json.dumps(counts))
        return 0
    finally:
        live.close()
