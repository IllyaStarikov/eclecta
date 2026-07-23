"""Tests for signalpipe.momentum — deterministic per-category momentum."""

from __future__ import annotations

import datetime
import json

from signalpipe import momentum as mom

NOW = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - datetime.timedelta(hours=hours_ago)).isoformat()


def _add(seed, i, category, hours_ago, relevance=8, status="done", skip=0):
    cid = seed.cluster(
        title="Story %d about %s" % (i, category),
        canonical_url="https://ex.example/%d" % i,
        first_seen=_iso(hours_ago),
    )
    seed.curation(cid, category=category, relevance_score=relevance,
                  status=status, skip=skip, channels=json.dumps([category]))
    return cid


def _cfg(**over):
    c = dict(mom.DEFAULTS)
    c.update(over)
    return c


def test_rising_category(conn, seed):
    # ai: 6 recent (featured), 1 baseline -> momentum >> 1, rising, all featured
    for i in range(6):
        _add(seed, i, "ai", hours_ago=24)
    _add(seed, 100, "ai", hours_ago=300)  # baseline slice (168 < 300 < 720)
    out = mom.compute(conn, _cfg(), NOW)
    assert out["ai"]["volume_recent"] == 6
    assert out["ai"]["volume_baseline"] == 1
    assert out["ai"]["momentum"] > 1.15
    assert out["ai"]["trend"] == "rising"
    assert out["ai"]["featured_rate"] == 1.0
    assert out["ai"]["emerging"] is False


def test_emerging_category(conn, seed):
    # hardware: 3 recent, 0 baseline -> emerging
    for i in range(3):
        _add(seed, 200 + i, "hardware", hours_ago=10)
    out = mom.compute(conn, _cfg(), NOW)
    assert out["hardware"]["emerging"] is True
    assert out["hardware"]["volume_baseline"] == 0


def test_not_emerging_below_threshold(conn, seed):
    # only 2 recent, 0 baseline, threshold 3 -> not emerging
    for i in range(2):
        _add(seed, 300 + i, "security", hours_ago=10)
    out = mom.compute(conn, _cfg(emerging_min_recent=3), NOW)
    assert out["security"]["emerging"] is False


def test_multipliers_clamped_and_monotone(conn, seed):
    lo, hi = 0.85, 1.25
    m = mom._multiplier
    assert m(0.0, lo, hi) == lo
    assert lo <= m(1.0, lo, hi) <= hi
    assert m(100.0, lo, hi) == hi           # clamped at ceiling
    # monotone non-decreasing
    vals = [m(x, lo, hi) for x in (0.0, 0.5, 1.0, 2.0, 3.0, 10.0)]
    assert vals == sorted(vals)


def test_empty_db_is_noop(conn):
    out = mom.compute(conn, _cfg(), NOW)
    assert out == {}
    assert mom.importance_multipliers(out, _cfg()) == {}


def test_artifact_and_load_multipliers(conn, seed, tmp_path):
    for i in range(4):
        _add(seed, 400 + i, "ai", hours_ago=12)
    rel, content = mom.momentum_artifact(conn, _cfg(), NOW)
    assert rel == "kb/momentum.json"
    (tmp_path / "kb").mkdir()
    (tmp_path / rel).write_text(content)
    mult = mom.load_multipliers(tmp_path)
    assert "ai" in mult and 0.85 <= mult["ai"] <= 1.25


def test_load_multipliers_missing_is_empty(tmp_path):
    assert mom.load_multipliers(tmp_path) == {}


def test_apply_multiplier():
    # no map -> no-op (identical scoring when disabled/absent)
    assert mom.apply_multiplier(1.0, "ai", {}) == 1.0
    # matching category scales
    assert mom.apply_multiplier(1.0, "ai", {"ai": 1.25}) == 1.25
    # non-matching category untouched
    assert mom.apply_multiplier(0.7, "hardware", {"ai": 1.25}) == 0.7
