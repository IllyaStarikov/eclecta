"""Eval sets: a versioned gold corpus + judge-replay scoring.

Repo-side only. Gold examples live in ``eval/gold/curation.jsonl``; metrics land
in ``eval/results/<date>.json``. Candidates are built from the DB **read-only**
(never the live server) using ``published_ledger`` as ground truth for whether a
story was *featured*. The judge is replayed with the SAME system prompt + schema
the live pipeline uses (``schemas.SYSTEM_JUDGE`` / ``JUDGE_SCHEMA``), defaulting
to the free local backend ($0).

The gold set exists so a future pass can measure whether the CURRENT judge still
agrees with past outcomes — a regression alarm — and so the bar for "featured"
can be tracked over time. It grows a few examples per nightly pass and is
corrected by hand via :func:`label`.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Callable, Dict, List, Optional

GOLD_REL = "eval/gold/curation.jsonl"
RESULTS_REL = "eval/results"
DEFAULT_FEATURED_REL = 6  # relevance at/above which a pick reaches the feed


# --------------------------------------------------------------------------- #
# Metrics (pure)
# --------------------------------------------------------------------------- #
def score_predictions(
    preds: List[Dict[str, Any]],
    golds: List[Dict[str, Any]],
    featured_rel: int = DEFAULT_FEATURED_REL,
) -> Dict[str, Any]:
    """Compare judge predictions to human/gold labels.

    ``preds`` items: ``{"id", "relevance": int, "skip": bool, "category": str}``.
    ``golds`` items carry ``human = {"featured": bool, "relevance": int,
    "category": str}``. A prediction counts as *featured* when it is not skipped
    and its relevance clears ``featured_rel``. All ratios are divide-by-zero
    safe (empty -> 0.0)."""
    by_id = {g["id"]: g for g in golds}
    n = agree = abs_err = tp = fp = fn = cat_ok = cat_n = 0
    for p in preds:
        g = by_id.get(p["id"])
        if g is None:
            continue
        n += 1
        gh = g.get("human", {})
        p_rel = int(p.get("relevance", 0) or 0)
        pred_featured = (not p.get("skip", False)) and p_rel >= featured_rel
        gold_featured = bool(gh.get("featured", False))
        if pred_featured == gold_featured:
            agree += 1
        if pred_featured and gold_featured:
            tp += 1
        elif pred_featured and not gold_featured:
            fp += 1
        elif (not pred_featured) and gold_featured:
            fn += 1
        abs_err += abs(p_rel - int(gh.get("relevance", 0) or 0))
        if gh.get("category"):
            cat_n += 1
            if p.get("category") == gh.get("category"):
                cat_ok += 1

    def _safe(a: float, b: float) -> float:
        return round(a / b, 4) if b else 0.0

    return {
        "n": n,
        "agreement_featured": _safe(agree, n),
        "relevance_mae": _safe(abs_err, n),
        "featured_precision": _safe(tp, tp + fp),
        "featured_recall": _safe(tp, tp + fn),
        "category_accuracy": _safe(cat_ok, cat_n),
    }


# --------------------------------------------------------------------------- #
# Gold I/O
# --------------------------------------------------------------------------- #
def gold_path(repo_root) -> pathlib.Path:
    return pathlib.Path(repo_root) / GOLD_REL


def load_gold(repo_root) -> List[Dict[str, Any]]:
    p = gold_path(repo_root)
    if not p.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def save_gold(repo_root, rows: List[Dict[str, Any]]) -> None:
    p = gold_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows
        )
    )


def grow(
    gold: List[Dict[str, Any]], candidates: List[Dict[str, Any]], k: int
) -> List[Dict[str, Any]]:
    """Append up to ``k`` candidates not already present (dedup by ``id``)."""
    seen = {g["id"] for g in gold}
    out = list(gold)
    added = 0
    for c in candidates:
        if added >= k:
            break
        if c["id"] in seen:
            continue
        out.append(c)
        seen.add(c["id"])
        added += 1
    return out


def label(
    gold: List[Dict[str, Any]], id: str, human: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Upsert a confirmed human label for ``id``."""
    out: List[Dict[str, Any]] = []
    found = False
    for g in gold:
        if g["id"] == id:
            g = dict(g)
            g["human"] = human
            g["confidence"] = "confirmed"
            g["labeled_by"] = "illya"
            found = True
        out.append(g)
    if not found:
        out.append(
            {
                "id": id,
                "title": "",
                "source": "",
                "url": "",
                "excerpt": "",
                "human": human,
                "provenance": "manual",
                "labeled_by": "illya",
                "confidence": "confirmed",
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Candidate build (DB read-only; never the live server)
# --------------------------------------------------------------------------- #
def _category_for(title: str, channels_json: Optional[str]) -> str:
    from .topics import match_taxonomy

    try:
        channels = json.loads(channels_json) if channels_json else []
    except (ValueError, TypeError):
        channels = []
    return match_taxonomy(title or "", channels).get("category", "")


def _mk(row, featured: bool, relevance: int, provenance: str) -> Dict[str, Any]:
    cid = row["story_id"] or ("url:" + (row["canonical_url"] or ""))
    return {
        "id": cid,
        "title": row["title"] or "",
        "source": row["canonical_url"] or "",
        "url": row["canonical_url"] or "",
        "excerpt": (row["excerpt"] if "excerpt" in row.keys() else None)
        or (row["title"] or ""),
        "human": {
            "featured": featured,
            "relevance": int(relevance),
            "category": _category_for(
                row["title"] or "",
                row["channels"] if "channels" in row.keys() else None,
            ),
        },
        "provenance": provenance,
        "labeled_by": "seed",
        "confidence": "provisional",
    }


def build_candidates(conn, limit: int = 50) -> List[Dict[str, Any]]:
    """Provisional gold candidates from committed DB state, read-only.

    positives  — done, un-skipped curations whose story appeared in a real
                 edition (``published_ledger`` surface != 'picks').
    negatives  — skipped curations.
    hard-neg   — clusters with a high deterministic score that were never
                 curated (the pipeline/human passed on them)."""
    out: List[Dict[str, Any]] = []

    pos = conn.execute(
        "SELECT c.story_id, c.title, c.canonical_url, cu.relevance_score, "
        "cu.channels, a.excerpt "
        "FROM curations cu JOIN clusters c ON c.id = cu.cluster_id "
        "LEFT JOIN articles a ON a.cluster_id = cu.cluster_id "
        "WHERE cu.status='done' AND cu.skip=0 AND EXISTS ("
        "  SELECT 1 FROM published_ledger pl "
        "  WHERE pl.story_id = c.story_id AND pl.surface <> 'picks') "
        "ORDER BY cu.relevance_score DESC LIMIT ?",
        (limit,),
    ).fetchall()
    for r in pos:
        out.append(_mk(r, True, r["relevance_score"] or 8, "edition"))

    neg = conn.execute(
        "SELECT c.story_id, c.title, c.canonical_url, cu.relevance_score, "
        "cu.channels, a.excerpt "
        "FROM curations cu JOIN clusters c ON c.id = cu.cluster_id "
        "LEFT JOIN articles a ON a.cluster_id = cu.cluster_id "
        "WHERE cu.status='skipped' OR (cu.status='done' AND cu.skip=1) "
        "ORDER BY cu.relevance_score LIMIT ?",
        (limit,),
    ).fetchall()
    for r in neg:
        out.append(_mk(r, False, r["relevance_score"] or 3, "skipped"))

    hard = conn.execute(
        "SELECT c.story_id, c.title, c.canonical_url, NULL AS channels, "
        "NULL AS excerpt, c.score "
        "FROM clusters c "
        "WHERE c.score IS NOT NULL AND c.score >= 5.0 AND NOT EXISTS ("
        "  SELECT 1 FROM curations cu "
        "  WHERE cu.cluster_id = c.id AND cu.status='done') "
        "ORDER BY c.score DESC LIMIT ?",
        (limit,),
    ).fetchall()
    for r in hard:
        out.append(_mk(r, False, 4, "top-uncurated"))

    return out


# --------------------------------------------------------------------------- #
# Judge replay
# --------------------------------------------------------------------------- #
def _judge_prompt(ex: Dict[str, Any]) -> str:
    body = ex.get("excerpt") or ex.get("title") or ""
    return "TITLE: %s\nSOURCE: %s\n\n%s" % (
        ex.get("title", ""),
        ex.get("source", ""),
        body,
    )


def _derive_category(out: Dict[str, Any], ex: Dict[str, Any]) -> str:
    from .topics import match_taxonomy

    channels = out.get("channels") or []
    return match_taxonomy(ex.get("title", ""), channels).get("category", "")


def _default_judge(cfg, backend: str, conn) -> Callable[..., Dict[str, Any]]:
    from .llm import schemas

    if backend == "local":
        from .llm import backend_local

        models = cfg.local_models_for("judge") if cfg else []
        model = models[0] if models else "llama3.1"

        def _local(_ex, prompt):
            obj, _ = backend_local.run(
                model, schemas.SYSTEM_JUDGE, prompt, schemas.JUDGE_SCHEMA, cfg
            )
            return obj

        return _local

    from .llm import adapter

    def _cloud(_ex, prompt):
        return adapter.complete(
            "judge",
            schemas.SYSTEM_JUDGE,
            prompt,
            schemas.JUDGE_SCHEMA,
            cfg=cfg,
            conn=conn,
        )

    return _cloud


def run(
    gold: List[Dict[str, Any]],
    cfg=None,
    backend: str = "local",
    date: str = "",
    conn=None,
    judge_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Replay the current judge over the gold set and score it.

    ``judge_fn(ex, prompt) -> judge-dict`` is injectable for tests; production
    resolves it from ``backend`` (default 'local', $0)."""
    from .llm import schemas

    jf = judge_fn or _default_judge(cfg, backend, conn)
    preds: List[Dict[str, Any]] = []
    for ex in gold:
        out = jf(ex, _judge_prompt(ex))
        preds.append(
            {
                "id": ex["id"],
                "relevance": int(out.get("relevance_score", 0) or 0),
                "skip": bool(out.get("skip", False)),
                "category": _derive_category(out, ex),
            }
        )
    metrics = score_predictions(preds, gold)
    metrics.update(
        {
            "backend": backend,
            "date": date,
            "judge_prompt_hash": hashlib.sha256(
                schemas.SYSTEM_JUDGE.encode("utf-8")
            ).hexdigest()[:12],
            "model_used": (
                cfg.model_for("judge", backend)
                if (cfg is not None and backend != "local")
                else "local"
            ),
            "cost_usd": 0.0,
        }
    )
    return {"metrics": metrics, "predictions": preds}


def write_result(repo_root, date: str, result: Dict[str, Any]) -> pathlib.Path:
    d = pathlib.Path(repo_root) / RESULTS_REL
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%s.json" % date)
    p.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return p


def latest_result(repo_root) -> Optional[Dict[str, Any]]:
    d = pathlib.Path(repo_root) / RESULTS_REL
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text())
