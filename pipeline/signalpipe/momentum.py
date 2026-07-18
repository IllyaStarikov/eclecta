"""Topic momentum: what matters now, and what's emerging.

Deterministic, zero-LLM. Aggregates curated items by taxonomy category over two
trailing windows (recent vs. an older baseline), using ``clusters.first_seen``
as the time axis, and emits:

  * ``kb/momentum.json`` — a machine-readable per-category view
    ``{volume_recent, volume_baseline, featured_rate, momentum, trend,
    emerging}`` for the Library + the nightly pass.
  * a clamped per-category **importance multiplier** (~0.85–1.25) that
    ``score.py`` can fold into the ``topic_match`` contribution — opt-in, a
    no-op when disabled or the artifact is absent.

Momentum is measured over *curated* items (the things that passed deterministic
scoring into curation): that is the pipeline's own revealed sense of what's
worth a closer look, which is exactly the signal we want to steer with.
"""

from __future__ import annotations

import datetime
import json
import math
import pathlib
from typing import Any, Dict

ARTIFACT_REL = "kb/momentum.json"
_EPS = 1e-9

DEFAULTS = {
    "enabled": False,
    "recent_hours": 168,      # 7d
    "baseline_hours": 720,    # 30d (the older slice is baseline minus recent)
    "multiplier_min": 0.85,
    "multiplier_max": 1.25,
    "emerging_min_recent": 3,
    "featured_relevance": 6,
}


def config(cfg) -> Dict[str, Any]:
    """Merge the `momentum` config block over defaults. Accepts a Config, a
    plain dict, or None."""
    raw: Dict[str, Any] = {}
    if isinstance(cfg, dict):
        raw = cfg
    elif cfg is not None:
        raw = (getattr(cfg, "data", {}) or {}).get("momentum", {}) or {}
    out = dict(DEFAULTS)
    out.update(raw)
    return out


def _category_of(row) -> str:
    cat = row["category"] if "category" in row.keys() else None
    if cat:
        return cat
    from .topics import match_taxonomy

    try:
        channels = json.loads(row["channels"]) if row["channels"] else []
    except (ValueError, TypeError, KeyError):
        channels = []
    return match_taxonomy(row["title"] or "", channels).get("category", "")


def compute(conn, mcfg: Dict[str, Any], now: datetime.datetime) -> Dict[str, Dict[str, Any]]:
    """Per-category momentum over the recent vs. baseline windows."""
    recent_h = float(mcfg["recent_hours"])
    base_h = float(mcfg["baseline_hours"])
    older_h = max(base_h - recent_h, _EPS)  # length of the baseline-only slice
    feat_rel = int(mcfg["featured_relevance"])
    emerging_min = int(mcfg["emerging_min_recent"])

    recent_since = (now - datetime.timedelta(hours=recent_h)).isoformat()
    base_since = (now - datetime.timedelta(hours=base_h)).isoformat()

    rows = conn.execute(
        "SELECT cu.category, cu.channels, cu.relevance_score, cu.skip, "
        "cu.status, c.title, c.first_seen "
        "FROM curations cu JOIN clusters c ON c.id = cu.cluster_id "
        "WHERE c.first_seen >= ?",
        (base_since,),
    ).fetchall()

    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cat = _category_of(r)
        if not cat:
            continue
        a = agg.setdefault(
            cat, {"recent": 0, "baseline": 0, "recent_featured": 0}
        )
        if r["first_seen"] >= recent_since:
            a["recent"] += 1
            featured = (
                r["status"] == "done"
                and not r["skip"]
                and (r["relevance_score"] or 0) >= feat_rel
            )
            if featured:
                a["recent_featured"] += 1
        else:
            a["baseline"] += 1

    out: Dict[str, Dict[str, Any]] = {}
    for cat, a in agg.items():
        recent_rate = a["recent"] / recent_h
        baseline_rate = a["baseline"] / older_h
        momentum = recent_rate / max(baseline_rate, _EPS)
        if momentum > 1.15:
            trend = "rising"
        elif momentum < 0.85:
            trend = "fading"
        else:
            trend = "steady"
        emerging = baseline_rate < _EPS and a["recent"] >= emerging_min
        out[cat] = {
            "volume_recent": a["recent"],
            "volume_baseline": a["baseline"],
            "featured_rate": round(
                a["recent_featured"] / a["recent"], 4) if a["recent"] else 0.0,
            "momentum": round(momentum, 4),
            "trend": trend,
            "emerging": emerging,
        }
    return out


def _multiplier(momentum: float, lo: float, hi: float) -> float:
    """Monotone-increasing map momentum->[lo,hi]. momentum=0->lo, =1->mid,
    >=3->hi. Clamped."""
    frac = math.log1p(max(momentum, 0.0)) / math.log(4.0)  # log1p(3)/log4 = 1.0
    x = lo + (hi - lo) * frac
    return round(max(lo, min(hi, x)), 3)


def importance_multipliers(
    mom: Dict[str, Dict[str, Any]], mcfg: Dict[str, Any]
) -> Dict[str, float]:
    lo = float(mcfg["multiplier_min"])
    hi = float(mcfg["multiplier_max"])
    return {
        cat: _multiplier(float(m["momentum"]), lo, hi) for cat, m in mom.items()
    }


def momentum_artifact(conn, mcfg: Dict[str, Any], now: datetime.datetime):
    """(relpath, json-content) for publish to commit. Deterministic."""
    mom = compute(conn, mcfg, now)
    mult = importance_multipliers(mom, mcfg)
    payload = {
        "generated_for": now.date().isoformat(),
        "recent_hours": mcfg["recent_hours"],
        "baseline_hours": mcfg["baseline_hours"],
        "categories": mom,
        "multipliers": mult,
    }
    return ARTIFACT_REL, json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_multipliers(repo_root) -> Dict[str, float]:
    """Read kb/momentum.json multipliers; {} if absent/unreadable (no-op)."""
    p = pathlib.Path(repo_root) / ARTIFACT_REL
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    mult = data.get("multipliers", {})
    if not isinstance(mult, dict):
        return {}
    return {k: float(v) for k, v in mult.items()}
