"""Deterministic scoring: rank clusters 0..10 with NO LLM involvement.

score = w_consensus·consensus + w_engagement·engagement + w_reputation·rep
      + w_recency·recency + w_topic·topic_match            (weights sum ~1)

- consensus:   #independent surfaces, saturating (the homegrown cross-source
               "trending consensus" signal — no public API offers this).
- engagement:  log-scaled max points + comments across surfaces.
- reputation:  best source reputation among surfaces (0..1.5 → capped).
- recency:     half-life decay on last_seen.
- topic_match: predefined channel + taxonomy lexicon match on the title
               (0.7 for a channel hit, 1.0 when a taxonomy subcategory term
               also hits — no per-user personalization).

Re-scores everything inside the rolling window each run (a story that peaks
on day 2 re-enters the funnel; curation idempotency lives in curations.status,
not here).
"""

from __future__ import annotations

import datetime
import json
import math
import time
from typing import Dict

from . import db as db_mod
from . import topics as topics_mod


def _consensus(n_surfaces: int) -> float:
    if n_surfaces <= 1:
        return 0.0
    return 1.0 - math.exp(-(n_surfaces - 1) / 2.0)


def _engagement(points, comments) -> float:
    p = max(0, points or 0)
    c = max(0, comments or 0)
    p_score = min(1.0, math.log10(1 + p) / 3.0)   # 1000 points -> 1.0
    c_score = min(1.0, math.log10(1 + c) / 2.7)   # ~500 comments -> 1.0
    return 0.7 * p_score + 0.3 * c_score


def _recency(last_seen_iso: str, halflife_h: float, now_ts: float) -> float:
    try:
        dt = datetime.datetime.fromisoformat(last_seen_iso.replace("Z", "+00:00"))
        age_h = max(0.0, (now_ts - dt.timestamp()) / 3600.0)
    except (ValueError, AttributeError):
        return 0.5
    return math.exp(-age_h * math.log(2) / max(1.0, halflife_h))


MIN_LATIN_RATIO = 0.5


def latin_ratio(title: str) -> float:
    """Share of a title's letters that are ASCII. Deterministic language
    gate: a mostly-CJK/Cyrillic title can still clear the score bar on
    engagement+recency alone, then burn a paid curation call only for the
    model to skip it as non-English. Titles with no letters at all (numbers,
    symbols) carry no language signal and pass."""
    letters = [ch for ch in (title or "") if ch.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for ch in letters if ch.isascii()) / len(letters)


def run(cfg, show: int = 20) -> int:
    started = time.time()
    w = cfg.score_weights
    window_h = int(cfg.funnel.get("score_window_hours", 72))
    topics_data = topics_mod.build_or_load(cfg)

    conn = db_mod.connect_rw(cfg.db_path)
    try:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=window_h)
        ).isoformat()
        clusters = conn.execute(
            "SELECT c.id, c.title, c.canonical_url, c.last_seen, c.surface_count "
            "FROM clusters c WHERE c.last_seen >= ?",
            (cutoff,),
        ).fetchall()

        now_ts = time.time()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Phase 1: compute all scores OUTSIDE any transaction (reads only) —
        # holding the writer lock across thousands of SELECTs would starve
        # the ingest job, and one bad row would roll back the whole batch.
        updates = []
        for c in clusters:
            if latin_ratio(c["title"]) < MIN_LATIN_RATIO:
                # Non-English (non-Latin script) — never reaches curation.
                updates.append((0.0, now_iso, c["id"]))
                continue
            agg = conn.execute(
                "SELECT MAX(s.points) AS points, MAX(s.comments) AS comments, "
                "MAX(src.reputation) AS rep "
                "FROM surfaces s JOIN sources src ON src.id = s.source_id "
                "WHERE s.cluster_id = ?",
                (c["id"],),
            ).fetchone()
            rep = min(1.0, (agg["rep"] or 1.0) / 1.5)
            channels = topics_mod.match_channels(c["title"], topics_data)
            topic = 0.0
            if channels:
                topic = 0.7
                # Specific taxonomy-subcategory hit = strong topical fit.
                tax = topics_mod.match_taxonomy(c["title"], sorted(channels))
                if tax["subcategories"]:
                    topic = 1.0

            score01 = (
                float(w.get("consensus", 0.3)) * _consensus(c["surface_count"])
                + float(w.get("engagement", 0.25)) * _engagement(agg["points"], agg["comments"])
                + float(w.get("reputation", 0.15)) * rep
                + float(w.get("recency", 0.15)) * _recency(
                    c["last_seen"], float(w.get("recency_halflife_hours", 18)), now_ts
                )
                + float(w.get("topic_match", 0.15)) * topic
            )
            updates.append((round(score01 * 10.0, 3), now_iso, c["id"]))

        # Phase 2: one short write transaction per batch (writer lock held
        # for milliseconds, partial progress survives a late failure).
        n = 0
        BATCH = 500
        for i in range(0, len(updates), BATCH):
            chunk = updates[i : i + BATCH]
            with db_mod.write_tx(conn):
                conn.executemany(
                    "UPDATE clusters SET score=?, score_at=? WHERE id=?", chunk
                )
            n += len(chunk)
        db_mod.checkpoint(conn)

        stats: Dict[str, object] = {"scored": n, "window_hours": window_h}
        msg = "score: %d clusters scored (window %dh)" % (n, window_h)
        print("%s (%.1fs)" % (msg, time.time() - started))
        db_mod.log_health(conn, "score", "info", msg, json.dumps(stats))
        _fp = cfg.config_fingerprint()
        db_mod.record_run(conn, "score", _fp["hash"], json.dumps(stats),
                          json.dumps(_fp["tunables"]))
        cfg.write_last_run("score", stats)

        if show:
            rows = conn.execute(
                "SELECT id, score, surface_count, title FROM clusters "
                "WHERE score IS NOT NULL ORDER BY score DESC LIMIT ?",
                (show,),
            ).fetchall()
            print("top %d:" % len(rows))
            for r in rows:
                print(
                    "  %5.2f  [%d surf]  #%d %s"
                    % (r["score"], r["surface_count"], r["id"], r["title"][:90])
                )
        return 0
    finally:
        conn.close()


def finalists(conn, cfg, limit=None):
    """Clusters due for curation: scored above threshold and either never
    curated OR failed >6h ago (transient LLM errors get retried at a bounded
    cadence instead of being silently dropped forever). Clusters whose
    fetched article has a detected non-English language are excluded before
    any LLM spend. The retry bound is a Python ISO timestamp — stored
    curated_at values use the 'T' separator, which compares wrong against
    SQLite's space-separated datetime('now')."""
    min_score = float(cfg.funnel.get("min_score_to_curate", 3.5))
    n = int(limit or cfg.funnel.get("daily_finalists", 40))
    retry_before = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=6)
    ).isoformat()
    return conn.execute(
        "SELECT c.* FROM clusters c "
        "LEFT JOIN curations cu ON cu.cluster_id = c.id "
        "WHERE c.score >= ? AND (cu.cluster_id IS NULL "
        "  OR (cu.status='failed' AND cu.curated_at < ?)) "
        "AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.cluster_id = c.id "
        "  AND a.lang IS NOT NULL AND a.lang <> '' AND a.lang <> 'en') "
        "ORDER BY c.score DESC LIMIT ?",
        (min_score, retry_before, n),
    ).fetchall()
