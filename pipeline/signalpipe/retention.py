"""DB retention: the wire forgets what it never kept.

signal.db grows without bound otherwise (~5k items/day). The rules:
  - uncurated clusters older than 30 days are pruned (their items fall back
    to cluster_id NULL and are swept as orphans); anything curated, published,
    or ledgered is kept forever
  - fetch_cache expires after 21 days; info-level health rows after 90
    (error rows are kept — they are the incident record)
  - VACUUM monthly (first Sunday), after a wal checkpoint

Runs as the Sunday 10:00 worker job (after the 09:00 backup, so every
destructive prune is preceded by a fresh VACUUM INTO snapshot) and as
`python3 -m signalpipe retention [--dry-run]`.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Dict

from . import db as db_mod


def _cutoff(days: int) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=days)
    ).isoformat()


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    cut30, cut21, cut90 = _cutoff(30), _cutoff(21), _cutoff(90)
    q = {
        "clusters_prunable": (
            "SELECT COUNT(*) FROM clusters WHERE last_seen < ? "
            "AND id NOT IN (SELECT cluster_id FROM curations "
            "               WHERE status='done') "
            "AND (story_id IS NULL OR story_id NOT IN "
            "     (SELECT story_id FROM published_ledger))", (cut30,)),
        "items_orphaned": (
            "SELECT COUNT(*) FROM items WHERE cluster_id IS NULL "
            "AND ingested_at < ?", (cut30,)),
        "fetch_cache_stale": (
            "SELECT COUNT(*) FROM fetch_cache WHERE fetched_at < ?", (cut21,)),
        "health_info_old": (
            "SELECT COUNT(*) FROM health WHERE ts < ? AND level='info'",
            (cut90,)),
    }
    return {k: conn.execute(sql, args).fetchone()[0]
            for k, (sql, args) in q.items()}


def run(cfg, dry_run: bool = False, vacuum: bool = False) -> Dict[str, int]:
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        before = counts(conn)
        if dry_run:
            print("[retention] dry-run: %s" % json.dumps(before))
            return before

        cut30, cut21, cut90 = _cutoff(30), _cutoff(21), _cutoff(90)
        with db_mod.write_tx(conn):
            # FKs cascade surfaces/articles/curations; items go SET NULL.
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "DELETE FROM clusters WHERE last_seen < ? "
                "AND id NOT IN (SELECT cluster_id FROM curations "
                "               WHERE status='done') "
                "AND (story_id IS NULL OR story_id NOT IN "
                "     (SELECT story_id FROM published_ledger))", (cut30,))
            conn.execute(
                "DELETE FROM items WHERE cluster_id IS NULL "
                "AND ingested_at < ?", (cut30,))
            conn.execute(
                "DELETE FROM fetch_cache WHERE fetched_at < ?", (cut21,))
            conn.execute(
                "DELETE FROM health WHERE ts < ? AND level='info'", (cut90,))

        if vacuum:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.isolation_level = None
            conn.execute("VACUUM")

        db_mod.log_health(conn, "retention", "info", json.dumps(before))
        print("[retention] pruned: %s%s"
              % (json.dumps(before), " + VACUUM" if vacuum else ""))
        return before
    finally:
        conn.close()
