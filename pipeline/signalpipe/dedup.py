"""Two-stage conservative clustering: one underlying story = one cluster.

Stage 1 — exact identity via canonical URL (high precision, zero false merges).
Stage 2 — near-dup via title token-set similarity, gated by a time window and
domain constraint: same registered domain merges at a lower threshold; merging
across different domains (the "NYT and Reuters both covered X" case) requires
near-exact title similarity. Default to UNDER-merging: a duplicate listing is a
minor annoyance, an over-merge is a visible trust-killing bug. Merges are
audited via clusters.merge_reason.

The LLM is never used here — clustering runs on thousands of items pre-funnel
and must stay deterministic.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import sqlite3
from typing import Optional, Set
from urllib.parse import urlsplit

from .canonical import registered_domain

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "how", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "this", "to", "was", "what", "when", "why", "will", "with", "you",
    "your", "new", "via", "show", "ask", "hn",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def title_tokens(title: str) -> Set[str]:
    return {
        t for t in _TOKEN_RE.findall((title or "").lower())
        if t not in _STOPWORDS and len(t) > 1
    }


def title_key(title: str) -> str:
    """Stable normalized key stored on clusters for candidate prefiltering."""
    return " ".join(sorted(title_tokens(title)))


def story_id(canonical_url: Optional[str], title_key_val: str) -> str:
    """Stable content id for cross-edition de-duplication and the publication
    ledger. Keyed on the canonical URL's registered domain + path when present
    (so it survives a cluster being recreated after it ages out of the dedup
    window); falls back to the title key for discussion-only clusters. Set once
    at cluster creation; never churned, so a published story keeps its id."""
    if canonical_url:
        parts = urlsplit(canonical_url)
        host = registered_domain(canonical_url) or parts.netloc
        basis = host + "|" + (parts.path or "/").rstrip("/")
        if parts.query:  # canonicalize() already stripped tracking params
            basis += "?" + parts.query
    else:
        basis = "titlekey|" + (title_key_val or "")
    return "s_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / float(len(a | b))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def assign_cluster(
    conn: sqlite3.Connection,
    title: str,
    canonical_url: Optional[str],
    published_at: Optional[str],
    cfg_dedup: dict,
) -> int:
    """Find or create the cluster for an incoming item. Caller owns the tx."""
    now = _now_iso()

    # Stage 1: exact canonical-URL identity.
    if canonical_url:
        row = conn.execute(
            "SELECT id FROM clusters WHERE canonical_url=?", (canonical_url,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE clusters SET last_seen=? WHERE id=?", (now, row["id"])
            )
            return int(row["id"])

    # Stage 2: gated near-dup by title similarity within the time window.
    window_h = int(cfg_dedup.get("near_dup_window_hours", 48))
    thr_same = float(cfg_dedup.get("title_jaccard_same_domain", 0.80))
    thr_cross = float(cfg_dedup.get("title_jaccard_cross_domain", 0.92))
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=window_h)
    ).isoformat()

    tokens = title_tokens(title)
    item_domain = registered_domain(canonical_url) if canonical_url else None

    best_id = None
    best_sim = 0.0
    best_reason = None
    if tokens:
        rows = conn.execute(
            "SELECT id, canonical_url, title_key FROM clusters WHERE last_seen >= ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            sim = jaccard(tokens, set((row["title_key"] or "").split()))
            if sim < min(thr_same, thr_cross):
                continue
            c_domain = (
                registered_domain(row["canonical_url"])
                if row["canonical_url"]
                else None
            )
            same_domain = bool(item_domain and c_domain and item_domain == c_domain)
            threshold = thr_same if same_domain else thr_cross
            if sim >= threshold and sim > best_sim:
                best_sim = sim
                best_id = int(row["id"])
                best_reason = "title-jaccard %.2f (%s-domain)" % (
                    sim,
                    "same" if same_domain else "cross",
                )

    if best_id is not None:
        conn.execute(
            "UPDATE clusters SET last_seen=?, merge_reason=COALESCE(merge_reason,?) "
            "WHERE id=?",
            (now, best_reason, best_id),
        )
        # Attach a canonical URL if the cluster gained one (e.g. matched a
        # self-post cluster that now has an article link).
        if canonical_url:
            conn.execute(
                "UPDATE clusters SET canonical_url=COALESCE(canonical_url, ?) "
                "WHERE id=?",
                (canonical_url, best_id),
            )
        return best_id

    tk = title_key(title)
    cur = conn.execute(
        "INSERT INTO clusters(canonical_url, title, title_key, story_id, "
        "first_seen, last_seen, surface_count) VALUES(?,?,?,?,?,?,0)",
        (canonical_url, title, tk, story_id(canonical_url, tk),
         published_at or now, now),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def refresh_surface_counts(conn: sqlite3.Connection) -> None:
    """Recompute clusters.surface_count from the surfaces table."""
    conn.execute(
        "UPDATE clusters SET surface_count = ("
        "  SELECT COUNT(*) FROM surfaces WHERE surfaces.cluster_id = clusters.id"
        ")"
    )
