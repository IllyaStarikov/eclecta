"""Tests for ``signalpipe.feed`` — parameterized RSS 2.0 generation.

Coverage strategy (derived by READING the source, not the docstring):

* ``parse_since`` — relative-window math is frozen by swapping ``feed.datetime``
  for a namespace whose ``datetime.now`` returns a fixed instant (the module
  computes ``now`` internally, so we cannot pass it). ISO parse + naive→UTC and
  the garbage→None branch are asserted directly.
* ``_rfc822`` — valid ISO, ``Z`` suffix, and the invalid/None → frozen-now
  fallback.
* ``_cdata`` — the ``]]>`` split that keeps CDATA well-formed.
* ``render_rss`` — a nearly-pure function exercised with hand-built dicts + a
  fake ``item_html`` map: guid format, uncurated title prefix, ``desc[:500]``
  truncation, dc:source suppression when it equals the link, content:encoded
  gating, saxutils escaping, and — critically — that ``archive_url`` never
  reaches the output.
* ``query_items`` / ``_surfaces_for`` — integration against a seeded tmp sqlite:
  limit clamp, channel guard (LIKE-injection defense), curated-first shape,
  relevance/score/since/source filters, and the scored-uncurated fallback with
  its topic-derived channel filtering.

Everything is hermetic — the only I/O boundary is sqlite (tmp DB via the shared
``conn``/``cfg``/``seed`` fixtures) and the clock (frozen by monkeypatch).
"""

from __future__ import annotations

import datetime
import email.utils
import types
import xml.etree.ElementTree as ET
from typing import Any, Dict
from xml.sax.saxutils import escape

import pytest

from signalpipe import feed


# --------------------------------------------------------------------------- #
# Clock freezing helper
# --------------------------------------------------------------------------- #
FROZEN = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)


class _FrozenDT:
    """Stand-in for ``datetime.datetime`` inside feed.py.

    ``feed`` only ever calls ``datetime.datetime.now`` and
    ``datetime.datetime.fromisoformat``; ``fromisoformat`` delegates to the real
    class so ISO parsing is untouched, while ``now`` returns a fixed instant.
    """

    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is None else FROZEN.astimezone(tz)

    @staticmethod
    def fromisoformat(s):
        return datetime.datetime.fromisoformat(s)


def _freeze_feed_clock(monkeypatch):
    fake = types.SimpleNamespace(
        datetime=_FrozenDT,
        timedelta=datetime.timedelta,
        timezone=datetime.timezone,
    )
    monkeypatch.setattr(feed, "datetime", fake)
    return FROZEN


@pytest.fixture
def frozen_clock(monkeypatch):
    return _freeze_feed_clock(monkeypatch)


# --------------------------------------------------------------------------- #
# parse_since
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,delta",
    [
        ("7d", datetime.timedelta(days=7)),
        ("24h", datetime.timedelta(hours=24)),
        ("30m", datetime.timedelta(minutes=30)),   # m == minutes, not months
        ("2w", datetime.timedelta(weeks=2)),
        ("1h", datetime.timedelta(hours=1)),
        ("0h", datetime.timedelta(hours=0)),
        ("  7D ", datetime.timedelta(days=7)),      # stripped + lowercased
    ],
)
def test_parse_since_relative_window(frozen_clock, raw, delta):
    assert feed.parse_since(raw) == (FROZEN - delta).isoformat()


def test_parse_since_naive_iso_gets_utc(frozen_clock):
    # A naive ISO date is stamped with +00:00.
    assert feed.parse_since("2026-01-15") == "2026-01-15T00:00:00+00:00"


def test_parse_since_aware_iso_preserved(frozen_clock):
    # An explicit offset is passed through unchanged.
    assert feed.parse_since("2026-01-15T08:00:00+02:00") == "2026-01-15T08:00:00+02:00"


@pytest.mark.parametrize("raw", [None, "", "garbage", "5y", "7dd", "d7", "  "])
def test_parse_since_invalid_returns_none(frozen_clock, raw):
    assert feed.parse_since(raw) is None


def test_parse_since_no_monkeypatch_is_structural():
    # Without freezing, "1h" must resolve to real-now minus exactly one hour
    # (tz-aware UTC), not merely "some instant in the past".
    before = datetime.datetime.now(datetime.timezone.utc)
    out = feed.parse_since("1h")
    after = datetime.datetime.now(datetime.timezone.utc)
    assert out is not None
    parsed = datetime.datetime.fromisoformat(out)
    # tz-aware, UTC-offset result.
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)
    # The internal `now` was captured between `before` and `after`, so
    # `now - 1h` is bracketed by [before-1h, after-1h]. This pins the unit
    # (hours, not days) and the sign (past, not future) to a tight window.
    hour = datetime.timedelta(hours=1)
    assert (before - hour) <= parsed <= (after - hour)


# --------------------------------------------------------------------------- #
# _rfc822
# --------------------------------------------------------------------------- #
def test_rfc822_valid_iso():
    assert feed._rfc822("2026-07-04T12:00:00+00:00") == "Sat, 04 Jul 2026 12:00:00 +0000"


def test_rfc822_z_suffix():
    # 'Z' is normalized to +00:00 before parsing.
    assert feed._rfc822("2026-07-04T12:00:00Z") == "Sat, 04 Jul 2026 12:00:00 +0000"


def test_rfc822_invalid_falls_back_to_now(frozen_clock):
    assert feed._rfc822("not-a-date") == email.utils.format_datetime(FROZEN)


def test_rfc822_none_falls_back_to_now(frozen_clock):
    assert feed._rfc822(None) == email.utils.format_datetime(FROZEN)


# --------------------------------------------------------------------------- #
# _cdata
# --------------------------------------------------------------------------- #
def test_cdata_plain():
    assert feed._cdata("<p>hi</p>") == "<![CDATA[<p>hi</p>]]>"


def test_cdata_splits_terminator():
    # A raw ']]>' inside the payload must be broken across two CDATA sections.
    out = feed._cdata("a]]>b")
    assert out == "<![CDATA[a]]]]><![CDATA[>b]]>"
    # And the wrapped result parses as valid XML text.
    root = ET.fromstring(("<x>%s</x>" % out).encode("utf-8"))
    assert root.text == "a]]>b"


# --------------------------------------------------------------------------- #
# render_rss
# --------------------------------------------------------------------------- #
def _curated_item(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": 1,
        "title": "Cats & Dogs <news>",
        "curated": True,
        "why_it_matters": 'Because "reasons" & <stuff>',
        "excerpt": "unused excerpt",
        "link": "https://ex.com/a?x=1&y=2",
        "source_url": "https://canonical.example/a",
        "curated_at": "2026-07-04T10:00:00+00:00",
        "last_seen": "2026-07-03T10:00:00+00:00",
        "score": 9.0,
        # INTERNAL ONLY — feed.render_rss must never emit this.
        "archive_url": "https://archive.today/SECRET",
    }
    base.update(over)
    return base


SELF_URL = "https://eclecta.co/feed.xml?channel=ai&limit=5"


def test_render_rss_channel_header(cfg, frozen_clock):
    xml = feed.render_rss([], {}, cfg, SELF_URL)
    assert "<title>Signal test feed</title>" in xml
    assert "<link>http://127.0.0.1:8765/</link>" in xml
    assert "<description>test feed</description>" in xml
    assert "<generator>signalpipe</generator>" in xml
    assert "<language>en-us</language>" in xml
    # lastBuildDate is _rfc822(None) -> frozen now.
    assert "<lastBuildDate>%s</lastBuildDate>" % email.utils.format_datetime(FROZEN) in xml
    # self URL goes through quoteattr (double-quoted, & escaped).
    assert (
        '<atom:link href="https://eclecta.co/feed.xml?channel=ai&amp;limit=5" '
        'rel="self" type="application/rss+xml"/>'
    ) in xml


def test_render_rss_curated_item_fields(cfg, frozen_clock):
    it = _curated_item()
    html = "<p>Body with ]]> sequence & <b>bold</b></p>"
    xml = feed.render_rss([it], {1: html}, cfg, SELF_URL)

    # Title escaped (& < >), quotes untouched by saxutils.escape.
    assert "<title>Cats &amp; Dogs &lt;news&gt;</title>" in xml
    # Link escaped.
    assert "<link>https://ex.com/a?x=1&amp;y=2</link>" in xml
    # guid uses GUID_FMT, isPermaLink=false.
    assert '<guid isPermaLink="false">tag:starikov.co,2026:signal/1</guid>' in xml
    # pubDate from curated_at.
    assert "<pubDate>Sat, 04 Jul 2026 10:00:00 +0000</pubDate>" in xml
    # description = why_it_matters, escaped, quotes preserved.
    assert '<description>Because "reasons" &amp; &lt;stuff&gt;</description>' in xml
    # dc:source present because source_url != link.
    assert "<dc:source>https://canonical.example/a</dc:source>" in xml
    # content:encoded present with a split CDATA payload (]]> broken).
    assert (
        "<content:encoded><![CDATA[<p>Body with ]]]]><![CDATA[> sequence "
        "& <b>bold</b></p>]]></content:encoded>"
    ) in xml


def test_render_rss_never_emits_archive_url(cfg, frozen_clock):
    xml = feed.render_rss([_curated_item()], {}, cfg, SELF_URL)
    assert "archive_url" not in xml
    assert "archive.today" not in xml
    assert "SECRET" not in xml


def test_render_rss_uncurated_prefix_and_excerpt_and_no_dc_source(cfg, frozen_clock):
    it = {
        "id": 2,
        "title": "GPU chips",
        "curated": False,
        "score": 7.5,
        "excerpt": "An excerpt",
        "why_it_matters": None,
        "link": "https://ex.com/b",
        "source_url": "https://ex.com/b",   # equal to link -> dc:source suppressed
        "last_seen": "2026-07-03T09:00:00+00:00",
        "curated_at": None,
    }
    xml = feed.render_rss([it], {}, cfg, SELF_URL)
    assert "<title>[uncurated 7.5] GPU chips</title>" in xml
    # Falls back to excerpt for the description.
    assert "<description>An excerpt</description>" in xml
    # pubDate from last_seen (no curated_at).
    assert "<pubDate>Fri, 03 Jul 2026 09:00:00 +0000</pubDate>" in xml
    # source_url == link -> no dc:source element.
    assert "<dc:source>" not in xml
    # No html supplied for id 2 -> no content:encoded.
    assert "<content:encoded>" not in xml


def test_render_rss_uncurated_score_none_prefix(cfg, frozen_clock):
    it = {
        "id": 9,
        "title": "Mystery",
        "curated": False,
        "score": None,
        "link": "https://ex.com/z",
        "last_seen": "2026-07-03T09:00:00+00:00",
    }
    xml = feed.render_rss([it], {}, cfg, SELF_URL)
    assert "<title>[uncurated 0.0] Mystery</title>" in xml


def test_render_rss_desc_truncated_and_no_link_and_dc_source_when_link_empty(cfg, frozen_clock):
    it = {
        "id": 3,
        "title": "Long",
        "curated": True,
        "why_it_matters": "z" * 600,
        "link": "",                     # falsy -> no <link> for this item
        "source_url": "https://src.example/z",
        "curated_at": None,             # pub None -> _rfc822(None) -> frozen now
        "last_seen": None,
    }
    xml = feed.render_rss([it], {}, cfg, SELF_URL)
    # description truncated to 500 chars.
    assert "<description>%s</description>" % ("z" * 500) in xml
    assert ("z" * 501) not in xml
    # dc:source present: source_url is truthy and != "" link.
    assert "<dc:source>https://src.example/z</dc:source>" in xml
    # pubDate falls back to frozen now.
    assert "<pubDate>%s</pubDate>" % email.utils.format_datetime(FROZEN) in xml
    # The item block for id 3 has no <link>; grab just that <item>...</item>.
    guid3 = "tag:starikov.co,2026:signal/3"
    block = xml[xml.index("<item>", xml.index(guid3) - 400):]
    block = block[: block.index("</item>") + len("</item>")]
    assert "<link>" not in block


def test_render_rss_is_well_formed_xml(cfg, frozen_clock):
    items = [
        _curated_item(),
        {
            "id": 2,
            "title": "GPU <chips> & \"more\"",
            "curated": False,
            "score": 7.5,
            "excerpt": "An excerpt with < & >",
            "link": "https://ex.com/b?a=1&b=2",
            "source_url": "https://ex.com/b?a=1&b=2",
            "last_seen": "2026-07-03T09:00:00+00:00",
        },
    ]
    html = {1: "<p>rich & <b>body</b> with ]]> inside</p>"}
    xml = feed.render_rss(items, html, cfg, SELF_URL)
    # Parses cleanly (bytes to dodge the encoding-declaration restriction).
    root = ET.fromstring(xml.encode("utf-8"))
    assert root.tag == "rss"
    channel = root.find("channel")
    assert channel is not None
    assert len(channel.findall("item")) == 2


# --------------------------------------------------------------------------- #
# _surfaces_for
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_surfaces_for_orders_points_desc_nulls_last(conn, seed):
    s1 = seed.source(slug="hn", name="Hacker News", homepage="https://news.ycombinator.com")
    s2 = seed.source(slug="lob", name="Lobsters", homepage="https://lobste.rs")
    s3 = seed.source(slug="rd", name="Reddit", homepage="https://reddit.com")
    cid = seed.cluster(canonical_url="https://ex.com/s")
    seed.surface(cid, s1, url="A", points=50)
    seed.surface(cid, s2, url="B", points=100)
    seed.surface(cid, s3, url="C", points=None)

    rows = feed._surfaces_for(conn, cid)
    assert [r["url"] for r in rows] == ["B", "A", "C"]  # 100, 50, NULL last
    # Row exposes the joined source metadata.
    top = dict(rows[0])
    assert set(top.keys()) == {"url", "points", "comments", "name", "slug", "homepage"}
    assert top["slug"] == "lob"
    assert top["name"] == "Lobsters"


# --------------------------------------------------------------------------- #
# query_items — helpers
# --------------------------------------------------------------------------- #
def _seed_curated(seed, cid_url, *, source_id, relevance=8, channels='["ai"]',
                  curated_at=None, score=None, with_article=True,
                  read_url="https://read.example/free",
                  canonical="https://canon.example/x", **cluster_over):
    """Seed a fully curated cluster surfaced by ``source_id``; return cluster id."""
    over: Dict[str, Any] = dict(canonical_url=canonical)
    if score is not None:
        over["score"] = score
    over.update(cluster_over)
    cid = seed.cluster(**over)
    seed.surface(cid, source_id, url="https://surface.example/%d" % cid)
    if with_article:
        seed.article(cid, source_url=canonical, read_url=read_url)
    cur: Dict[str, Any] = dict(relevance_score=relevance, channels=channels)
    if curated_at is not None:
        cur["curated_at"] = curated_at
    seed.curation(cid, **cur)
    return cid


# --------------------------------------------------------------------------- #
# query_items — channel guard + clamp
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_query_items_unknown_channel_returns_empty(conn, cfg, seed):
    src = seed.source(slug="hn")
    _seed_curated(seed, "c1", source_id=src)
    # 'bogus' is not in cfg.channels -> guard returns [] (LIKE-injection defense).
    assert feed.query_items(conn, cfg, {"channel": "bogus"}) == []
    # Even wildcard-y raw values are refused, not interpolated.
    assert feed.query_items(conn, cfg, {"channel": "%"}) == []
    assert feed.query_items(conn, cfg, {"topic": "_"}) == []


@pytest.mark.integration
def test_query_items_everything_disables_channel_filter(conn, cfg, seed):
    src = seed.source(slug="hn")
    _seed_curated(seed, "c1", source_id=src, channels='["security"]')
    # 'everything' -> channel None -> no LIKE filter, item returned despite
    # its channel tag being 'security'.
    out = feed.query_items(conn, cfg, {"channel": "everything"})
    assert len(out) == 1
    assert out[0]["curated"] is True


@pytest.mark.integration
def test_query_items_limit_clamped_to_feed_max(conn, cfg, seed):
    cfg.data["server"]["feed_max_limit"] = 2
    src = seed.source(slug="hn")
    for i in range(3):
        _seed_curated(
            seed, "c%d" % i, source_id=src,
            canonical="https://canon.example/%d" % i,
            curated_at="2026-07-04T0%d:00:00+00:00" % i,
        )
    out = feed.query_items(conn, cfg, {"limit": 100})
    assert len(out) == 2  # clamped by feed_max_limit


@pytest.mark.integration
def test_query_items_default_limit_used_when_absent(conn, cfg, seed):
    cfg.data["server"]["feed_default_limit"] = 1
    cfg.data["server"]["feed_max_limit"] = 200
    src = seed.source(slug="hn")
    for i in range(3):
        _seed_curated(
            seed, "c%d" % i, source_id=src,
            canonical="https://canon.example/%d" % i,
            curated_at="2026-07-04T0%d:00:00+00:00" % i,
        )
    out = feed.query_items(conn, cfg, {})
    assert len(out) == 1  # feed_default_limit


# --------------------------------------------------------------------------- #
# query_items — curated shape + link precedence
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_query_items_curated_shape(conn, cfg, seed):
    src = seed.source(slug="hn", name="Hacker News", homepage="https://news.ycombinator.com")
    cid = _seed_curated(
        seed, "c1", source_id=src,
        canonical="https://canon.example/x",
        read_url="https://read.example/free",
    )
    out = feed.query_items(conn, cfg, {})
    assert len(out) == 1
    d = out[0]
    assert d["id"] == cid
    assert d["curated"] is True
    # notes/channels parsed from the stored JSON.
    assert d["notes_list"] == ["point one", "point two"]
    assert d["channel_list"] == ["ai"]
    # link precedence: read_url wins over canonical_url.
    assert d["link"] == "https://read.example/free"
    # surfaces expanded to plain dicts with joined source metadata.
    assert isinstance(d["surfaces"], list) and d["surfaces"]
    assert set(d["surfaces"][0].keys()) == {
        "url", "points", "comments", "name", "slug", "homepage",
    }
    assert d["surfaces"][0]["slug"] == "hn"
    # archive_url is never selected/rendered.
    assert "archive_url" not in d


@pytest.mark.integration
def test_query_items_curated_link_falls_back_to_canonical(conn, cfg, seed):
    src = seed.source(slug="hn")
    _seed_curated(
        seed, "c1", source_id=src,
        canonical="https://canon.example/only",
        with_article=False,   # no article row -> read_url is NULL
    )
    out = feed.query_items(conn, cfg, {})
    assert len(out) == 1
    assert out[0]["link"] == "https://canon.example/only"


# --------------------------------------------------------------------------- #
# query_items — relevance / status / skip filters
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_query_items_relevance_threshold(conn, cfg, seed):
    src = seed.source(slug="hn")
    _seed_curated(seed, "low", source_id=src, canonical="https://c/low", relevance=3)
    _seed_curated(seed, "high", source_id=src, canonical="https://c/high", relevance=8)
    # Default threshold = funnel.min_relevance_for_feed (6): only the 8 shows.
    out = feed.query_items(conn, cfg, {})
    assert [d["relevance_score"] for d in out] == [8]
    # Override lets the low one through.
    out2 = feed.query_items(conn, cfg, {"min_relevance": 2})
    assert sorted(d["relevance_score"] for d in out2) == [3, 8]


@pytest.mark.integration
def test_query_items_excludes_skipped_and_non_done(conn, cfg, seed):
    src = seed.source(slug="hn")
    good = seed.cluster(canonical_url="https://c/good")
    seed.surface(good, src)
    seed.curation(good, relevance_score=8, status="done", skip=0)

    skipped = seed.cluster(canonical_url="https://c/skip")
    seed.surface(skipped, src)
    seed.curation(skipped, relevance_score=9, status="done", skip=1)

    pending = seed.cluster(canonical_url="https://c/pend")
    seed.surface(pending, src)
    seed.curation(pending, relevance_score=9, status="pending", skip=0)

    out = feed.query_items(conn, cfg, {})
    assert [d["id"] for d in out] == [good]


# --------------------------------------------------------------------------- #
# query_items — channel LIKE, since, min_score
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_query_items_channel_like_filter(conn, cfg, seed):
    src = seed.source(slug="hn")
    ai = _seed_curated(seed, "ai", source_id=src, canonical="https://c/ai",
                       channels='["ai"]')
    _seed_curated(seed, "sec", source_id=src, canonical="https://c/sec",
                  channels='["security"]')
    out = feed.query_items(conn, cfg, {"channel": "ai"})
    assert [d["id"] for d in out] == [ai]


@pytest.mark.integration
def test_query_items_since_filter(conn, cfg, seed):
    src = seed.source(slug="hn")
    new = _seed_curated(seed, "new", source_id=src, canonical="https://c/new",
                        curated_at="2026-07-04T10:00:00+00:00")
    _seed_curated(seed, "old", source_id=src, canonical="https://c/old",
                  curated_at="2026-07-01T00:00:00+00:00")
    out = feed.query_items(conn, cfg, {"since": "2026-07-03"})
    assert [d["id"] for d in out] == [new]


@pytest.mark.integration
def test_query_items_min_score_filter_curated(conn, cfg, seed):
    src = seed.source(slug="hn")
    hi = _seed_curated(seed, "hi", source_id=src, canonical="https://c/hi", score=8.0)
    _seed_curated(seed, "lo", source_id=src, canonical="https://c/lo", score=2.0)
    out = feed.query_items(conn, cfg, {"min_score": 5})
    assert [d["id"] for d in out] == [hi]


# --------------------------------------------------------------------------- #
# query_items — source filter clause (EXISTS)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_query_items_source_filter(conn, cfg, seed):
    hn = seed.source(slug="hn", name="HN")
    lob = seed.source(slug="lob", name="Lobsters")
    c_hn = _seed_curated(seed, "hn", source_id=hn, canonical="https://c/hn",
                         curated_at="2026-07-04T10:00:00+00:00")
    c_lob = _seed_curated(seed, "lob", source_id=lob, canonical="https://c/lob",
                          curated_at="2026-07-04T09:00:00+00:00")

    only_hn = feed.query_items(conn, cfg, {"sources": "hn"})
    assert [d["id"] for d in only_hn] == [c_hn]

    both = feed.query_items(conn, cfg, {"sources": "hn,lob"})
    assert sorted(d["id"] for d in both) == sorted([c_hn, c_lob])

    none = feed.query_items(conn, cfg, {"sources": "nonexistent"})
    assert none == []


# --------------------------------------------------------------------------- #
# query_items — scored-uncurated fallback
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_query_items_uncurated_fallback_shape(conn, cfg, seed):
    src = seed.source(slug="hn", name="HN")
    cid = seed.cluster(
        canonical_url="https://c/rust",
        title="Rust programming language release",
        score=7.0,
    )
    seed.surface(cid, src, url="https://surface/rust")
    # No curations at all -> fallback path.
    out = feed.query_items(conn, cfg, {})
    assert len(out) == 1
    d = out[0]
    assert d["curated"] is False
    assert d["notes_list"] == []
    assert d["channel_list"] == ["devtools"]      # topic-derived, sorted
    assert d["link"] == "https://c/rust"          # canonical wins
    assert d["source_url"] == "https://c/rust"
    assert d["paywalled"] == 0
    assert d["read_kind"] is None
    assert d["score"] == 7.0


@pytest.mark.integration
def test_query_items_uncurated_link_from_surface_when_no_canonical(conn, cfg, seed):
    src = seed.source(slug="hn")
    cid = seed.cluster(
        canonical_url=None, story_id="sid-nocanon",
        title="Rust programming language release", score=6.0,
    )
    seed.surface(cid, src, url="https://surface/only")
    out = feed.query_items(conn, cfg, {})
    assert len(out) == 1
    assert out[0]["link"] == "https://surface/only"


@pytest.mark.integration
def test_query_items_uncurated_min_score_defaults_to_five(conn, cfg, seed):
    src = seed.source(slug="hn")
    # score below 5.0 default, and a NULL score, are excluded; >=5 included.
    seed.cluster(canonical_url="https://c/low", title="Low score story", score=4.0)
    seed.cluster(canonical_url="https://c/null", title="No score story", score=None)
    keep = seed.cluster(canonical_url="https://c/keep", title="Kept story", score=6.0)
    out = feed.query_items(conn, cfg, {})
    assert [d["id"] for d in out] == [keep]
    # Lowering min_score pulls the 4.0 in (NULL still excluded).
    out2 = feed.query_items(conn, cfg, {"min_score": 3})
    assert keep in [d["id"] for d in out2]
    assert len(out2) == 2


@pytest.mark.integration
def test_query_items_uncurated_channel_filter_drops_nonmatching(conn, cfg, seed):
    src = seed.source(slug="hn")
    ai = seed.cluster(canonical_url="https://c/ai",
                      title="OpenAI releases new GPT model", score=8.0)
    seed.surface(ai, src, url="https://surface/ai")
    dev = seed.cluster(canonical_url="https://c/dev",
                       title="Rust programming language release", score=9.0)
    seed.surface(dev, src, url="https://surface/dev")
    # channel 'ai' keeps only the title whose topics include 'ai'.
    out = feed.query_items(conn, cfg, {"channel": "ai"})
    assert [d["id"] for d in out] == [ai]


@pytest.mark.integration
def test_query_items_uncurated_since_filter_on_last_seen(conn, cfg, seed):
    src = seed.source(slug="hn")
    new = seed.cluster(canonical_url="https://c/new", title="Fresh story", score=8.0,
                       last_seen="2026-07-04T10:00:00+00:00")
    seed.surface(new, src, url="https://surface/new")
    old = seed.cluster(canonical_url="https://c/old", title="Stale story", score=9.0,
                       last_seen="2026-07-01T00:00:00+00:00")
    seed.surface(old, src, url="https://surface/old")
    # Fallback path filters on c.last_seen >= since.
    out = feed.query_items(conn, cfg, {"since": "2026-07-03"})
    assert [d["id"] for d in out] == [new]


@pytest.mark.integration
def test_query_items_uncurated_ordered_by_score_desc(conn, cfg, seed):
    src = seed.source(slug="hn")
    c_lo = seed.cluster(canonical_url="https://c/lo", title="Story lo", score=6.0)
    seed.surface(c_lo, src, url="https://surface/lo")
    c_hi = seed.cluster(canonical_url="https://c/hi", title="Story hi", score=9.0)
    seed.surface(c_hi, src, url="https://surface/hi")
    out = feed.query_items(conn, cfg, {})
    assert [d["id"] for d in out] == [c_hi, c_lo]


@pytest.mark.integration
def test_query_items_empty_db_returns_empty(conn, cfg):
    assert feed.query_items(conn, cfg, {}) == []


@pytest.mark.integration
def test_query_items_curated_preempts_fallback(conn, cfg, seed):
    # When a curated match exists, the scored-uncurated fallback never runs.
    src = seed.source(slug="hn")
    cid = _seed_curated(seed, "c1", source_id=src, canonical="https://c/1", score=9.0)
    # A separately scored-but-uncurated cluster must NOT leak in.
    other = seed.cluster(canonical_url="https://c/2", title="Uncurated story", score=9.9)
    seed.surface(other, src, url="https://surface/2")
    out = feed.query_items(conn, cfg, {})
    assert [d["id"] for d in out] == [cid]
    assert out[0]["curated"] is True


# --------------------------------------------------------------------------- #
# module constants
# --------------------------------------------------------------------------- #
def test_module_constants():
    assert feed.GUID_FMT % 42 == "tag:starikov.co,2026:signal/42"
    assert feed._REL_RE.match("12h").groups() == ("12", "h")
    assert feed._REL_RE.match("12x") is None
