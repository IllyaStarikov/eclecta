"""Tests for signalpipe.dedup — deterministic two-stage clustering.

Pure helpers (title_tokens/title_key/story_id/jaccard/_now_iso) get direct unit
tests. assign_cluster and refresh_surface_counts are exercised against a real
sqlite schema (the ``conn`` fixture applies db.SCHEMA v5) with both wall clocks
frozen: dedup reads the clock two ways — ``_now_iso()`` for stored timestamps and
an inline ``datetime.datetime.now(tz)`` for the near-dup cutoff — so a single fake
``dedup.datetime`` namespace pins both consistently.
"""

from __future__ import annotations

import datetime
import types

import pytest

from signalpipe import dedup

# Same base instant the conftest Seeder uses for its ``_iso`` offsets, so seeded
# rows (last_seen=_iso(-1), etc.) line up with the frozen dedup clock.
BASE = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)


def iso(offset_hours: float = 0.0) -> str:
    return (BASE + datetime.timedelta(hours=offset_hours)).isoformat()


def _fake_datetime_ns(fixed: datetime.datetime) -> types.SimpleNamespace:
    """A stand-in for the ``datetime`` module whose ``datetime.now(tz)`` always
    returns ``fixed``; ``timedelta``/``timezone`` pass through to the real ones."""
    fake_dt = types.SimpleNamespace(now=lambda tz=None: fixed)
    return types.SimpleNamespace(
        datetime=fake_dt,
        timedelta=datetime.timedelta,
        timezone=datetime.timezone,
    )


def freeze(monkeypatch, fixed: datetime.datetime = BASE) -> datetime.datetime:
    """Freeze both of dedup's clocks to ``fixed`` (see module docstring)."""
    monkeypatch.setattr(dedup, "datetime", _fake_datetime_ns(fixed))
    return fixed


@pytest.fixture
def frozen(monkeypatch):
    """Freeze the dedup clock at BASE for the duration of a test."""
    return freeze(monkeypatch)


# --------------------------------------------------------------------------- #
# title_tokens
# --------------------------------------------------------------------------- #
def test_title_tokens_stopwords_short_and_case():
    # 'the'/'new'/'via'/'show'/'hn' are stopwords; single-char tokens drop; the
    # rest lowercase and split on [a-z0-9]+.
    assert dedup.title_tokens("The New AI Model") == {"ai", "model"}


def test_title_tokens_domain_stopwords_collapse():
    # 'show'/'hn'/'new'/'ask'/'via' all stripped -> only content tokens remain.
    assert dedup.title_tokens("Show HN: New Foo") == {"foo"}
    assert dedup.title_tokens("Ask HN via Show") == set()


@pytest.mark.parametrize(
    "title,expected",
    [
        ("", set()),
        (None, set()),  # title or "" guard tolerates None
        ("   ", set()),
        ("A B C", set()),  # all single-char -> dropped
        ("Rust 2.0 released!", {"rust", "released"}),  # 2 and 0 are single-char -> dropped
        ("foo-bar_baz", {"foo", "bar", "baz"}),  # punctuation/underscore split
        ("GPT4 model", {"gpt4", "model"}),  # alnum token kept whole
    ],
)
def test_title_tokens_table(title, expected):
    assert dedup.title_tokens(title) == expected


def test_title_tokens_numeric_multichar_survive():
    # A pure-digit token longer than one char survives (len>1, not a stopword).
    assert dedup.title_tokens("Report 2024 edition") == {"report", "2024", "edition"}


# --------------------------------------------------------------------------- #
# title_key
# --------------------------------------------------------------------------- #
def test_title_key_sorted_and_order_invariant():
    assert dedup.title_key("Foo Bar") == "bar foo"
    assert dedup.title_key("bar foo") == "bar foo"
    assert dedup.title_key("Foo Bar") == dedup.title_key("Bar Foo")


def test_title_key_empty_and_whitespace():
    assert dedup.title_key("") == ""
    assert dedup.title_key("   ") == ""
    assert dedup.title_key("The And Or") == ""  # all stopwords


def test_title_key_dedupes_repeated_tokens():
    # title_tokens returns a set, so repeats collapse and the key is unique-sorted.
    assert dedup.title_key("model model MODEL alpha") == "alpha model"


# --------------------------------------------------------------------------- #
# story_id
# --------------------------------------------------------------------------- #
def test_story_id_url_pins_exact_sha1():
    # Regression pin: basis = registered_domain|path.rstrip('/') for a URL.
    assert dedup.story_id("https://example.com/story", "ignored-key") == "s_6b09ccb24d2975a6"


def test_story_id_titlekey_fallback_pins_exact_sha1():
    # canonical_url=None -> basis 'titlekey|<key>'.
    assert dedup.story_id(None, "bar foo") == "s_b420930c6f81de1e"
    assert dedup.story_id("", "bar foo") == "s_b420930c6f81de1e"  # falsy url -> fallback


def test_story_id_ignores_title_key_when_url_present():
    a = dedup.story_id("https://example.com/story", "one key")
    b = dedup.story_id("https://example.com/story", "totally different key")
    assert a == b == "s_6b09ccb24d2975a6"


def test_story_id_www_and_trailing_slash_collapse_but_query_distinguishes():
    # registered_domain strips www and the path rstrips '/', so www and a
    # trailing slash collapse — but a surviving query string is PART of the id
    # (else every youtube.com/watch?v=... collides). Tracking params never get
    # here: canonicalize() strips them before story_id sees the URL.
    base = dedup.story_id("https://example.com/story", "k")
    assert dedup.story_id("https://www.example.com/story", "k") == base
    assert dedup.story_id("https://example.com/story/", "k") == base
    assert dedup.story_id("https://example.com/story?v=abc", "k") != base
    assert dedup.story_id("https://example.com/story?v=abc", "k") == dedup.story_id(
        "https://www.example.com/story?v=abc", "k"
    )


def test_story_id_different_paths_differ():
    assert dedup.story_id("https://example.com/story", "k") != dedup.story_id(
        "https://example.com/other", "k"
    )


def test_story_id_titlekey_empty_basis():
    assert dedup.story_id(None, "") == "s_3b76b21d1d4e13d8"  # basis 'titlekey|'


# --------------------------------------------------------------------------- #
# jaccard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "a,b,expected",
    [
        (set(), {"a"}, 0.0),
        ({"a"}, set(), 0.0),
        (set(), set(), 0.0),
        ({"a", "b"}, {"a", "b"}, 1.0),
        ({"a", "b"}, {"a", "c"}, 1.0 / 3.0),
        ({"a"}, {"b"}, 0.0),  # disjoint, non-empty
        ({"a", "b", "c", "d"}, {"a", "b", "c", "d", "e"}, 0.8),  # 4/5
    ],
)
def test_jaccard_table(a, b, expected):
    assert dedup.jaccard(a, b) == pytest.approx(expected)


def test_jaccard_symmetric():
    a, b = {"x", "y", "z"}, {"y", "z", "w"}
    assert dedup.jaccard(a, b) == dedup.jaccard(b, a)


# --------------------------------------------------------------------------- #
# _now_iso
# --------------------------------------------------------------------------- #
def test_now_iso_reads_module_clock(monkeypatch):
    freeze(monkeypatch)
    assert dedup._now_iso() == BASE.isoformat()


def test_now_iso_is_tz_aware_iso_string():
    parsed = datetime.datetime.fromisoformat(dedup._now_iso())
    assert parsed.tzinfo is not None


# --------------------------------------------------------------------------- #
# assign_cluster — Stage 1 (exact canonical-URL identity)
# --------------------------------------------------------------------------- #
def _count_clusters(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM clusters").fetchone()["n"]


def _cluster(conn, cid: int):
    return conn.execute("SELECT * FROM clusters WHERE id=?", (cid,)).fetchone()


@pytest.mark.integration
def test_assign_stage1_exact_url_returns_existing_and_bumps_last_seen(conn, seed, frozen):
    cid = seed.cluster(
        canonical_url="https://example.com/story",
        title="Original headline about widgets",
        last_seen=iso(-5),
    )
    got = dedup.assign_cluster(
        conn, "Completely different words here", "https://example.com/story", None, {}
    )
    assert got == cid
    assert _count_clusters(conn) == 1  # no new row
    assert _cluster(conn, cid)["last_seen"] == BASE.isoformat()  # bumped to frozen now


@pytest.mark.integration
def test_assign_stage1_new_url_creates_row_with_story_id(conn, seed, frozen):
    seed.cluster(canonical_url="https://example.com/story", title="Original headline")
    title = "Zebra giraffe elephant rhinoceros wildebeest"
    got = dedup.assign_cluster(conn, title, "https://other.org/new", None, {})
    assert _count_clusters(conn) == 2
    row = _cluster(conn, got)
    assert row["canonical_url"] == "https://other.org/new"
    # Concrete pins (not re-derived via the SUT): title normalizes to the
    # sorted, lowercased, stopword-free token key; story_id hashes
    # 'other.org|/new' (registered domain + path).
    assert row["title_key"] == "elephant giraffe rhinoceros wildebeest zebra"
    assert row["story_id"] == "s_7da0e1915d600e54"
    # first_seen falls back to now when published_at is None; both == frozen now.
    assert row["first_seen"] == BASE.isoformat()
    assert row["last_seen"] == BASE.isoformat()
    assert row["surface_count"] == 0
    assert row["merge_reason"] is None


@pytest.mark.integration
def test_assign_stage1_takes_priority_over_title_similarity(conn, seed, frozen):
    # Even a byte-identical title on a matching canonical URL is a Stage-1 hit,
    # not a Stage-2 merge (no merge_reason gets written).
    cid = seed.cluster(
        canonical_url="https://example.com/story",
        title="Alpha beta gamma delta",
        title_key="alpha beta delta gamma",
    )
    got = dedup.assign_cluster(
        conn, "Alpha beta gamma delta", "https://example.com/story", None, {}
    )
    assert got == cid
    assert _cluster(conn, cid)["merge_reason"] is None


@pytest.mark.integration
def test_assign_uses_published_at_for_first_seen(conn, frozen):
    published = "2026-06-01T00:00:00+00:00"
    got = dedup.assign_cluster(
        conn, "Solo article about quantum", "https://solo.example/a", published, {}
    )
    row = _cluster(conn, got)
    assert row["first_seen"] == published
    assert row["last_seen"] == BASE.isoformat()


# --------------------------------------------------------------------------- #
# assign_cluster — Stage 2 (gated title-Jaccard near-dup)
# --------------------------------------------------------------------------- #
# Token construction: a 4-token seeded key vs a 5-token incoming superset gives
# jaccard = 4/5 = 0.80 (>= same-domain 0.80, < cross-domain 0.92). Identical sets
# give 1.0 (>= both thresholds).
SEED_KEY = "alpha beta delta gamma"  # sorted 4-token key
INCOMING_080 = "Alpha Beta Gamma Delta Epsilon"  # 5 tokens, sim 0.80 vs SEED_KEY
INCOMING_100 = "Alpha Beta Gamma Delta"  # 4 tokens, sim 1.00 vs SEED_KEY


@pytest.mark.integration
def test_assign_stage2_same_domain_merges_at_080(conn, seed, frozen):
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-1),
    )
    got = dedup.assign_cluster(conn, INCOMING_080, "https://example.com/b", None, {})
    assert got == cid  # merged into the seeded cluster
    assert _count_clusters(conn) == 1
    row = _cluster(conn, cid)
    assert row["merge_reason"] == "title-jaccard 0.80 (same-domain)"
    assert row["last_seen"] == BASE.isoformat()
    # COALESCE keeps the cluster's existing canonical URL (it already had one).
    assert row["canonical_url"] == "https://example.com/a"


@pytest.mark.integration
def test_assign_stage2_cross_domain_no_merge_between_thresholds(conn, seed, frozen):
    # sim 0.80 on a DIFFERENT registered domain: below cross-domain 0.92 -> no
    # merge (the module's deliberate under-merge bias).
    seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-1),
    )
    got = dedup.assign_cluster(conn, INCOMING_080, "https://different.org/x", None, {})
    assert _count_clusters(conn) == 2  # a brand-new cluster
    assert _cluster(conn, got)["merge_reason"] is None


@pytest.mark.integration
def test_assign_stage2_cross_domain_merges_at_092_plus(conn, seed, frozen):
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-1),
    )
    got = dedup.assign_cluster(conn, INCOMING_100, "https://different.org/x", None, {})
    assert got == cid
    assert _count_clusters(conn) == 1
    assert _cluster(conn, cid)["merge_reason"] == "title-jaccard 1.00 (cross-domain)"


@pytest.mark.integration
def test_assign_stage2_time_window_excludes_stale_cluster(conn, seed, frozen):
    # A byte-perfect title match, but last_seen is 72h old (> 48h window) -> the
    # candidate scan (WHERE last_seen >= cutoff) skips it -> new cluster.
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-72),
    )
    got = dedup.assign_cluster(conn, INCOMING_100, "https://example.com/b", None, {})
    assert got != cid
    assert _count_clusters(conn) == 2


@pytest.mark.integration
def test_assign_stage2_time_window_boundary_included(conn, seed, frozen):
    # last_seen exactly at the cutoff (48h back) satisfies '>= cutoff' -> merges.
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-48),
    )
    got = dedup.assign_cluster(conn, INCOMING_100, "https://example.com/b", None, {})
    assert got == cid
    assert _count_clusters(conn) == 1


@pytest.mark.integration
def test_assign_stage2_coalesce_attaches_url_to_selfpost_cluster(conn, seed, frozen):
    # Self-post cluster with NULL canonical_url. A later item on the same story
    # with a real article URL merges (needs cross threshold since c_domain=None)
    # and back-fills clusters.canonical_url via COALESCE.
    cid = seed.cluster(
        canonical_url=None,
        title_key=SEED_KEY,
        last_seen=iso(-1),
    )
    assert _cluster(conn, cid)["canonical_url"] is None
    got = dedup.assign_cluster(conn, INCOMING_100, "https://news.example/article", None, {})
    assert got == cid
    row = _cluster(conn, cid)
    assert row["canonical_url"] == "https://news.example/article"
    assert row["merge_reason"] == "title-jaccard 1.00 (cross-domain)"


@pytest.mark.integration
def test_assign_stage2_merge_reason_not_overwritten(conn, seed, frozen):
    # COALESCE(merge_reason, ?) preserves an existing audit reason on re-merge.
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-1),
        merge_reason="pre-existing reason",
    )
    got = dedup.assign_cluster(conn, INCOMING_100, "https://example.com/b", None, {})
    assert got == cid
    assert _cluster(conn, cid)["merge_reason"] == "pre-existing reason"


@pytest.mark.integration
def test_assign_none_url_skips_stage1_and_merges_without_attaching(conn, seed, frozen):
    # Incoming canonical_url=None: Stage 1 is skipped outright, and on a Stage-2
    # merge the URL-attach COALESCE is skipped too (nothing to attach), leaving
    # the cluster's existing URL intact.
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-1),
    )
    got = dedup.assign_cluster(conn, INCOMING_100, None, None, {})
    assert got == cid
    assert _count_clusters(conn) == 1
    row = _cluster(conn, cid)
    assert row["canonical_url"] == "https://example.com/a"  # unchanged
    assert row["merge_reason"] == "title-jaccard 1.00 (cross-domain)"


@pytest.mark.integration
def test_assign_none_url_creates_titlekey_only_cluster(conn, frozen):
    # No existing clusters and no URL -> a fresh title-key-only cluster whose
    # story_id uses the 'titlekey|' basis.
    title = "Discussion about local llm quantization tricks"
    got = dedup.assign_cluster(conn, title, None, None, {})
    row = _cluster(conn, got)
    assert row["canonical_url"] is None
    # No URL -> story_id hashes 'titlekey|<sorted token key>'. Pinned to the
    # concrete key/hash rather than re-derived through the SUT's own helpers.
    assert row["title_key"] == "about discussion llm local quantization tricks"
    assert row["story_id"] == "s_206ea565a8e92c4d"


@pytest.mark.integration
def test_assign_no_tokens_skips_stage2_and_creates_cluster(conn, seed, frozen):
    # An all-stopword title yields no tokens, so the Stage-2 scan is skipped
    # entirely even though a similar cluster exists; a new cluster is created.
    seed.cluster(canonical_url="https://example.com/a", title_key=SEED_KEY, last_seen=iso(-1))
    got = dedup.assign_cluster(conn, "The And Or", "https://example.com/b", None, {})
    assert _count_clusters(conn) == 2
    assert _cluster(conn, got)["title_key"] == ""


@pytest.mark.integration
def test_assign_best_match_wins_among_candidates(conn, seed, frozen):
    # Two in-window candidates both clear threshold; the higher-sim one wins.
    weak = seed.cluster(
        canonical_url="https://example.com/weak",
        title_key=SEED_KEY,  # sim 0.80 vs INCOMING_080
        last_seen=iso(-1),
    )
    strong = seed.cluster(
        canonical_url="https://example.com/strong",
        title_key="alpha beta delta epsilon gamma",  # sim 1.00 vs INCOMING_080
        last_seen=iso(-2),
    )
    got = dedup.assign_cluster(conn, INCOMING_080, "https://example.com/new", None, {})
    assert got == strong
    assert got != weak
    assert _cluster(conn, strong)["merge_reason"] == "title-jaccard 1.00 (same-domain)"


# --------------------------------------------------------------------------- #
# assign_cluster — cfg_dedup threshold plumbing
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_assign_empty_cfg_uses_default_thresholds(conn, seed, frozen):
    # cfg_dedup={} -> 48h / 0.80 / 0.92 fallbacks; a same-domain 0.80 match merges.
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-1),
    )
    got = dedup.assign_cluster(conn, INCOMING_080, "https://example.com/b", None, {})
    assert got == cid


@pytest.mark.integration
def test_assign_custom_same_domain_threshold_blocks_merge(conn, seed, frozen):
    # Raise the same-domain threshold above 0.80: the 0.80 match no longer merges.
    seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-1),
    )
    got = dedup.assign_cluster(
        conn,
        INCOMING_080,
        "https://example.com/b",
        None,
        {"title_jaccard_same_domain": 0.95},
    )
    assert _count_clusters(conn) == 2
    assert _cluster(conn, got)["merge_reason"] is None


@pytest.mark.integration
def test_assign_custom_window_excludes_recent_cluster(conn, seed, frozen):
    # Shrink the window to 1h: a 5h-old identical cluster now falls outside it.
    cid = seed.cluster(
        canonical_url="https://example.com/a",
        title_key=SEED_KEY,
        last_seen=iso(-5),
    )
    got = dedup.assign_cluster(
        conn,
        INCOMING_100,
        "https://example.com/b",
        None,
        {"near_dup_window_hours": 1},
    )
    assert got != cid
    assert _count_clusters(conn) == 2


# --------------------------------------------------------------------------- #
# refresh_surface_counts
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_refresh_surface_counts_recomputes_from_surfaces(conn, seed):
    s1 = seed.source(slug="src1")
    s2 = seed.source(slug="src2")
    # Two surfaces (distinct sources), one surface, and zero surfaces.
    c_two = seed.cluster(canonical_url="https://ex.com/two", surface_count=99)
    c_one = seed.cluster(canonical_url="https://ex.com/one", surface_count=0)
    c_zero = seed.cluster(canonical_url="https://ex.com/zero", surface_count=7)

    seed.surface(c_two, s1, url="https://hn/1")
    seed.surface(c_two, s2, url="https://lo/1")
    seed.surface(c_one, s1, url="https://hn/2")

    dedup.refresh_surface_counts(conn)

    assert _cluster(conn, c_two)["surface_count"] == 2
    assert _cluster(conn, c_one)["surface_count"] == 1
    assert _cluster(conn, c_zero)["surface_count"] == 0


@pytest.mark.integration
def test_refresh_surface_counts_noop_on_empty_db(conn):
    # No clusters -> UPDATE touches nothing and does not raise.
    dedup.refresh_surface_counts(conn)
    assert _count_clusters(conn) == 0


# --------------------------------------------------------------------------- #
# Property: the module's stated under-merge safety invariant
# --------------------------------------------------------------------------- #
@pytest.mark.property
def test_property_disjoint_titles_never_over_merge(conn, monkeypatch):
    """Disjoint token sets on different domains (jaccard 0, well below the
    cross-domain 0.92 gate) must NEVER be merged into the same cluster."""
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    freeze(monkeypatch)

    # Two disjoint alphabets guarantee the two titles share no tokens.
    words_a = st.text(alphabet="abcdef", min_size=2, max_size=6)
    words_b = st.text(alphabet="uvwxyz", min_size=2, max_size=6)

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        st.lists(words_a, min_size=1, max_size=5),
        st.lists(words_b, min_size=1, max_size=5),
    )
    def inner(a_words, b_words):
        conn.execute("DELETE FROM clusters")
        title_a = " ".join(a_words)
        title_b = " ".join(b_words)
        # Both titles must survive tokenization (non-empty, disjoint) for the
        # invariant to be meaningful.
        ta, tb = dedup.title_tokens(title_a), dedup.title_tokens(title_b)
        hypothesis.assume(ta and tb and not (ta & tb))

        seeded = dedup.assign_cluster(conn, title_a, "https://aaa.example/x", None, {})
        got = dedup.assign_cluster(conn, title_b, "https://bbb.example/y", None, {})
        assert got != seeded

    inner()
