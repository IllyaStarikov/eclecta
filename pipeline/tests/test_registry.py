"""Tests for signalpipe.ingest.registry.

Covers the pure parsers (slugify, markdown/OPML/feed parsing, feed-link
discovery), the registry file round-trip (load/save/write_opml), the probe
logic (probe_url + probe_candidates), the DB seed/stats paths, and the CLI
handlers (probe_cmd/import_cmd/expand).

Hermetic: no real network. probe_candidates/probe_cmd/expand construct their own
PoliteClient internally, so those seams are monkeypatched at ``registry.PoliteClient``
(and probe_url is patched to a pure function where the real HTTP path is irrelevant).
DB/FS writes are routed to tmp paths OUTSIDE 'Mobile Documents' (the db safe-path
guard rejects the iCloud repo tree), and ``_now_iso`` is frozen for stable output.
"""

from __future__ import annotations

import json
import os

import pytest

import signalpipe.ingest.registry as reg
from signalpipe import db as db_mod
from signalpipe.ingest import gdelt as gdelt_mod
from signalpipe.ingest.fetch_http import FetchResult
from signalpipe.models import ProbeResult, SourceSpec


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
def _polite_factory(result=None, mapping=None, sink=None):
    """Build a fake ``PoliteClient`` class (context-manager) for monkeypatching
    ``registry.PoliteClient``. ``fetch`` returns ``mapping[url]`` if present else
    ``result``. Instances append themselves to ``sink`` if given."""

    class _FP:
        def __init__(self, cfg=None, conn=None):
            self.host_intervals = {}
            self.requested = []
            if sink is not None:
                sink.append(self)

        def fetch(self, url, conditional=False):
            self.requested.append(url)
            if mapping is not None and url in mapping:
                return mapping[url]
            return result

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

    return _FP


def _use_tmp_registry(cfg, tmp_path):
    """Repoint cfg.sources_json / cfg.sources_opml at tmp (absolute paths bypass
    repo_path's REPO_ROOT join). Keeps every registry-file write off the real repo."""
    cfg.data["sources"] = {
        "registry": str(tmp_path / "sources.json"),
        "opml": str(tmp_path / "sources.opml"),
    }
    return cfg


def _write_registry(cfg, sources):
    path = cfg.sources_json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sources": sources}))


def _source_dict(**over):
    base = dict(
        slug="ex",
        name="Example",
        type="rss",
        url="https://example.com/feed.xml",
        homepage="https://example.com",
        category="ai",
        topics=["ai"],
        reputation=1.0,
        tier=2,
        cadence_min=60,
        paywalled=False,
        enabled=True,
    )
    base.update(over)
    return base


def _db_row(cfg, slug):
    conn = db_mod.connect_ro(cfg.db_path)
    try:
        return conn.execute(
            "SELECT * FROM sources WHERE slug=?", (slug,)
        ).fetchone()
    finally:
        conn.close()


HOMEPAGE_HTML = (
    b"<html><head>"
    b'<link rel="stylesheet" href="/style.css">'
    b'<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
    b"</head><body>hi</body></html>"
)
HOMEPAGE_NO_FEED = (
    b"<html><head>"
    b'<link rel="alternate" type="text/html" href="/other">'
    b"</head><body>nothing</body></html>"
)


# --------------------------------------------------------------------------- #
# slugify
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Hacker News", "hacker-news"),
        ("Hacker News!!!", "hacker-news"),
        ("  --Hello--World--  ", "hello-world"),
        ("a.b.c", "a-b-c"),
        ("***", "source"),
        ("", "source"),
        (None, "source"),
        ("9 lives", "9-lives"),
    ],
)
def test_slugify_table(name, expected):
    assert reg.slugify(name) == expected


def test_slugify_truncates_to_64():
    out = reg.slugify("x" * 200)
    assert len(out) == 64
    assert out == "x" * 64


def test_slugify_property():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st
    import re as _re

    @given(st.text(max_size=120))
    @settings(max_examples=200)
    def check(text):
        out = reg.slugify(text)
        assert len(out) <= 64
        # "source" fallback, else lowercase alnum + single internal dashes,
        # never a leading dash. (A trailing dash CAN appear via [:64] truncation.)
        assert out == "source" or (
            _re.match(r"^[a-z0-9-]+$", out)
            and not out.startswith("-")
            and "--" not in out
        )

    check()


# --------------------------------------------------------------------------- #
# parse_markdown_list
# --------------------------------------------------------------------------- #
def test_parse_markdown_list_bullets_tables_dedup_order():
    text = (
        "Intro prose with a [Prose](https://prose.example) ignored.\n"
        "- [Alpha](https://alpha.example) bullet\n"
        "* [Beta](https://beta.example) star bullet\n"
        "| [Gamma](https://gamma.example) | cell |\n"
        "- [Alpha again](https://alpha.example) duplicate url\n"
        "> [Quote](https://quote.example) blockquote ignored\n"
        "- plain bullet no link\n"
    )
    out = reg.parse_markdown_list(text)
    assert out == [
        ("Alpha", "https://alpha.example"),
        ("Beta", "https://beta.example"),
        ("Gamma", "https://gamma.example"),
    ]


def test_parse_markdown_list_empty():
    assert reg.parse_markdown_list("no lists here\njust prose") == []


# --------------------------------------------------------------------------- #
# parse_opml / _parse_opml_regex
# --------------------------------------------------------------------------- #
def test_parse_opml_wellformed_nested_skips_missing_xmlurl():
    wf = (
        b'<?xml version="1.0"?><opml version="2.0"><head><title>t</title></head>'
        b"<body>"
        b'<outline text="Cat A">'
        b'<outline type="rss" text="Feed One" title="Feed One Title" '
        b'xmlUrl="https://a.example/feed.xml" htmlUrl="https://a.example/"/>'
        b'<outline text="nogroup" xmlUrl="https://b.example/rss"/>'
        b"</outline>"
        b'<outline type="rss" text="No xmlUrl outline"/>'
        b"</body></opml>"
    )
    assert reg.parse_opml(wf) == [
        ("Feed One Title", "https://a.example/feed.xml", "https://a.example/"),
        ("nogroup", "https://b.example/rss", None),
    ]


def test_parse_opml_bare_ampersand_escape_retry():
    amp = (
        b'<?xml version="1.0"?><opml version="2.0"><body>'
        b'<outline type="rss" text="Tom & Jerry" title="Tom & Jerry" '
        b'xmlUrl="https://c.example/feed" htmlUrl="https://c.example/"/>'
        b"</body></opml>"
    )
    assert reg.parse_opml(amp) == [
        ("Tom & Jerry", "https://c.example/feed", "https://c.example/")
    ]


def test_parse_opml_regex_fallback_on_raw_html_in_attr():
    # A raw '<' inside an attribute value fails both XML parse attempts (the
    # escape-retry only fixes bare '&'), so the tag-level regex fallback runs.
    bad = (
        b'<?xml version="1.0"?><opml><body>'
        b'<outline type="rss" xmlUrl="https://d.example/feed" '
        b'title="A &amp; B < C" htmlUrl="https://d.example/"/>'
        b"</body></opml>"
    )
    # &amp; is unescaped by the regex path; raw '<' survives verbatim.
    assert reg.parse_opml(bad) == [
        ("A & B < C", "https://d.example/feed", "https://d.example/")
    ]


def test_parse_opml_title_text_fallback_ordering():
    order = (
        b'<?xml version="1.0"?><opml><body>'
        b'<outline text="OnlyText" xmlUrl="https://e.example/f"/>'
        b'<outline xmlUrl="https://f.example/f"/>'
        b"</body></opml>"
    )
    # title missing -> text; both missing -> xmlUrl.
    assert reg.parse_opml(order) == [
        ("OnlyText", "https://e.example/f", None),
        ("https://f.example/f", "https://f.example/f", None),
    ]


def test_parse_opml_skips_non_outline_children():
    # A non-<outline> child element is walked past without emitting a row.
    content = (
        b'<?xml version="1.0"?><opml><body>'
        b"<note>ignore me</note>"
        b'<outline xmlUrl="https://k.example/f" text="K"/>'
        b"</body></opml>"
    )
    assert reg.parse_opml(content) == [("K", "https://k.example/f", None)]


def test_parse_opml_rootless_walk():
    # No <body>: walk from root.
    nobody = (
        b'<?xml version="1.0"?><opml>'
        b'<outline xmlUrl="https://g.example/f" text="G"/></opml>'
    )
    assert reg.parse_opml(nobody) == [("G", "https://g.example/f", None)]


def test_parse_opml_regex_direct_skips_missing_xmlurl_and_unescapes():
    content = (
        b'<outline text="x" htmlUrl="https://z.example/"/>'  # no xmlUrl -> skipped
        b'<outline type="rss" xmlUrl="" title="Empty"/>'     # empty xmlUrl -> skipped
        b'<outline title="R &amp; D" xmlUrl="https://h.example/feed"/>'
    )
    assert reg._parse_opml_regex(content) == [
        ("R & D", "https://h.example/feed", None)
    ]


# --------------------------------------------------------------------------- #
# _looks_like_feed
# --------------------------------------------------------------------------- #
def test_looks_like_feed_rss(load_bytes):
    ok, kind, title, n, latest = reg._looks_like_feed(load_bytes("arxiv_cs_ai.rss"))
    assert ok is True
    assert kind == "rss"
    assert title == "cs.AI updates on arXiv.org"
    assert n == 4
    assert latest == "2026-07-03T22:00:00+00:00"


def test_looks_like_feed_atom(load_bytes):
    ok, kind, title, n, latest = reg._looks_like_feed(load_bytes("reddit_top.rss"))
    assert ok is True
    assert kind == "atom"
    assert title == "Top posts from r/python"
    assert n == 3
    assert latest == "2023-11-14T22:13:20+00:00"


@pytest.mark.parametrize("content", [b"", b"<html><body>not a feed</body></html>"])
def test_looks_like_feed_garbage(content):
    assert reg._looks_like_feed(content) == (False, None, None, 0, None)


def test_looks_like_feed_entries_without_dates_latest_none():
    rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<title>NoDates</title>"
        b"<item><title>x</title><link>http://x.example/1</link></item>"
        b"</channel></rss>"
    )
    ok, kind, title, n, latest = reg._looks_like_feed(rss)
    assert (ok, kind, title, n, latest) == (True, "rss", "NoDates", 1, None)


# --------------------------------------------------------------------------- #
# _discover_feed_links
# --------------------------------------------------------------------------- #
def test_discover_feed_links_returns_only_feed_resolved_absolute():
    assert reg._discover_feed_links(HOMEPAGE_HTML, "https://site.example/") == [
        "https://site.example/feed.xml"
    ]


def test_discover_feed_links_atom_type():
    html = (
        b'<link rel="alternate" type="application/atom+xml" href="atom.xml">'
    )
    assert reg._discover_feed_links(html, "https://site.example/blog/") == [
        "https://site.example/blog/atom.xml"
    ]


def test_discover_feed_links_none_when_no_feed_link():
    assert reg._discover_feed_links(HOMEPAGE_NO_FEED, "https://x.example/") == []


def test_discover_feed_links_skips_feed_link_without_href():
    # rel=alternate + matching type but no href attribute -> nothing yielded.
    html = b'<link rel="alternate" type="application/rss+xml">'
    assert reg._discover_feed_links(html, "https://x.example/") == []


# --------------------------------------------------------------------------- #
# load_specs / save_specs / write_opml
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_load_specs_missing_file_returns_empty(cfg, tmp_path):
    _use_tmp_registry(cfg, tmp_path)
    assert reg.load_specs(cfg) == []


@pytest.mark.integration
def test_load_specs_applies_defaults_and_slug_fallback(cfg, tmp_path):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(
        cfg,
        [
            # minimal row: no slug -> slugify(name); coercions applied
            {"name": "My Feed", "url": "https://f.example/rss",
             "reputation": "1.5", "tier": "1", "cadence_min": "30",
             "paywalled": 1, "enabled": 0, "topics": ["ai", "science"]},
        ],
    )
    specs = reg.load_specs(cfg)
    assert len(specs) == 1
    s = specs[0]
    assert s.slug == "my-feed"
    assert s.type == "rss"
    assert s.category == "uncategorized"
    assert s.reputation == 1.5 and isinstance(s.reputation, float)
    assert s.tier == 1 and isinstance(s.tier, int)
    assert s.cadence_min == 30
    assert s.paywalled is True
    assert s.enabled is False
    assert s.topics == ["ai", "science"]


@pytest.mark.integration
def test_save_specs_round_trip_and_sorted(cfg, tmp_path, freeze_now_iso):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    specs = [
        SourceSpec(slug="zeta", name="Zeta", type="rss",
                   url="https://z.example/feed", category="news", tier=2,
                   topics=["news"]),
        SourceSpec(slug="alpha", name="Alpha", type="atom",
                   url="https://a.example/feed", category="ai", tier=1,
                   topics=["ai"]),
        SourceSpec(slug="beta", name="Beta", type="json",
                   url="https://b.example/api", category="ai", tier=2,
                   topics=["ai"]),
    ]
    reg.save_specs(cfg, specs)

    reloaded = reg.load_specs(cfg)
    # Sorted on disk by (category, tier, slug): ai/1/alpha, ai/2/beta, news/2/zeta
    assert [s.slug for s in reloaded] == ["alpha", "beta", "zeta"]
    by_slug = {s.slug: s for s in reloaded}
    assert by_slug["alpha"].type == "atom"
    assert by_slug["beta"].type == "json"
    assert by_slug["zeta"].category == "news"

    # A second save from the reloaded specs is byte-stable.
    first = cfg.sources_json.read_text()
    reg.save_specs(cfg, reloaded)
    assert cfg.sources_json.read_text() == first


@pytest.mark.integration
def test_write_opml_only_enabled_rss_atom_grouped_and_escaped(cfg, tmp_path, freeze_now_iso):
    frozen = freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    specs = [
        SourceSpec(slug="a", name='Alice & "Bob"', type="rss",
                   url="https://a.example/feed", homepage="https://a.example",
                   category="ai", tier=2, topics=["ai"]),
        SourceSpec(slug="b", name="Bee", type="atom",
                   url="https://b.example/feed", category="ai", tier=1,
                   topics=["ai"]),
        SourceSpec(slug="c", name="Jsony", type="json",
                   url="https://c.example/api", category="ai", topics=["ai"]),
        SourceSpec(slug="d", name="Disabled", type="rss",
                   url="https://d.example/feed", category="ai", enabled=False,
                   topics=["ai"]),
    ]
    reg.write_opml(cfg, specs)
    opml = cfg.sources_opml.read_text()

    assert ("<dateModified>%s</dateModified>" % frozen) in opml
    # json + disabled excluded
    assert "https://c.example/api" not in opml
    assert "https://d.example/feed" not in opml
    # enabled rss/atom included
    assert "https://a.example/feed" in opml
    assert "https://b.example/feed" in opml
    # quoteattr escaped the ampersand in the name
    assert "&amp;" in opml
    assert "Alice &amp;" in opml
    # single category group present
    assert "<outline text=\"ai\">" in opml


# --------------------------------------------------------------------------- #
# _existing_keys
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_existing_keys_collects_feed_urls_and_domains(cfg, tmp_path):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(
        cfg,
        [
            _source_dict(slug="one", url="https://www.one.example/feed",
                         homepage="https://one.example"),
            _source_dict(slug="two", url="https://sub.two.example/rss",
                         homepage=None),
        ],
    )
    feed_keys, domains = reg._existing_keys(cfg)
    assert "https://one.example/feed" in feed_keys       # www stripped, https forced
    assert "https://sub.two.example/rss" in feed_keys
    assert "one.example" in domains
    assert "two.example" in domains                      # homepage None -> url domain


@pytest.mark.integration
def test_existing_keys_skips_source_without_url(cfg, tmp_path):
    _use_tmp_registry(cfg, tmp_path)
    # A row with neither url nor homepage contributes no keys (url "" -> None
    # canonical; empty homepage -> skipped).
    _write_registry(cfg, [{"name": "NoUrl", "slug": "nourl"}])
    feed_keys, domains = reg._existing_keys(cfg)
    assert feed_keys == set()
    assert domains == set()


# --------------------------------------------------------------------------- #
# merge_into_registry
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_merge_adds_new_and_returns_count(cfg, tmp_path, freeze_now_iso):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [])
    verified = [
        {"name": "New One", "slug": "new-one", "type": "json",
         "url": "https://new.example/feed", "homepage": "https://new.example",
         "category": "ai", "topics": ["ai"], "tier": 2},
    ]
    added = reg.merge_into_registry(cfg, verified)
    assert added == 1
    specs = {s.slug: s for s in reg.load_specs(cfg)}
    assert "new-one" in specs
    # non-rss/atom type coerced to rss
    assert specs["new-one"].type == "rss"


@pytest.mark.integration
def test_merge_dedup_by_canonical_url(cfg, tmp_path):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(
        cfg, [_source_dict(slug="exist", url="https://dup.example/feed")]
    )
    before = cfg.sources_json.read_text()
    verified = [
        {"name": "Dup", "slug": "dup-new", "type": "rss",
         # canonicalizes to the same key (www + trailing slash normalized away)
         "url": "https://www.dup.example/feed/", "topics": ["ai"], "tier": 2},
    ]
    added = reg.merge_into_registry(cfg, verified)
    assert added == 0
    # save_specs is only called when added>0 -> file untouched
    assert cfg.sources_json.read_text() == before


@pytest.mark.integration
def test_merge_slug_collision_suffixed(cfg, tmp_path):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [_source_dict(slug="foo", url="https://old.example/feed")])
    verified = [
        {"name": "Foo Two", "slug": "foo", "type": "rss",
         "url": "https://new.example/feed", "topics": ["ai"], "tier": 3},
    ]
    added = reg.merge_into_registry(cfg, verified)
    assert added == 1
    slugs = {s.slug for s in reg.load_specs(cfg)}
    assert "foo" in slugs and "foo-2" in slugs


@pytest.mark.integration
def test_merge_drops_invalid_candidate(cfg, tmp_path):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [])
    verified = [
        {"name": "Bad Topic", "slug": "bad", "type": "rss",
         "url": "https://bad.example/feed", "topics": ["not-a-channel"], "tier": 2},
    ]
    added = reg.merge_into_registry(cfg, verified)
    assert added == 0
    assert reg.load_specs(cfg) == []


# --------------------------------------------------------------------------- #
# probe_url
# --------------------------------------------------------------------------- #
def test_probe_url_direct_feed(fake_client, make_result, load_bytes):
    url = "https://feed.example/rss"
    client = fake_client(
        responses={url: make_result(content=load_bytes("arxiv_cs_ai.rss"),
                                    status=200, final_url=url)}
    )
    r = reg.probe_url(client, url, ["/feed/"])
    assert r.ok is True
    assert r.feed_url == url
    assert r.kind == "rss"
    assert r.entries == 4
    assert client.requested == [url]


def test_probe_url_homepage_rel_alternate(fake_client, make_result, load_bytes):
    home = "https://site.example/"
    feed = "https://site.example/feed.xml"
    client = fake_client(
        responses={
            home: make_result(content=HOMEPAGE_HTML, status=200, final_url=home),
            feed: make_result(content=load_bytes("arxiv_cs_ai.rss"), status=200,
                             final_url=feed),
        }
    )
    r = reg.probe_url(client, home, ["/rss/"])
    assert r.ok is True
    assert r.feed_url == feed
    assert client.requested == [home, feed]


def test_probe_url_common_path_probing(fake_client, make_result, load_bytes):
    home = "https://site2.example/"
    cand = "https://site2.example/feed.xml"
    client = fake_client(
        responses={
            home: make_result(content=HOMEPAGE_NO_FEED, status=200, final_url=home),
            cand: make_result(content=load_bytes("reddit_top.rss"), status=200,
                             final_url=cand),
        }
    )
    r = reg.probe_url(client, home, ["/feed.xml"])
    assert r.ok is True
    assert r.feed_url == cand
    assert r.kind == "atom"
    # homepage first (no rel=alternate), then the common-path guess
    assert client.requested == [home, cand]


def test_probe_url_discovered_links_invalid_then_common_path(fake_client, make_result, load_bytes):
    # Two rel=alternate links: one 404s (skipped), one is 200-but-not-a-feed
    # (skipped); then common-path probing tries a 200-garbage path before the
    # valid feed path. Exercises every "keep looking" branch of probe_url.
    home = "https://disc.example/"
    html = (
        b'<link rel="alternate" type="application/rss+xml" href="/a.xml">'
        b'<link rel="alternate" type="application/rss+xml" href="/b.xml">'
    )
    client = fake_client(
        responses={
            home: make_result(content=html, status=200, final_url=home),
            "https://disc.example/a.xml": make_result(content=b"x", status=404),
            "https://disc.example/b.xml": make_result(content=b"not a feed", status=200),
            "https://disc.example/p1": make_result(content=b"still not", status=200),
            "https://disc.example/p2": make_result(
                content=load_bytes("arxiv_cs_ai.rss"), status=200,
                final_url="https://disc.example/p2"),
        }
    )
    r = reg.probe_url(client, home, ["/p1", "/p2"])
    assert r.ok is True
    assert r.feed_url == "https://disc.example/p2"
    assert client.requested == [
        home, "https://disc.example/a.xml", "https://disc.example/b.xml",
        "https://disc.example/p1", "https://disc.example/p2",
    ]


def test_probe_url_no_valid_feed(fake_client, make_result):
    home = "https://site3.example/"
    cand = "https://site3.example/rss"
    client = fake_client(
        responses={
            home: make_result(content=HOMEPAGE_NO_FEED, status=200, final_url=home),
            cand: make_result(content=b"still not a feed", status=404),
        }
    )
    r = reg.probe_url(client, home, ["/rss"])
    assert r.ok is False
    assert r.error == "no valid feed found"


def test_probe_url_status_zero_passes_error(fake_client, make_result):
    url = "https://down.example/feed"
    client = fake_client(
        responses={url: make_result(status=0, error="connection refused")}
    )
    r = reg.probe_url(client, url, ["/feed/"])
    assert r.ok is False
    assert r.error == "connection refused"


def test_probe_url_none_content(fake_client, make_result):
    url = "https://empty.example/feed"
    client = fake_client(responses={url: make_result(content=None, status=200)})
    r = reg.probe_url(client, url, ["/feed/"])
    assert r.ok is False
    assert r.error == "fetch failed"


# --------------------------------------------------------------------------- #
# probe_candidates
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_probe_candidates_partitions_and_dedups(cfg, tmp_path, monkeypatch):
    _use_tmp_registry(cfg, tmp_path)
    # Pre-registered: feed url + homepage domain that later candidates collide with.
    _write_registry(
        cfg,
        [_source_dict(slug="dupsrc", url="https://dup.example/feed",
                      homepage="https://dup.example")],
    )

    def fake_probe(client, url, probe_paths):
        table = {
            "https://good.example/feed": ProbeResult(
                candidate_url=url, ok=True, feed_url="https://good.example/feed",
                kind="rss", entries=5, title="Good"),
            "https://dup.example/feed": ProbeResult(
                candidate_url=url, ok=True, feed_url="https://dup.example/feed",
                kind="rss", entries=3),
            "https://dead.example/feed": ProbeResult(
                candidate_url=url, ok=True,
                feed_url="https://openai.com/blog/rss.xml", kind="rss", entries=3),
            "https://dup.example/section": ProbeResult(
                candidate_url=url, ok=True,
                feed_url="https://dup.example/newfeed.xml", kind="rss", entries=3),
            "https://fallback.example/badfeed": ProbeResult(
                candidate_url=url, ok=False, error="404"),
            "https://fallback.example": ProbeResult(
                candidate_url=url, ok=True, feed_url="https://fallback.example/real",
                kind="atom", entries=2, title="FB"),
        }
        return table.get(url, ProbeResult(candidate_url=url, error="unknown"))

    monkeypatch.setattr(reg, "probe_url", fake_probe)
    monkeypatch.setattr(reg, "PoliteClient", _polite_factory())

    candidates = [
        {"name": "Good", "homepage": "https://good.example",
         "feed_url": "https://good.example/feed"},
        {"name": "Dup", "homepage": "https://dup.example",
         "feed_url": "https://dup.example/feed"},
        {"name": "Dead", "homepage": "https://dead.example",
         "feed_url": "https://dead.example/feed"},
        {"name": "DomDup", "homepage": "https://dup.example/section"},
        {"name": "FB", "homepage": "https://fallback.example",
         "feed_url": "https://fallback.example/badfeed"},
        {},  # no url
    ]
    verified, rejected = reg.probe_candidates(cfg, candidates, max_workers=2)

    assert {v["name"] for v in verified} == {"Good", "FB"}
    good = next(v for v in verified if v["name"] == "Good")
    assert good["url"] == "https://good.example/feed"
    assert good["slug"] == "good"
    assert good["tier"] == 3 and good["reputation"] == 0.8

    reasons = {r["name"]: r["reason"] for r in rejected}
    assert reasons["Dup"] == "duplicate/dead"
    assert reasons["Dead"] == "duplicate/dead"
    assert reasons["DomDup"].startswith("domain already registered")
    # the no-url candidate rejects with an empty name
    assert any(r["reason"] == "no url" for r in rejected)


# --------------------------------------------------------------------------- #
# seed
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_seed_empty_registry_returns_1(cfg, tmp_path, capsys):
    _use_tmp_registry(cfg, tmp_path)  # sources.json does not exist
    assert reg.seed(cfg) == 1
    assert "nothing to seed" in capsys.readouterr().out


@pytest.mark.integration
def test_seed_invalid_specs_returns_1(cfg, tmp_path, capsys):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [_source_dict(slug="bad", tier=5)])  # tier not in 1..3
    assert reg.seed(cfg) == 1
    assert "invalid specs" in capsys.readouterr().err


@pytest.mark.integration
def test_seed_insert_then_update(cfg, tmp_path, capsys, freeze_now_iso):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(
        cfg,
        [
            _source_dict(slug="s-a", url="https://a.example/feed"),
            _source_dict(slug="s-b", url="https://b.example/feed"),
        ],
    )
    assert reg.seed(cfg) == 0
    out = capsys.readouterr().out
    assert "seeded: 2 new, 0 updated, 2 total" in out

    # Re-seed: same slugs -> updates, no inserts.
    assert reg.seed(cfg) == 0
    out = capsys.readouterr().out
    assert "seeded: 0 new, 2 updated, 2 total" in out
    assert _db_row(cfg, "s-a")["added_at"] is not None


@pytest.mark.integration
def test_seed_preserves_auto_disabled_but_reenables_flaky(cfg, tmp_path, freeze_now_iso):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(
        cfg,
        [
            _source_dict(slug="broken", url="https://broken.example/feed", enabled=True),
            _source_dict(slug="flaky", url="https://flaky.example/feed", enabled=True),
        ],
    )
    # Pre-create both rows disabled: broken has enough errors to stay disabled,
    # flaky is under the threshold and should be re-enabled by seed.
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO sources(slug, name, category, type, url, tier, "
            "enabled, error_count) VALUES(?,?,?,?,?,?,?,?)",
            ("broken", "Broken", "ai", "rss", "https://broken.example/feed",
             2, 0, reg.PRESERVE_DISABLED_ERRORS),
        )
        conn.execute(
            "INSERT INTO sources(slug, name, category, type, url, tier, "
            "enabled, error_count) VALUES(?,?,?,?,?,?,?,?)",
            ("flaky", "Flaky", "ai", "rss", "https://flaky.example/feed",
             2, 0, 1),
        )
    finally:
        conn.close()

    assert reg.seed(cfg) == 0
    assert _db_row(cfg, "broken")["enabled"] == 0   # preserved auto-disable
    assert _db_row(cfg, "flaky")["enabled"] == 1     # re-enabled


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_stats_db_missing_falls_back_to_registry_file(cfg, tmp_path, capsys):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [_source_dict(slug="one"), _source_dict(slug="two")])
    # cfg.db_path (tmp signal.db) does not exist yet
    assert not cfg.db_path.exists()
    assert reg.stats(cfg) == 0
    assert "registry file: 2 sources (db not created yet)" in capsys.readouterr().out


@pytest.mark.integration
def test_stats_reads_db(cfg, conn, seed, capsys):
    seed.source(slug="a1", category="ai", tier=1, enabled=1,
                verified_at="2026-07-01T00:00:00+00:00")
    seed.source(slug="a2", category="ai", tier=2, enabled=1, verified_at=None)
    seed.source(slug="n1", category="news", tier=3, enabled=0, error_count=5)
    cfg.data["operation_1k"] = {"target_verified": 500}

    assert reg.stats(cfg) == 0
    out = capsys.readouterr().out
    assert "sources: 3 total, 2 enabled, 1 verified, 1 failing(3+)" in out
    assert "ai" in out and "news" in out
    assert "operation-1k: 1 / 500 verified" in out


# --------------------------------------------------------------------------- #
# expand
# --------------------------------------------------------------------------- #
TECHMEME_OPML = (
    b'<?xml version="1.0"?><opml version="2.0"><head><title>lb</title></head>'
    b"<body>"
    b'<outline type="rss" text="TechCrunch" title="TechCrunch" '
    b'xmlUrl="https://techcrunch.example/feed" htmlUrl="https://techcrunch.example/"/>'
    b'<outline type="rss" text="The Verge" title="The Verge" '
    b'xmlUrl="https://theverge.example/rss" htmlUrl="https://theverge.example/"/>'
    # Same title as the first -> slug collision -> "-2" suffix.
    b'<outline type="rss" text="TechCrunch" title="TechCrunch" '
    b'xmlUrl="https://techcrunch.example/feed2" htmlUrl="https://techcrunch.example/2"/>'
    b"</body></opml>"
)


@pytest.mark.integration
def test_expand_adds_builtins_then_idempotent(cfg, tmp_path, capsys, freeze_now_iso, monkeypatch):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    monkeypatch.setattr(
        reg, "PoliteClient",
        _polite_factory(result=FetchResult(status=200, content=TECHMEME_OPML)),
    )

    assert reg.expand(cfg) == 0
    out = capsys.readouterr().out
    assert "expand: added" in out

    specs = {s.slug: s for s in reg.load_specs(cfg)}
    for slug in (
        "arxiv-cs-ai", "reddit-programming", "gnews-technology",
        "gnews-q-ai", "gdelt-tech", "mastodon-trends",
        "tm-techcrunch", "tm-the-verge", "tm-techcrunch-2",
    ):
        assert slug in specs, slug
    # gdelt row carries the full artlist query URL
    assert specs["gdelt-tech"].url == gdelt_mod.query_url(
        "artificial intelligence sourcelang:eng"
    )

    # Second run: everything already present -> nothing added.
    assert reg.expand(cfg) == 0
    assert "expand: nothing new" in capsys.readouterr().out


@pytest.mark.integration
def test_expand_techmeme_fetch_failure_warns_but_adds_rest(cfg, tmp_path, capsys, freeze_now_iso, monkeypatch):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    # gnews_queries as a bare list (not a dict) exercises the list->dict branch.
    cfg.data["ingest"]["gnews_queries"] = ["neural nets"]
    monkeypatch.setattr(
        reg, "PoliteClient",
        _polite_factory(result=FetchResult(status=503, content=None, error="down")),
    )
    assert reg.expand(cfg) == 0
    captured = capsys.readouterr()
    assert "could not fetch Techmeme lb.opml (down)" in captured.err
    specs = {s.slug for s in reg.load_specs(cfg)}
    assert "arxiv-cs-ai" in specs
    assert "gnews-q-neural-nets" in specs               # list->dict query branch
    assert not any(s.startswith("tm-") for s in specs)  # no techmeme rows


# --------------------------------------------------------------------------- #
# probe_cmd
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_probe_cmd_url_ok(cfg, tmp_path, capsys, monkeypatch):
    _use_tmp_registry(cfg, tmp_path)
    monkeypatch.setattr(reg, "PoliteClient", _polite_factory())
    monkeypatch.setattr(
        reg, "probe_url",
        lambda client, url, paths: ProbeResult(
            candidate_url=url, ok=True, feed_url=url, kind="rss", entries=3),
    )
    rc = reg.probe_cmd(cfg, candidates=None, url="https://x.example/feed", import_ok=False)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["feed_url"] == "https://x.example/feed"


@pytest.mark.integration
def test_probe_cmd_url_not_ok_returns_1(cfg, tmp_path, monkeypatch):
    _use_tmp_registry(cfg, tmp_path)
    monkeypatch.setattr(reg, "PoliteClient", _polite_factory())
    monkeypatch.setattr(
        reg, "probe_url",
        lambda client, url, paths: ProbeResult(
            candidate_url=url, error="no valid feed found"),
    )
    assert reg.probe_cmd(cfg, candidates=None, url="https://x.example", import_ok=False) == 1


@pytest.mark.integration
def test_probe_cmd_requires_input(cfg, tmp_path, capsys):
    _use_tmp_registry(cfg, tmp_path)
    assert reg.probe_cmd(cfg, candidates=None, url=None, import_ok=False) == 2
    assert "need --candidates file or --url" in capsys.readouterr().err


@pytest.mark.integration
def test_probe_cmd_candidates_writes_verified_json(cfg, tmp_path, capsys, monkeypatch):
    _use_tmp_registry(cfg, tmp_path)
    cand_path = tmp_path / "cands.json"
    cand_path.write_text(json.dumps([{"name": "A"}, {"name": "B"}]))

    verified = [{"name": "A", "slug": "a", "type": "rss",
                 "url": "https://a.example/feed", "topics": ["ai"], "tier": 2}]
    rejected = [{"name": "B", "url": "https://b.example", "reason": "dead"}]
    monkeypatch.setattr(reg, "probe_candidates", lambda c, cands: (verified, rejected))

    rc = reg.probe_cmd(cfg, candidates=cand_path, url=None, import_ok=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified 1 / rejected 1 of 2 candidates" in out
    assert "REJECT" in out
    out_path = tmp_path / "cands.verified.json"
    assert out_path.exists()
    assert json.loads(out_path.read_text()) == verified


@pytest.mark.integration
def test_probe_cmd_candidates_import_ok_merges_and_seeds(cfg, tmp_path, capsys, freeze_now_iso, monkeypatch):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [])
    cand_path = tmp_path / "cands.json"
    # dict form with a 'candidates' key exercises the unwrap branch
    cand_path.write_text(json.dumps({"candidates": [{"name": "A"}]}))

    verified = [{"name": "New", "slug": "new", "type": "rss",
                 "url": "https://new.example/feed", "homepage": "https://new.example",
                 "category": "ai", "topics": ["ai"], "tier": 2}]
    monkeypatch.setattr(reg, "probe_candidates", lambda c, cands: (verified, []))

    rc = reg.probe_cmd(cfg, candidates=cand_path, url=None, import_ok=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 1 into" in out
    assert "seeded: 1 new" in out              # seed(cfg) ran
    assert "new" in {s.slug for s in reg.load_specs(cfg)}


# --------------------------------------------------------------------------- #
# import_cmd
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_import_cmd_adds_then_seeds(cfg, tmp_path, capsys, freeze_now_iso):
    freeze_now_iso(reg)
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [])
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"sources": [
        {"name": "Imp", "slug": "imp", "type": "rss",
         "url": "https://imp.example/feed", "topics": ["ai"], "tier": 2},
    ]}))
    rc = reg.import_cmd(cfg, path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 1 new sources" in out
    assert "seeded: 1 new" in out
    assert "imp" in {s.slug for s in reg.load_specs(cfg)}


@pytest.mark.integration
def test_import_cmd_nothing_new_skips_seed(cfg, tmp_path, capsys):
    _use_tmp_registry(cfg, tmp_path)
    _write_registry(cfg, [_source_dict(slug="have", url="https://have.example/feed")])
    path = tmp_path / "import.json"
    # a bare list (non-dict) + a duplicate url -> 0 added
    path.write_text(json.dumps([
        {"name": "Dup", "slug": "dup", "type": "rss",
         "url": "https://have.example/feed", "topics": ["ai"], "tier": 2},
    ]))
    rc = reg.import_cmd(cfg, path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 0 new sources" in out
    assert "seeded" not in out


# --------------------------------------------------------------------------- #
# live smoke (deselected by default; env-gated)
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_probe_url_live_lobsters():
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test; set SIGNAL_LIVE=1 to run")
    from signalpipe.ingest.fetch_http import PoliteClient
    import signalpipe.config as config_mod

    cfg = config_mod.load()
    with PoliteClient(cfg) as client:
        r = reg.probe_url(client, "https://lobste.rs/rss", ["/rss"])
    assert r.ok is True
    assert r.entries > 0
