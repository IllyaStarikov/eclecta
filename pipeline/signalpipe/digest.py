"""Multi-cadence digests: Opus over the period's curated best.

Five kinds (daily/weekly/monthly/quarterly/yearly; see period.py for the
calendar authority). daily/weekly digest the period's curations directly;
monthly/quarterly/yearly are HIERARCHICAL — they read the lower-tier digest
bodies inside the period (monthly eats weeklies+dailies, quarterly eats
monthlies, yearly eats quarterlies) plus the period's top curations, which
keeps Opus context bounded no matter how busy the period was.

Output lands in:
  - digests row (kind, period_key, body_md + body_html) -> dashboard + site
  - staged markdown in the state dir (promote.py compatibility, weekly only
    in practice — promote stays weekly-only Ghost semantics)
  - the site repo via publish.publish_digest, gated by cfg site.push

Guardrail: the digest may cite ONLY read_url/source_url values; archive.*
links are refused at generation time (and again at promote time).
"""

from __future__ import annotations

import datetime
import json
import time
from typing import Any, Dict, List, Optional

from . import db as db_mod
from . import period as period_mod
from .llm import LLMError, SpendCapExceeded, UsageLimitExhausted, adapter
from .llm.schemas import DIGEST_SCHEMA, STYLE_FALLBACK, system_digest
from .publish import _ARCHIVE_RE, no_archive

# Which lower-tier digests each hierarchical kind consumes.
LOWER_TIERS = {
    "monthly": ("weekly", "daily"),
    "quarterly": ("monthly",),
    "yearly": ("quarterly",),
}

STYLE_DOC = "doc/digest-style.md"
EDITORIAL_DOC = "doc/editorial-policy.md"


def _load_style(cfg) -> str:
    """doc/digest-style.md (runtime copy or repo), else the baked-in
    fallback. The style workflow owns the file; we only read it."""
    for base_path in (cfg.repo_path(STYLE_DOC), cfg.blog_repo / STYLE_DOC):
        try:
            text = base_path.read_text().strip()
            if text:
                return text
        except OSError:
            continue
    return STYLE_FALLBACK


def _load_editorial(cfg) -> str:
    """doc/editorial-policy.md (the 'what to publish' guide), or "" when
    absent. Injected into the digest system prompt alongside the style guide."""
    for base_path in (cfg.repo_path(EDITORIAL_DOC), cfg.blog_repo / EDITORIAL_DOC):
        try:
            text = base_path.read_text().strip()
            if text:
                return text
        except OSError:
            continue
    return ""


def _gather(conn, since: str, until: str, min_relevance: int, max_items: int,
            kind: str, period_key: str):
    """Curations completed in the window (curated_at, not published_at —
    matches the rest of the pipeline's completion-time semantics).

    Cross-edition de-dup: a story that already ran in a PRIOR edition of this
    same cadence (its story_id is in the published_ledger under this kind with a
    different edition_key) is excluded, so a daily never repeats a previous
    daily. Same-key rows stay eligible so --force regeneration keeps its own
    stories. Weeklies+ may still synthesize their dailies — the filter is
    per-cadence, not global."""
    return conn.execute(
        "SELECT c.id, c.title, c.story_id, cu.relevance_score, cu.why_it_matters, "
        "cu.notes, cu.summary, cu.channels, cu.novelty, "
        "a.read_url, a.source_url "
        "FROM curations cu "
        "JOIN clusters c ON c.id = cu.cluster_id "
        "LEFT JOIN articles a ON a.cluster_id = cu.cluster_id "
        "WHERE cu.status='done' AND cu.skip=0 AND cu.relevance_score >= ? "
        "AND cu.curated_at >= ? AND cu.curated_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM published_ledger pl "
        "  WHERE pl.story_id = c.story_id AND pl.surface = ? "
        "  AND pl.edition_key <> ?) "
        "ORDER BY cu.relevance_score DESC, cu.curated_at DESC LIMIT ?",
        (min_relevance, since, until, kind, period_key, max_items),
    ).fetchall()


def _gather_subdigests(conn, kind: str, since: str, until: str):
    """Lower-tier digests whose window starts inside the period."""
    lower = LOWER_TIERS[kind]
    marks = ",".join("?" for _ in lower)
    return conn.execute(
        "SELECT kind, period_key, title, body_md FROM digests "
        "WHERE kind IN (%s) AND window_start >= ? AND window_start < ? "
        "ORDER BY window_start, kind" % marks,
        tuple(lower) + (since, until),
    ).fetchall()


def _items_payload(rows) -> List[Dict[str, Any]]:
    payload = []
    for r in rows:
        payload.append({
            "title": r["title"],
            "relevance": r["relevance_score"],
            "why": r["why_it_matters"],
            "notes": json.loads(r["notes"] or "[]"),
            "summary": r["summary"],
            "channels": json.loads(r["channels"] or "[]"),
            "novelty": r["novelty"],
            # archive.* never reaches the model — it would cite it back.
            "url": (no_archive(r["read_url"])
                    or no_archive(r["source_url"]) or ""),
        })
    return payload


def _build_prompt(kind: str, key: str, since: str, until: str,
                  items, subdigests) -> str:
    parts = ["PERIOD: %s %s (%s .. %s)" % (kind, key, since[:10], until[:10])]
    if subdigests:
        chunks = []
        for d in subdigests:
            chunks.append(
                "### [%s %s] %s\n%s"
                % (d["kind"], d["period_key"], d["title"] or "(untitled)",
                   d["body_md"] or "")
            )
        parts.append("LOWER-TIER DIGESTS IN THE PERIOD (oldest first):\n\n"
                     + "\n\n".join(chunks))
    if items:
        label = ("TOP CURATIONS IN THE PERIOD (JSON, best first):"
                 if subdigests else "CURATED ITEMS (JSON, best first):")
        parts.append("%s\n%s" % (
            label,
            json.dumps(_items_payload(items), indent=1, ensure_ascii=False),
        ))
    return "\n\n".join(parts)


def _staged_markdown(title: str, excerpt: str, body_md: str, key: str) -> str:
    today = datetime.date.today().isoformat()
    return (
        "<!--\n"
        "NAME         Signal Digest %s\n"
        "PROJECT      Signal\n"
        "D.CREATED    %s\n"
        "D.MODIFIED   %s\n"
        "VERSION      1.0.0\n"
        "TAGS         #Signal\n"
        "-->\n\n"
        "# %s\n\n"
        "> *%s*\n\n"
        "%s\n" % (key, today, today, title, excerpt, body_md)
    )


def run(cfg, kind: str = "weekly", period: Optional[str] = None,
        force: bool = False) -> int:
    started = time.time()
    if kind not in period_mod.KINDS:
        print("unknown digest kind %r (expected one of %s)"
              % (kind, ", ".join(period_mod.KINDS)))
        return 2

    if period:
        key = period
        since, until = period_mod.parse_period(kind, period)
    else:
        run_date = datetime.date.today()
        key = period_mod.period_key(kind, run_date)
        since, until = period_mod.window(kind, run_date)

    kcfg = cfg.digests[kind]
    min_relevance = int(kcfg.get("min_relevance", 7))
    max_items = int(kcfg.get("max_items", 40))

    conn = db_mod.connect_rw(cfg.db_path)
    try:
        existing = conn.execute(
            "SELECT id, window_end FROM digests WHERE kind=? AND period_key=?",
            (kind, key),
        ).fetchone()
        if existing and not force:
            # A row generated EARLY (e.g. a manual Tuesday weekly claiming
            # the ISO week with a window ending Tuesday) must not pre-empt
            # the scheduled run: regenerate when the existing window is
            # shorter than the one due now; skip only on full coverage.
            if (existing["window_end"] or "") >= until:
                print("%s digest for %s exists (use --force to regenerate)"
                      % (kind, key))
                return 0
            print("%s digest for %s exists but covers a shorter window "
                  "(%s < %s) — regenerating with the full window"
                  % (kind, key, existing["window_end"], until))

        subdigests = []
        if kind in LOWER_TIERS:
            subdigests = _gather_subdigests(conn, kind, since, until)
        items = _gather(conn, since, until, min_relevance, max_items, kind, key)
        if not items and not subdigests:
            print("no curated items (relevance>=%d) in %s %s — nothing to "
                  "digest" % (min_relevance, kind, key))
            return 1
        print("building %s digest for %s from %d items + %d sub-digests..."
              % (kind, key, len(items), len(subdigests)))

        prompt = _build_prompt(kind, key, since, until, items, subdigests)
        system = system_digest(kind, _load_style(cfg), _load_editorial(cfg))

        try:
            # The retrospectives (monthly+) synthesize across sub-digests and
            # earn 'max'; the bounded daily/weekly editions get 'high', the
            # recommended ceiling for prose — no overthinking, less quota burn.
            out, cost = adapter.complete_with_cost(
                "digest", system, prompt, DIGEST_SCHEMA,
                cfg=cfg, conn=conn,
                effort=("max" if kind in ("monthly", "quarterly", "yearly") else "high"),
                cap_kind="digest",
            )
        except UsageLimitExhausted as e:
            # Not a failure — the editions dispatcher re-fires on its interval
            # and digest.run is idempotent, so this retries until quota is back.
            db_mod.log_health(conn, "digest", "warn", str(e))
            print("digest deferred: %s" % e)
            return 1
        except (LLMError, SpendCapExceeded) as e:
            db_mod.log_health(conn, "digest", "error", str(e))
            print("digest failed: %s" % e)
            return 1

        title = out["title"].strip()
        body_md = out["body_md"].strip()
        blurb = (out.get("blurb") or "").strip()
        if _ARCHIVE_RE.search(body_md):  # same breadth as the publish scrub
            db_mod.log_health(
                conn, "digest", "error",
                "digest cited an archive link — regenerate; not stored",
            )
            print("REFUSED: digest body cites archive.* links")
            return 1

        try:
            import markdown as md_mod

            body_html = md_mod.markdown(body_md, extensions=["extra"])
        except (ImportError, AttributeError):
            # ImportError: markdown not installed. AttributeError: a python
            # without PyPI markdown resolves `import markdown` to the repo's
            # ./markdown/ content dir (an implicit namespace package with no
            # .markdown()), which would otherwise crash the digest AFTER the
            # costly model call. Fall back to a <pre> body either way.
            body_html = "<pre>%s</pre>" % body_md

        excerpt = blurb or (
            "The best of technology and AI, %s %s." % (kind, key)
        )
        # Stage in the state dir (TCC-safe for the launchd worker); promote
        # copies it into the repo's markdown/draft/ at publish time.
        staged_path = cfg.staging_dir / (
            "signal_digest_%s.md" % key.lower().replace("-", "_")
        )
        staged_path.write_text(_staged_markdown(title, excerpt, body_md, key))

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with db_mod.write_tx(conn):
            conn.execute(
                "INSERT INTO digests(kind, period_key, window_start, "
                "window_end, generated_at, model_used, title, blurb, "
                "body_md, body_html, cluster_ids, staged_path, promoted, "
                "cost_usd) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?) "
                "ON CONFLICT(kind, period_key) DO UPDATE SET "
                "window_start=excluded.window_start, "
                "window_end=excluded.window_end, "
                "generated_at=excluded.generated_at, "
                "model_used=excluded.model_used, title=excluded.title, "
                "blurb=excluded.blurb, "
                "body_md=excluded.body_md, body_html=excluded.body_html, "
                "cluster_ids=excluded.cluster_ids, "
                "staged_path=excluded.staged_path, "
                "cost_usd=excluded.cost_usd",
                (
                    kind, key, since, until, now, cfg.model_for("digest"),
                    title, blurb, body_md, body_html,
                    json.dumps([r["id"] for r in items]), str(staged_path),
                    cost,
                ),
            )
            # Record this edition's stories so a later edition of the same
            # cadence won't repeat them (cross-edition de-dup).
            conn.executemany(
                "INSERT OR IGNORE INTO published_ledger"
                "(story_id, surface, edition_key, cluster_id, first_at) "
                "VALUES(?,?,?,?,?)",
                [(r["story_id"], kind, key, r["id"], now)
                 for r in items if r["story_id"]],
            )
        msg = "%s digest %s: '%s' (%d items, %d sub-digests, $%.2f)" % (
            kind, key, title, len(items), len(subdigests), cost)
        print("%s (%.1fs)" % (msg, time.time() - started))
        print("staged: %s" % staged_path)
        if kind == "weekly":
            print("review on the dashboard, then: "
                  "python3 -m signalpipe promote --target local --apply")
        db_mod.log_health(conn, "digest", "info", msg)
        _dstats = {"kind": kind, "period": key,
                   "items": len(items), "cost_usd": cost}
        _fp = cfg.config_fingerprint()
        db_mod.record_run(conn, "digest", _fp["hash"], json.dumps(_dstats),
                          json.dumps(_fp["tunables"]))
        cfg.write_last_run("digest", _dstats)

        if cfg.site.get("push"):
            from . import publish

            try:
                publish.publish_digest(cfg, conn, kind, key, blurb=blurb)
            except Exception as e:  # noqa: BLE001 — publish never kills digest
                db_mod.log_health(conn, "digest", "warn",
                                  "site publish failed: %s" % e)
                print("site publish failed: %s" % e)
        return 0
    finally:
        conn.close()
