"""LLM curation of the daily finalists. Idempotent: a cluster gets exactly
one curations row; re-runs skip everything already pending/done/failed.

Funnel + division of labour:
  - deterministic score picks the finalists (score.finalists);
  - clusters inside funnel.triage_band get a cheap keep/skip gate first
    (triage tier — Haiku);
  - survivors are JUDGED by a single cloud call (judge tier — Haiku): keep/skip,
    relevance, channels, novelty + extracted facts;
  - kept items are WRITTEN by Claude (write tier — Sonnet), which polishes
    why_it_matters / notes / summary from the full article plus the judge's
    extracted facts.

Each tier's backend (api | subscription) comes from config
(backend.tier_overrides); the worker runs triage/judge/write on the metered API
and reserves the subscription for digests. The spend cap gates every call. One
bad item never kills the run; a cap hit stops the batch cleanly.
"""

from __future__ import annotations

import datetime
import json
import time
from typing import List, Optional, Tuple

from . import db as db_mod
from . import downtime
from . import score as score_mod
from .llm import LLMError, SpendCapExceeded, UsageLimitExhausted, adapter, quota
from .llm.schemas import (
    JUDGE_SCHEMA,
    SYSTEM_JUDGE,
    SYSTEM_TRIAGE,
    SYSTEM_WRITE,
    TRIAGE_SCHEMA,
    WRITE_SCHEMA,
)

MAX_ARTICLE_CHARS = 24000  # full article — used for the Claude write
MAX_JUDGE_CHARS = 6000     # the local judge/triage only need the lede + key
                           # facts; a short excerpt cuts 70B prompt-eval ~3-4x


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _build_prompt(conn, cluster, max_chars: int = MAX_ARTICLE_CHARS) -> str:
    art = conn.execute(
        "SELECT source_url, read_url, paywalled, text, excerpt, word_count "
        "FROM articles WHERE cluster_id=?",
        (cluster["id"],),
    ).fetchone()
    surfaces = conn.execute(
        "SELECT s.url, s.points, s.comments, src.name "
        "FROM surfaces s JOIN sources src ON src.id=s.source_id "
        "WHERE s.cluster_id=? ORDER BY s.points IS NULL, s.points DESC LIMIT 8",
        (cluster["id"],),
    ).fetchall()

    parts = [
        "TITLE: %s" % cluster["title"],
        "URL: %s" % (cluster["canonical_url"] or "(discussion-only)"),
        "FIRST SEEN: %s   DETERMINISTIC SCORE: %.1f/10   SURFACES: %d"
        % (cluster["first_seen"][:10], cluster["score"] or 0,
           cluster["surface_count"]),
    ]
    if surfaces:
        parts.append("WHERE IT SURFACED:")
        for s in surfaces:
            bits = ["- %s" % s["name"]]
            if s["points"]:
                bits.append("%d points" % s["points"])
            if s["comments"]:
                bits.append("%d comments" % s["comments"])
            parts.append(", ".join(bits))
    if art and art["paywalled"]:
        parts.append("NOTE: original is paywalled; text below may be partial.")
    text = (art["text"] if art else None) or ""
    if text:
        parts.append("ARTICLE TEXT:\n%s" % text[:max_chars])
    elif art and art["excerpt"]:
        parts.append("EXCERPT ONLY:\n%s" % art["excerpt"])
    else:
        parts.append(
            "NO ARTICLE TEXT AVAILABLE — judge from title/surfaces only and "
            "be conservative with relevance_score."
        )
    return "\n\n".join(parts)


def _write_prompt(article_prompt: str, judged: dict) -> str:
    """The Claude writer sees the article PLUS the arena's extraction, so it
    polishes from the source (not from the facts alone)."""
    extra = [
        "EDITOR'S JUDGMENT (write from the article above; the facts below are "
        "the editor's extraction to build on, not a substitute for the text):",
        "relevance: %s/10" % judged.get("relevance_score"),
        "channels: %s" % ", ".join(judged.get("channels") or []),
        "novelty: %s" % (judged.get("novelty") or ""),
    ]
    facts = judged.get("facts") or []
    if facts:
        extra.append("extracted facts:")
        extra.extend("- %s" % f for f in facts)
    return article_prompt + "\n\n" + "\n".join(extra)


# ---- persistence helpers ------------------------------------------------------

def _model_label(cfg, tier: str, backend: str) -> str:
    """The model id to record in the ledger. For local tiers, cfg.model_for maps
    local->subscription (the cloud-fallback id), so read the real Ollama model
    from local_models_for instead — otherwise the DB mislabels local runs."""
    if backend == "local":
        models = cfg.local_models_for(tier)
        return models[0] if models else "local"
    return cfg.model_for(tier, backend)


def _mark_triaged_out(conn, c, gate, cfg) -> None:
    backend = cfg.backend_for("triage")
    with db_mod.write_tx(conn):
        conn.execute(
            "UPDATE curations SET status='skipped', skip=1, skip_reason=?, "
            "tier_used='triage', backend_used=?, model_used=?, "
            "curated_at=? WHERE cluster_id=?",
            (str(gate.get("reason", ""))[:300],
             backend, _model_label(cfg, "triage", backend),
             _now_iso(), c["id"]),
        )


def _mark_judge_skip(conn, c, judged, cfg, triaged: bool) -> None:
    backend = cfg.backend_for("judge")
    with db_mod.write_tx(conn):
        conn.execute(
            "UPDATE curations SET status='skipped', tier_used=?, "
            "backend_used=?, model_used=?, relevance_score=?, "
            "channels=?, novelty=?, audience=?, skip=1, skip_reason=?, "
            "curated_at=? WHERE cluster_id=?",
            (("triage+judge" if triaged else "judge"),
             backend, _model_label(cfg, "judge", backend),
             int(judged.get("relevance_score") or 0),
             json.dumps(judged.get("channels") or []),
             (judged.get("novelty") or "")[:160] or None, judged.get("audience"),
             str(judged.get("skip_reason") or "")[:300],
             _now_iso(), c["id"]),
        )


def _persist_done(conn, c, judged, written, cfg, triaged: bool) -> None:
    backend = cfg.backend_for("write")
    with db_mod.write_tx(conn):
        conn.execute(
            "UPDATE curations SET status='done', tier_used=?, "
            "backend_used=?, model_used=?, relevance_score=?, "
            "why_it_matters=?, notes=?, summary=?, channels=?, novelty=?, "
            "audience=?, skip=0, skip_reason=NULL, curated_at=? "
            "WHERE cluster_id=?",
            (("triage+judge+write" if triaged else "judge+write"),
             backend, _model_label(cfg, "write", backend),
             int(judged.get("relevance_score") or 0),
             written.get("why_it_matters"),
             json.dumps(written.get("notes") or []),
             written.get("summary"),
             json.dumps(judged.get("channels") or []),
             (judged.get("novelty") or "")[:160] or None, judged.get("audience"),
             _now_iso(), c["id"]),
        )


def _mark_failed(conn, c, e) -> None:
    with db_mod.write_tx(conn):
        conn.execute(
            "UPDATE curations SET status='failed', skip_reason=?, "
            "curated_at=? WHERE cluster_id=?",
            (str(e)[:300], _now_iso(), c["id"]),
        )


def run(cfg, limit: Optional[int] = None, dry_run: bool = False) -> int:
    started = time.time()
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        # Fully-local preflight: if curation routes to Ollama and Ollama is
        # unreachable, defer the WHOLE run without marking any item 'failed'
        # (a failure would impose the 6h finalists retry penalty needlessly).
        # The score.finalists query already retries genuine per-item failures.
        if not dry_run and cfg.backend_for("judge") == "local" \
                and not downtime.ollama_up(cfg):
            msg = "ollama unreachable — deferring curate (no items touched)"
            print("curate: %s" % msg)
            db_mod.log_health(conn, "curate", "warn", msg)
            return 0

        # Same shape for a subscription usage-limit hold: defer the WHOLE run,
        # touch nothing. The worker's probe job resumes us when usage is back.
        held, held_why = quota.status()
        if not dry_run and held:
            msg = "deferring curate (%s)" % held_why
            print("curate: %s" % msg)
            db_mod.log_health(conn, "curate", "warn", msg)
            return 0

        # Sweep orphaned 'pending' claims from a crashed run (sole writer).
        if not dry_run:
            with db_mod.write_tx(conn):
                cur = conn.execute(
                    "DELETE FROM curations WHERE status='pending'")
            if cur.rowcount:
                msg = ("cleared %d orphaned pending claim(s) from a "
                       "previous run" % cur.rowcount)
                print("curate: %s" % msg)
                db_mod.log_health(conn, "curate", "warn", msg)

        # Local judgment is slow, so each cycle curates a small batch and the
        # worker runs curate often (cadences.curate_min). The deterministic
        # score orders finalists, so the best items are judged first.
        if limit is None:
            # Default 3, NOT None: a missing key must not fall through to
            # finalists()' daily_finalists cap (80) — an 80-item paid batch.
            limit = cfg.funnel.get("curate_batch", 3)
        finalists = score_mod.finalists(conn, cfg, limit)
        if not finalists:
            print("no uncurated finalists")
            return 0
        if dry_run:
            print("would curate %d clusters:" % len(finalists))
            for c in finalists:
                print("  %5.2f  #%d %s" % (c["score"], c["id"], c["title"][:90]))
            return 0

        band = cfg.funnel.get("triage_band", [3.5, 6.0])
        stats = {"done": 0, "skipped": 0, "triaged_out": 0, "failed": 0,
                 "cap_stopped": 0, "quota_stopped": 0}

        # Claim every finalist up front (idempotent).
        for c in finalists:
            with db_mod.write_tx(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO curations(cluster_id, status) "
                    "VALUES(?, 'pending')", (c["id"],))

        # PHASE 1 — triage (cheap keep/skip gate). survivors carry a triaged flag.
        # Triage and judge only need the lede + key facts, so they run on a short
        # excerpt (MAX_JUDGE_CHARS) at 'low' effort — classification/extraction,
        # not writing. Only the writer (below) reads the full article.
        survivors: List[Tuple[dict, str, bool]] = []
        for c in finalists:
            prompt = _build_prompt(conn, c, MAX_JUDGE_CHARS)
            try:
                in_band = bool(band) and band[0] <= (c["score"] or 0) <= band[1]
                if in_band:
                    gate = adapter.complete(
                        "triage", SYSTEM_TRIAGE, prompt, TRIAGE_SCHEMA,
                        cfg=cfg, conn=conn, effort="low")
                    if not gate.get("keep"):
                        _mark_triaged_out(conn, c, gate, cfg)
                        stats["triaged_out"] += 1
                        continue
                survivors.append((c, prompt, in_band))
            except UsageLimitExhausted as e:
                # Quota is a waiting game, not a failure: stop the run without
                # marking anything failed. Leftover 'pending' claims are swept
                # by the next run; the probe job pulls curate forward when
                # usage is back.
                db_mod.log_health(conn, "curate", "warn", str(e))
                stats["quota_stopped"] = 1
                print("STOP: %s" % e)
                break
            except LLMError as e:
                _mark_failed(conn, c, e)
                stats["failed"] += 1

        # PHASE 2 — judge + write. One cloud judge call per survivor (keep/skip +
        # relevance + facts); kept items get a Claude write from the full
        # article. Both tiers route through the adapter, so their backend
        # (subscription | api) and model come from config — judge=Haiku,
        # write=Sonnet — and the spend cap gates every call.
        for c, prompt, triaged in survivors:
            try:
                judged = adapter.complete(
                    "judge", SYSTEM_JUDGE, prompt, JUDGE_SCHEMA,
                    cfg=cfg, conn=conn, effort="low")

                if judged.get("skip"):
                    _mark_judge_skip(conn, c, judged, cfg, triaged)
                    stats["skipped"] += 1
                    continue

                # the writer polishes from the FULL article (the triage/judge
                # prompt above is only the short excerpt), plus the judge's facts.
                full_prompt = _build_prompt(conn, c, MAX_ARTICLE_CHARS)
                written = adapter.complete(
                    "write", SYSTEM_WRITE, _write_prompt(full_prompt, judged),
                    WRITE_SCHEMA, cfg=cfg, conn=conn)
                _persist_done(conn, c, judged, written, cfg, triaged)
                stats["done"] += 1
            except UsageLimitExhausted as e:
                db_mod.log_health(conn, "curate", "warn", str(e))
                with db_mod.write_tx(conn):
                    conn.execute(
                        "DELETE FROM curations WHERE cluster_id=? "
                        "AND status='pending'", (c["id"],))
                stats["quota_stopped"] = 1
                print("STOP: %s" % e)
                break
            except SpendCapExceeded as e:
                db_mod.log_health(conn, "curate", "warn", str(e))
                with db_mod.write_tx(conn):
                    conn.execute(
                        "DELETE FROM curations WHERE cluster_id=? "
                        "AND status='pending'", (c["id"],))
                stats["cap_stopped"] = 1
                print("STOP: %s" % e)
                break
            except LLMError as e:
                _mark_failed(conn, c, e)
                stats["failed"] += 1

        msg = (
            "curate: %(done)d done, %(skipped)d skipped, %(triaged_out)d "
            "triaged out, %(failed)d failed" % stats
        )
        if stats["cap_stopped"]:
            msg += " [stopped at spend cap]"
        if stats["quota_stopped"]:
            msg += " [stopped: subscription usage limit — will retry]"
        print("%s (%.1fs)" % (msg, time.time() - started))
        db_mod.log_health(conn, "curate", "info", msg, json.dumps(stats))
        _fp = cfg.config_fingerprint()
        db_mod.record_run(conn, "curate", _fp["hash"], json.dumps(stats),
                          json.dumps(_fp["tunables"]))
        cfg.write_last_run("curate", stats)
        return 0
    finally:
        conn.close()
