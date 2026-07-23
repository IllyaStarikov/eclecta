"""Adaptive selection thresholds: a "featured" bar that hardens over time.

Opt-in and bounded. When ``funnel.adaptive.enabled`` is false, every reader
returns the caller's existing constant, so selection is byte-identical to today.
When enabled, the effective bar is a percentile of a trailing window, clamped to
a floor/ceiling and step-limited day to day; the target percentile ramps slowly
upward over ``ramp_days`` so being featured gets harder as the corpus improves.
An empty window falls back to the constant — the site never starves.

Pure-ish: the math is pure; the readers take an open DB connection and a
``now`` datetime (both injected in tests).
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List, Optional


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def percentile(values: List[float], p: float) -> float:
    """Linear-interpolation percentile (p in 0..100). Values need not be sorted.
    Raises ValueError on an empty list (callers guard first)."""
    if not values:
        raise ValueError("percentile of empty sequence")
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    rank = (p / 100.0) * (len(xs) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


def _as_date(now) -> datetime.date:
    if isinstance(now, datetime.datetime):
        return now.date()
    if isinstance(now, datetime.date):
        return now
    return datetime.date.fromisoformat(str(now))


def ramped_percentile(now, acfg: Dict[str, Any]) -> float:
    """Target percentile at ``now``: ramps percentile_start -> percentile_end
    linearly over ramp_days beginning at ramp_start (clamped outside)."""
    start = float(acfg.get("percentile_start", 50))
    end = float(acfg.get("percentile_end", 70))
    ramp_days = float(acfg.get("ramp_days", 120))
    ramp_start = datetime.date.fromisoformat(str(acfg.get("ramp_start", "2026-08-01")))
    days = (_as_date(now) - ramp_start).days
    frac = clamp(days / ramp_days, 0.0, 1.0) if ramp_days > 0 else 1.0
    return start + (end - start) * frac


def _windowed(conn, sql: str, since: str) -> List[float]:
    return [
        r[0] for r in conn.execute(sql, (since,)).fetchall() if r[0] is not None
    ]


def effective_min_score(
    conn, acfg: Dict[str, Any], now: datetime.datetime, base: float,
    prev: Optional[float] = None,
) -> float:
    """The deterministic-score gate for paid curation. Returns ``base`` when
    disabled or when the trailing window is empty."""
    if not acfg.get("enabled"):
        return base
    window_h = float(acfg.get("window_hours", 336))
    since = (now - datetime.timedelta(hours=window_h)).isoformat()
    vals = _windowed(
        conn,
        "SELECT score FROM clusters WHERE score IS NOT NULL AND last_seen >= ?",
        since,
    )
    if not vals:
        return base
    val = clamp(
        percentile(vals, ramped_percentile(now, acfg)),
        float(acfg.get("score_floor", 3.5)),
        float(acfg.get("score_ceiling", 7.0)),
    )
    if prev is not None:
        step = float(acfg.get("max_daily_step", 0.5))
        val = clamp(val, prev - step, prev + step)
    return round(val, 3)


def effective_min_relevance(
    conn, acfg: Dict[str, Any], now: datetime.datetime, base: int,
    prev: Optional[float] = None,
) -> int:
    """The judge-relevance gate for picks/editions/feed. Returns ``base`` when
    disabled or when the trailing window is empty."""
    if not acfg.get("enabled"):
        return base
    window_h = float(acfg.get("window_hours", 336))
    since = (now - datetime.timedelta(hours=window_h)).isoformat()
    vals = _windowed(
        conn,
        "SELECT relevance_score FROM curations WHERE status='done' AND skip=0 "
        "AND relevance_score IS NOT NULL AND curated_at >= ?",
        since,
    )
    if not vals:
        return base
    val = clamp(
        percentile(vals, ramped_percentile(now, acfg)),
        float(acfg.get("relevance_floor", 6)),
        float(acfg.get("relevance_ceiling", 8)),
    )
    if prev is not None:
        step = float(acfg.get("max_daily_step", 0.5))
        val = clamp(val, prev - step, prev + step)
    return int(round(val))
