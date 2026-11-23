"""Tests for signalpipe.adaptive — percentile/ramp math + effective readers."""

from __future__ import annotations

import datetime

from signalpipe import adaptive as ad

NOW = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - datetime.timedelta(hours=hours_ago)).isoformat()


# --------------------------------------------------------------------------- #
# Pure math
# --------------------------------------------------------------------------- #
def test_percentile_interpolates():
    xs = list(range(1, 11))  # 1..10
    assert ad.percentile(xs, 50) == 5.5
    assert ad.percentile(xs, 0) == 1.0
    assert ad.percentile(xs, 100) == 10.0


def test_clamp():
    assert ad.clamp(5, 0, 10) == 5
    assert ad.clamp(-1, 0, 10) == 0
    assert ad.clamp(99, 0, 10) == 10


def test_ramped_percentile_over_time():
    acfg = {"percentile_start": 50, "percentile_end": 70,
            "ramp_start": "2026-01-01", "ramp_days": 100}
    start = datetime.date(2026, 1, 1)
    assert ad.ramped_percentile(start, acfg) == 50
    assert ad.ramped_percentile(start + datetime.timedelta(days=100), acfg) == 70
    assert ad.ramped_percentile(start + datetime.timedelta(days=50), acfg) == 60
    # before the ramp starts -> clamped to start
    assert ad.ramped_percentile(start - datetime.timedelta(days=30), acfg) == 50


# --------------------------------------------------------------------------- #
# Effective readers
# --------------------------------------------------------------------------- #
def _seed_scores(seed, scores):
    for i, s in enumerate(scores):
        seed.cluster(title="C%d" % i, canonical_url="https://ex/%d" % i,
                     score=float(s), last_seen=_iso(1))


def _flat(pct, **over):
    c = {"enabled": True, "window_hours": 336,
         "percentile_start": pct, "percentile_end": pct,
         "ramp_start": "2026-01-01", "ramp_days": 100,
         "score_floor": 0.0, "score_ceiling": 10.0,
         "relevance_floor": 0, "relevance_ceiling": 10,
         "max_daily_step": 0.5}
    c.update(over)
    return c


def test_effective_min_score_disabled_returns_base(conn, seed):
    _seed_scores(seed, range(1, 11))
    acfg = _flat(50)
    acfg["enabled"] = False
    assert ad.effective_min_score(conn, acfg, NOW, base=3.5) == 3.5


def test_effective_min_score_percentile(conn, seed):
    _seed_scores(seed, range(1, 11))  # scores 1..10
    assert ad.effective_min_score(conn, _flat(50), NOW, base=3.5) == 5.5


def test_effective_min_score_empty_window_falls_back(conn):
    # no clusters -> base (never starves)
    assert ad.effective_min_score(conn, _flat(50), NOW, base=3.5) == 3.5


def test_effective_min_score_clamped_to_floor_ceiling(conn, seed):
    _seed_scores(seed, range(1, 11))
    hi = ad.effective_min_score(conn, _flat(100, score_ceiling=7.0), NOW, base=3.5)
    assert hi == 7.0  # percentile 100 = 10, clamped to ceiling
    lo = ad.effective_min_score(conn, _flat(0, score_floor=4.0), NOW, base=3.5)
    assert lo == 4.0  # percentile 0 = 1, clamped to floor


def test_effective_min_score_step_limited(conn, seed):
    _seed_scores(seed, range(1, 11))  # p50 = 5.5
    # prev=4.0, step 0.5 -> can rise to at most 4.5
    v = ad.effective_min_score(conn, _flat(50), NOW, base=3.5, prev=4.0)
    assert v == 4.5


def test_effective_min_relevance(conn, seed):
    for i, r in enumerate([5, 6, 7, 8, 9]):
        cid = seed.cluster(title="R%d" % i, canonical_url="https://r/%d" % i)
        seed.curation(cid, status="done", skip=0, relevance_score=r,
                      curated_at=_iso(1))
    acfg = _flat(50, relevance_floor=0, relevance_ceiling=10)
    # p50 of [5,6,7,8,9] = 7
    assert ad.effective_min_relevance(conn, acfg, NOW, base=6) == 7
    acfg["enabled"] = False
    assert ad.effective_min_relevance(conn, acfg, NOW, base=6) == 6
