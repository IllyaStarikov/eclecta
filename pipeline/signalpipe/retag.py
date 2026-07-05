"""Backfill the v2 taxonomy onto historical curations (deterministic).

Re-runnable and idempotent: recomputes a primary `category` + `subcategories`
for every curation from the cluster title and the legacy `channels[]`, via
`topics.match_taxonomy` (the same lexicon the site uses in src/lib/taxonomy.ts).
No LLM, no spend. With --dry-run it prints the resulting distribution and a few
examples and writes nothing.

    python3 -m signalpipe retag --dry-run
    python3 -m signalpipe retag
"""

from __future__ import annotations

import datetime
import json
from collections import Counter
from typing import Optional

from . import topics
from .config import Config
from .db import connect_rw, write_tx


def run(cfg: Config, dry_run: bool = False, limit: Optional[int] = None) -> int:
    conn = connect_rw(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT cu.cluster_id AS cid, c.title AS title, cu.channels AS channels "
            "FROM curations cu JOIN clusters c ON c.id = cu.cluster_id"
        ).fetchall()
        if limit:
            rows = rows[:limit]

        updates = []
        cat_dist: Counter = Counter()
        sub_dist: Counter = Counter()
        for r in rows:
            try:
                chans = json.loads(r["channels"] or "[]")
            except (ValueError, TypeError):
                chans = []
            tax = topics.match_taxonomy(r["title"] or "", chans)
            updates.append(
                (tax["category"], json.dumps(tax["subcategories"]), r["cid"])
            )
            cat_dist[tax["category"]] += 1
            for s in tax["subcategories"]:
                sub_dist["%s/%s" % (tax["category"], s)] += 1

        print("retag: %d curations" % len(updates))
        print("by category:")
        for cat, n in cat_dist.most_common():
            print("  %-10s %d" % (cat, n))

        if dry_run:
            print("top subcategories:")
            for sub, n in sub_dist.most_common(12):
                print("  %-22s %d" % (sub, n))
            print("examples:")
            for cat, subs, cid in updates[:8]:
                print("  #%-7s -> %-9s %s" % (cid, cat, subs))
            print("(dry-run: nothing written)")
            return 0

        with write_tx(conn) as c:
            c.executemany(
                "UPDATE curations SET category=?, subcategories=? "
                "WHERE cluster_id=?",
                updates,
            )
        print("retag: wrote %d rows" % len(updates))

        # Seed the de-dup ledger from already-published digests so future
        # editions of the same cadence don't repeat their stories.
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ledger_rows = []
        for d in conn.execute(
            "SELECT kind, period_key, cluster_ids FROM digests "
            "WHERE cluster_ids IS NOT NULL"
        ).fetchall():
            try:
                cids = json.loads(d["cluster_ids"] or "[]")
            except (ValueError, TypeError):
                cids = []
            for cid in cids:
                row = conn.execute(
                    "SELECT story_id FROM clusters WHERE id=?", (cid,)
                ).fetchone()
                if row and row["story_id"]:
                    ledger_rows.append(
                        (row["story_id"], d["kind"], d["period_key"], cid, now)
                    )
        if ledger_rows:
            with write_tx(conn) as c:
                c.executemany(
                    "INSERT OR IGNORE INTO published_ledger"
                    "(story_id, surface, edition_key, cluster_id, first_at) "
                    "VALUES(?,?,?,?,?)",
                    ledger_rows,
                )
        print("retag: seeded ledger with %d edition-story rows" % len(ledger_rows))
        return 0
    finally:
        conn.close()
