"""Tests for :mod:`signalpipe.score`.

Deterministic 0..10 cluster scoring (consensus / engagement / reputation /
recency / topic) plus the SQL that selects curation finalists. No LLM, no
network. The pure helpers are exercised directly; ``run`` and ``finalists``
run against a real on-disk WAL DB (via the ``conn``/``seed``/``cfg`` fixtures)
with the clock frozen so window cutoffs and recency decay are deterministic.
"""

from __future__ import annotations

import datetime
import json
import math
import types

import pytest

import signalpipe.score as score
import signalpipe.topics as topics_mod

# --------------------------------------------------------------------------- #
# Frozen-clock anchor. Matches conftest._iso so seeded timestamps line up.
# --------------------------------------------------------------------------- #
FIXED_NOW = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)
FIXED_TS = FIXED_NOW.timestamp()
FIXED_ISO = FIXED_NOW.isoformat()
LN2 = math.log(2)


def iso(offset_hours: float = 0.0) -> str:
    return (FIXED_NOW + datetime.timedelta(hours=offset_hours)).isoformat()


def _freeze_clock(monkeypatch):
    """Freeze ``score``'s two non-injectable clocks: ``datetime.datetime.now``
    (used for the window cutoff / ``now_iso`` / finalists retry bound) and
    ``time.time`` (used for ``now_ts`` and the elapsed banner). Both resolve to
    ``FIXED_NOW`` / ``FIXED_TS``. ``fromisoformat`` is left real so ``_recency``
    still parses stored timestamps."""
    fake_datetime_cls = types.SimpleNamespace(
        now=lambda tz=None: FIXED_NOW,
        fromisoformat=datetime.datetime.fromisoformat,
    )
    fake_dt = types.SimpleNamespace(
        datetime=fake_datetime_cls,
        timezone=datetime.timezone,
        timedelta=datetime.timedelta,
    )
    monkeypatch.setattr(score, "datetime", fake_dt)
    monkeypatch.setattr(score, "time", types.SimpleNamespace(time=lambda: FIXED_TS))


# ═══════════════════════════════════════════════════════════════════════════
# _consensus
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("n", [-5, 0, 1])
def test_consensus_below_two_is_zero(n):
    assert score._consensus(n) == 0.0


def test_consensus_two_surfaces_value():
    # 1 - exp(-0.5), pinned as a literal so a formula change is caught here.
    assert score._consensus(2) == pytest.approx(0.3934693402873666, abs=1e-12)


def test_consensus_formula_matches_closed_form():
    # Concrete literals for 1 - exp(-(n-1)/2): re-deriving with the same
    # closed form the SUT uses would be tautological (would pass even if the
    # SUT's exponent were wrong), so the expected values are hard-coded.
    expected = {
        2: 0.3934693402873666,
        3: 0.6321205588285577,
        5: 0.8646647167633873,
        8: 0.9698026165776815,
    }
    for n, want in expected.items():
        assert score._consensus(n) == pytest.approx(want, abs=1e-12)


def test_consensus_monotone_and_bounded():
    vals = [score._consensus(n) for n in range(2, 31)]
    assert all(0.0 < v < 1.0 for v in vals)
    # strictly increasing across the whole small range
    assert all(b > a for a, b in zip(vals, vals[1:]))
    assert score._consensus(2) < score._consensus(10) < score._consensus(30)


@pytest.mark.property
def test_consensus_property_monotone_bounded():
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    @settings(max_examples=200)
    @given(st.integers(min_value=2, max_value=30))
    def check(n):
        v = score._consensus(n)
        assert 0.0 < v < 1.0
        assert score._consensus(n) < score._consensus(n + 1)

    check()


# ═══════════════════════════════════════════════════════════════════════════
# _engagement
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "points,comments",
    [(None, None), (0, 0), (-10, -3), (None, -1), (-1, None), (0, None)],
)
def test_engagement_nonpositive_is_zero(points, comments):
    assert score._engagement(points, comments) == 0.0


def test_engagement_points_saturate_at_weight():
    # log10(1001)/3 > 1 -> p_score capped at 1.0 -> 0.7 * 1.0
    assert score._engagement(1000, 0) == pytest.approx(0.7)
    assert score._engagement(10**6, 0) == pytest.approx(0.7)


def test_engagement_comments_saturate_at_weight():
    # log10(1001)/2.7 > 1 -> c_score capped at 1.0 -> 0.3 * 1.0
    assert score._engagement(0, 1000) == pytest.approx(0.3)


def test_engagement_mixed_value_matches_formula():
    # p=100 -> 0.7*log10(101)/3, c=42 -> 0.3*log10(43)/2.7. Pinned as a
    # literal rather than re-derived with the SUT's own min/log10 expression.
    assert score._engagement(100, 42) == pytest.approx(0.6491714822803484, abs=1e-12)


def test_engagement_points_only_partial():
    # p=9 -> log10(10)/3 = 1/3 -> 0.7/3 ; c=0 -> 0
    assert score._engagement(9, 0) == pytest.approx(0.2333333333333333, abs=1e-12)


def test_engagement_bounded_over_grid():
    for p in (-5, 0, 1, 5, 50, 500, 5000, 10**7):
        for c in (-5, 0, 1, 5, 50, 500, 5000, 10**7):
            v = score._engagement(p, c)
            assert 0.0 <= v <= 1.0


@pytest.mark.property
def test_engagement_property_bounded():
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    ints = st.one_of(st.none(), st.integers(min_value=-(10**9), max_value=10**9))

    @settings(max_examples=300)
    @given(ints, ints)
    def check(p, c):
        v = score._engagement(p, c)
        assert 0.0 <= v <= 1.0

    check()


# ═══════════════════════════════════════════════════════════════════════════
# _recency
# ═══════════════════════════════════════════════════════════════════════════
def test_recency_age_zero_is_one():
    assert score._recency(FIXED_ISO, 18.0, FIXED_TS) == pytest.approx(1.0)


def test_recency_one_halflife_is_half():
    assert score._recency(iso(-18), 18.0, FIXED_TS) == pytest.approx(0.5)


def test_recency_two_halflives_is_quarter():
    assert score._recency(iso(-36), 18.0, FIXED_TS) == pytest.approx(0.25)


def test_recency_future_timestamp_clamped_to_one():
    # age is clamped to >= 0, so a future last_seen yields full recency.
    assert score._recency(iso(5), 18.0, FIXED_TS) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-99", 12345, 3.14])
def test_recency_malformed_returns_half(bad):
    assert score._recency(bad, 18.0, FIXED_TS) == 0.5


def test_recency_accepts_z_suffix():
    # 'Z' must be normalized to +00:00; a mishandled 'Z' would raise and fall
    # back to 0.5, so pin the real 1-hour/hl-18 decay value, not just parity.
    z = score._recency("2026-07-04T11:00:00Z", 18.0, FIXED_TS)
    plus = score._recency(iso(-1), 18.0, FIXED_TS)
    assert z == pytest.approx(0.9622238368941451, abs=1e-12)
    assert z == pytest.approx(plus)


@pytest.mark.parametrize("halflife", [0.0, 0.5, -100.0])
def test_recency_halflife_floored_at_one(halflife):
    # max(1.0, h) means any halflife <= 1 behaves like exactly 1 hour.
    got = score._recency(iso(-1), halflife, FIXED_TS)
    ref = score._recency(iso(-1), 1.0, FIXED_TS)
    assert got == pytest.approx(ref)
    assert got == pytest.approx(math.exp(-1.0 * LN2 / 1.0))  # one hour, hl=1 -> 0.5


# ═══════════════════════════════════════════════════════════════════════════
# latin_ratio + MIN_LATIN_RATIO
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "title,expected",
    [
        ("Hello", 1.0),
        ("Hello, World! 2026", 1.0),
        ("", 1.0),
        (None, 1.0),
        ("12345 !!! ---", 1.0),
        ("世界人", 0.0),
        ("Привет", 0.0),
    ],
)
def test_latin_ratio_values(title, expected):
    assert score.latin_ratio(title) == pytest.approx(expected)


def test_latin_ratio_mixed_scripts():
    # 5 ascii letters (Hello) + 2 CJK letters -> 5/7
    assert score.latin_ratio("Hello 世界") == pytest.approx(5.0 / 7.0)
    # 1 ascii letter (A) + 4 CJK -> 1/5
    assert score.latin_ratio("A 世界人民") == pytest.approx(1.0 / 5.0)


def test_min_latin_ratio_gate():
    assert score.MIN_LATIN_RATIO == 0.5
    assert score.latin_ratio("Hello 世界") >= score.MIN_LATIN_RATIO
    assert score.latin_ratio("A 世界人民") < score.MIN_LATIN_RATIO
    assert score.latin_ratio("世界人") < score.MIN_LATIN_RATIO


# ═══════════════════════════════════════════════════════════════════════════
# run()  — integration (real on-disk WAL DB, frozen clock)
# ═══════════════════════════════════════════════════════════════════════════
EXACT_TITLE = "Homemade jam and pickles for winter"
TOPIC0_TITLE = "Sunday afternoon baking bread at home"
TOPIC07_TITLE = "Linux users share weekend stories"
TOPIC10_TITLE = "OpenAI releases a new frontier model benchmark"
NONLATIN_TITLE = "人工知能のニュース"


@pytest.mark.integration
def test_run_scores_window_gates_nonlatin_and_topic_branches(cfg, conn, seed, monkeypatch, capsys):
    _freeze_clock(monkeypatch)

    # Guard the topic-branch assumptions this test's deltas rely on, so a
    # lexicon change fails loudly here rather than silently skewing a delta.
    td = topics_mod.build_or_load(cfg)
    assert topics_mod.match_channels(EXACT_TITLE, td) == set()
    assert topics_mod.match_channels(TOPIC0_TITLE, td) == set()
    ch07 = sorted(topics_mod.match_channels(TOPIC07_TITLE, td))
    assert ch07 and topics_mod.match_taxonomy(TOPIC07_TITLE, ch07)["subcategories"] == []
    ch10 = sorted(topics_mod.match_channels(TOPIC10_TITLE, td))
    assert topics_mod.match_taxonomy(TOPIC10_TITLE, ch10)["subcategories"]

    src = seed.source(slug="hn", reputation=1.0)

    exact = seed.cluster(
        canonical_url="https://ex.com/exact",
        title=EXACT_TITLE,
        surface_count=3,
        last_seen=iso(-1),
    )
    seed.surface(exact, src, points=100, comments=42)

    topic0 = seed.cluster(
        canonical_url="https://ex.com/t0", title=TOPIC0_TITLE, surface_count=1, last_seen=iso(-1)
    )
    topic07 = seed.cluster(
        canonical_url="https://ex.com/t07", title=TOPIC07_TITLE, surface_count=1, last_seen=iso(-1)
    )
    topic10 = seed.cluster(
        canonical_url="https://ex.com/t10", title=TOPIC10_TITLE, surface_count=1, last_seen=iso(-1)
    )
    nonlatin = seed.cluster(
        canonical_url="https://ex.com/jp", title=NONLATIN_TITLE, surface_count=1, last_seen=iso(-1)
    )
    out = seed.cluster(
        canonical_url="https://ex.com/old",
        title="Old story about jam recipes",
        surface_count=1,
        last_seen=iso(-100),
    )

    assert score.run(cfg, show=5) == 0

    def get(cid):
        return conn.execute("SELECT score, score_at FROM clusters WHERE id=?", (cid,)).fetchone()

    # Non-latin title: gated to exactly 0.0 but still time-stamped this run.
    r = get(nonlatin)
    assert r["score"] == 0.0
    assert r["score_at"] == FIXED_ISO

    # Out of the rolling window: never selected, score stays NULL.
    assert get(out)["score"] is None

    # Exact-score reconstruction of the weighted assembly + rounding.
    w = cfg.score_weights
    exp01 = (
        float(w["consensus"]) * score._consensus(3)
        + float(w["engagement"]) * score._engagement(100, 42)
        + float(w["reputation"]) * min(1.0, 1.0 / 1.5)
        + float(w["recency"]) * score._recency(iso(-1), 18.0, FIXED_TS)
        + float(w["topic_match"]) * 0.0
    )
    got_exact = get(exact)
    assert got_exact["score"] == pytest.approx(round(exp01 * 10.0, 3))
    # Concrete literal anchor: if any helper AND its mirror in exp01 regressed
    # together, the reconstruction above could still pass — this literal can't.
    assert got_exact["score"] == pytest.approx(5.963, abs=1e-9)
    assert got_exact["score_at"] == FIXED_ISO

    # Topic branch isolation: the three clusters differ ONLY in topic term.
    # delta == w_topic * topic * 10  (0.7 and 1.0 respectively).
    s0 = get(topic0)["score"]
    s07 = get(topic07)["score"]
    s10 = get(topic10)["score"]
    wt = float(w["topic_match"])
    assert s07 - s0 == pytest.approx(wt * 0.7 * 10.0, abs=5e-3)
    assert s10 - s0 == pytest.approx(wt * 1.0 * 10.0, abs=5e-3)

    printed = capsys.readouterr().out
    # 5 of the 6 seeded clusters are in-window (the iso(-100) "out" is not).
    assert "5 clusters scored (window 72h)" in printed
    assert "top 5:" in printed  # show=5 prints the leaderboard block


@pytest.mark.integration
def test_run_batches_over_500_and_records_side_effects(cfg, conn, seed, monkeypatch, capsys):
    _freeze_clock(monkeypatch)
    n = 501  # crosses the BATCH=500 boundary -> two write transactions
    for i in range(n):
        seed.cluster(
            canonical_url="https://ex.com/b/%d" % i,
            title="Story number %d about widgets" % i,
            surface_count=1,
            last_seen=iso(-1),
        )

    assert score.run(cfg, show=0) == 0

    scored = conn.execute("SELECT COUNT(*) FROM clusters WHERE score IS NOT NULL").fetchone()[0]
    assert scored == n

    # Orchestration side effects: health log, attributable run, last_run file.
    assert conn.execute("SELECT COUNT(*) FROM health WHERE job='score'").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE job='score'").fetchone()[0] == 1

    saved = json.loads(cfg.path.read_text())
    assert saved["last_run"]["job"] == "score"
    assert saved["last_run"]["stats"]["scored"] == n

    out = capsys.readouterr().out
    assert "501 clusters scored" in out
    assert "top" not in out  # show=0 suppresses the leaderboard block


@pytest.mark.integration
def test_run_scores_cluster_without_surfaces_using_defaults(cfg, conn, seed, monkeypatch):
    # A cluster with no surfaces: MAX(...) aggregates are NULL, so reputation
    # falls back to 1.0/1.5 and engagement to 0. Still scored (never NULL).
    _freeze_clock(monkeypatch)
    cid = seed.cluster(
        canonical_url="https://ex.com/bare",
        title="Sunday afternoon baking bread at home",
        surface_count=1,
        last_seen=iso(-1),
    )
    assert score.run(cfg, show=0) == 0

    row = conn.execute("SELECT score FROM clusters WHERE id=?", (cid,)).fetchone()
    w = cfg.score_weights
    expected01 = (
        float(w["consensus"]) * score._consensus(1)  # 0.0
        + float(w["engagement"]) * score._engagement(None, None)  # 0.0
        + float(w["reputation"]) * min(1.0, 1.0 / 1.5)
        + float(w["recency"]) * score._recency(iso(-1), 18.0, FIXED_TS)
        + float(w["topic_match"]) * 0.0
    )
    assert row["score"] == pytest.approx(round(expected01 * 10.0, 3))
    # Concrete literal anchor for the surfaces-absent path (rep 1/1.5 +
    # recency at 1h/hl-18 only, everything else zero): 0.15*(2/3)+0.15*decay.
    assert row["score"] == pytest.approx(2.443, abs=1e-9)


# ═══════════════════════════════════════════════════════════════════════════
# finalists()  — integration
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.integration
def test_finalists_threshold_and_uncurated(cfg, conn, seed):
    hi = seed.cluster(canonical_url="https://ex.com/hi", title="High score story", score=8.0)
    seed.cluster(canonical_url="https://ex.com/lo", title="Low score story", score=2.0)
    ids = [r["id"] for r in score.finalists(conn, cfg)]
    assert ids == [hi]  # min_score_to_curate = 3.5


@pytest.mark.integration
def test_finalists_score_boundary_is_inclusive(cfg, conn, seed):
    at = seed.cluster(canonical_url="https://ex.com/at", title="At boundary", score=3.5)
    below = seed.cluster(canonical_url="https://ex.com/bl", title="Below boundary", score=3.499)
    ids = [r["id"] for r in score.finalists(conn, cfg)]
    assert at in ids
    assert below not in ids


@pytest.mark.integration
def test_finalists_curation_states_and_retry_window(cfg, conn, seed, monkeypatch):
    _freeze_clock(monkeypatch)  # retry_before = FIXED_NOW - 6h = iso(-6)

    done = seed.cluster(canonical_url="https://ex.com/d", title="Done curated", score=9.0)
    seed.curation(done, status="done", curated_at=iso(-1))

    failed_old = seed.cluster(canonical_url="https://ex.com/fo", title="Failed old", score=7.0)
    seed.curation(failed_old, status="failed", curated_at=iso(-7))  # > 6h ago

    failed_recent = seed.cluster(
        canonical_url="https://ex.com/fr", title="Failed recent", score=6.0
    )
    seed.curation(failed_recent, status="failed", curated_at=iso(-1))  # < 6h ago

    uncur = seed.cluster(canonical_url="https://ex.com/u", title="Never curated", score=5.0)

    ids = [r["id"] for r in score.finalists(conn, cfg)]
    assert done not in ids  # succeeded -> not a finalist
    assert failed_recent not in ids  # failed too recently -> not yet retried
    assert failed_old in ids  # failed > 6h ago -> retried
    assert uncur in ids  # never curated -> eligible
    # ordering is by score DESC: failed_old (7.0) precedes uncur (5.0)
    assert ids.index(failed_old) < ids.index(uncur)


@pytest.mark.integration
def test_finalists_excludes_non_english_article(cfg, conn, seed):
    en = seed.cluster(canonical_url="https://ex.com/en", title="English", score=8.0)
    seed.article(en, lang="en")
    fr = seed.cluster(canonical_url="https://ex.com/fr", title="French", score=8.0)
    seed.article(fr, lang="fr")
    nolang = seed.cluster(canonical_url="https://ex.com/nl", title="No lang", score=7.0)
    seed.article(nolang, lang=None)
    emptylang = seed.cluster(canonical_url="https://ex.com/el", title="Empty lang", score=6.0)
    seed.article(emptylang, lang="")
    noart = seed.cluster(canonical_url="https://ex.com/na", title="No article", score=5.0)

    ids = {r["id"] for r in score.finalists(conn, cfg)}
    assert fr not in ids  # detected non-English -> excluded before LLM spend
    assert ids == {en, nolang, emptylang, noart}


@pytest.mark.integration
def test_finalists_ordering_and_limit(cfg, conn, seed):
    a = seed.cluster(canonical_url="https://ex.com/a", title="A", score=5.0)
    b = seed.cluster(canonical_url="https://ex.com/b", title="B", score=9.0)
    c = seed.cluster(canonical_url="https://ex.com/c", title="C", score=7.0)
    d = seed.cluster(canonical_url="https://ex.com/d2", title="D", score=3.6)

    # funnel.daily_finalists = 80 -> all four returned, score DESC.
    assert [r["id"] for r in score.finalists(conn, cfg)] == [b, c, a, d]
    # explicit limit honored
    assert [r["id"] for r in score.finalists(conn, cfg, limit=2)] == [b, c]
    # limit=0 is falsy -> falls back to the funnel default (all four)
    assert [r["id"] for r in score.finalists(conn, cfg, limit=0)] == [b, c, a, d]


@pytest.mark.integration
def test_finalists_defaults_when_funnel_keys_missing(conn, seed):
    # finalists only touches cfg.funnel; an empty funnel exercises the .get
    # defaults (min_score_to_curate=3.5, daily_finalists=40).
    fake_cfg = types.SimpleNamespace(funnel={})
    at = seed.cluster(canonical_url="https://ex.com/x", title="At default", score=3.5)
    below = seed.cluster(canonical_url="https://ex.com/y", title="Below default", score=3.4)
    ids = [r["id"] for r in score.finalists(conn, fake_cfg)]
    assert at in ids
    assert below not in ids
