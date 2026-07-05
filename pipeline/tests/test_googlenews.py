"""Tests for ``signalpipe.ingest.googlenews`` — the Google News RSS fetcher.

Google News entry links are ``news.google.com/rss/articles/<id>`` redirect
wrappers. The module resolves the top-N per feed in two stages:

  1. offline — legacy ids base64-embed the target URL in a protobuf blob
     (``_decode_embedded_url``); free, never hits the network.
  2. network — ``client.resolve()`` follows redirects, but the 2026-era ids
     return a 200 splash instead of a 302, so after
     ``RESOLVE_GIVE_UP_AFTER`` consecutive misses the feed stops resolving.

All network is faked. Unit tests drive ``fetch_items`` with the shared
``FakePoliteClient`` (records ``.requested`` / ``.resolved`` and honours an
injected ``resolver``). The give-up counter is per-feed loop state, so the
tests craft entry order deliberately and assert the exact resolve-call order.

Legacy-id fixtures are built by base64-encoding a byte blob that embeds a
URL; ``_URL_IN_BLOB_RE`` (``https?://[\\x21-\\x7e]+``) matches from ``http``
up to the first non-printable byte, so a trailing ``\\x9a`` terminates the URL.
"""

from __future__ import annotations

import base64
import os
from typing import List, Optional

import pytest

from signalpipe.ingest import googlenews
from signalpipe.ingest.googlenews import (
    GOOGLE_HOSTS,
    RESOLVE_GIVE_UP_AFTER,
    _decode_embedded_url,
    _is_google,
)

FEED_URL = "https://news.google.com/rss/topic/technology"
# A URL the resolver hands back for a "miss": it lives on a google host, so
# ``_is_google`` is True and the entry stays unresolved.
MISS_URL = "https://news.google.com/rss/articles/AU_yqSPLASH200"


# --------------------------------------------------------------------------- #
# Local helpers — build Google News style links + feeds
# --------------------------------------------------------------------------- #
def _legacy_link(url: str) -> str:
    """A legacy article id whose blob base64-embeds ``url`` (offline-decodable).

    The trailing ``\\x9a`` byte (0x9a > 0x7e) terminates the regex match, so
    the decoded URL is exactly ``url`` with none of the protobuf tail.
    """
    blob = b'\x08\x13"B' + url.encode("ascii") + b"\x9a\x01\x0eCBMi"
    seg = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
    return "https://news.google.com/rss/articles/" + seg


def _newfmt_link(tag: str) -> str:
    """A new-format id: decodes to bytes with NO embedded http URL, so
    ``_decode_embedded_url`` returns None and the network path is taken."""
    blob = b"\x08\x13\x9a\x01" + tag.encode("ascii") + b"\x9a\x02"
    seg = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
    return "https://news.google.com/rss/articles/" + seg


def _item(
    title: str,
    link: Optional[str],
    guid: Optional[str] = None,
    pub: Optional[str] = "Sat, 04 Jul 2026 10:00:00 GMT",
    source: Optional[str] = None,
    source_url: str = "https://pub.example.com",
) -> str:
    parts = ["<item>", "<title>%s</title>" % title]
    if link is not None:
        parts.append("<link>%s</link>" % link)
    if guid is not None:
        parts.append('<guid isPermaLink="false">%s</guid>' % guid)
    if pub is not None:
        parts.append("<pubDate>%s</pubDate>" % pub)
    if source is not None:
        parts.append('<source url="%s">%s</source>' % (source_url, source))
    parts.append("</item>")
    return "".join(parts)


def _feed(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Google News</title>'
        + "".join(items)
        + "</channel></rss>"
    ).encode("utf-8")


def _run(fake_client, make_result, feed_bytes, *, resolver=None, resolve_top=25, slug="gn-topic"):
    client = fake_client(responses={FEED_URL: make_result(content=feed_bytes)}, resolver=resolver)
    items = googlenews.fetch_items(client, {"url": FEED_URL, "slug": slug}, resolve_top=resolve_top)
    return client, items


# --------------------------------------------------------------------------- #
# _decode_embedded_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/story-42",
        "http://plain.org/a",
        "https://sub.domain.co.uk/path?q=1&x=2",
        "https://www.reuters.com/technology/chip-deal",
    ],
)
def test_decode_embedded_url_legacy_id_round_trips(url):
    # The embedded URL is recovered exactly; the protobuf tail is not included.
    assert _decode_embedded_url(_legacy_link(url)) == url


def test_decode_embedded_url_ignores_query_url_case():
    # sanity: encode/decode is not identity on the seg, it decodes the blob.
    link = _legacy_link("https://host.tld/x")
    assert link.startswith("https://news.google.com/rss/articles/")
    assert _decode_embedded_url(link) == "https://host.tld/x"


@pytest.mark.parametrize(
    "link,why",
    [
        # path lacks /articles/
        ("https://news.google.com/rss/topics/CAAqBwgKM-XYZ", "non-articles"),
        # last seg 'A' pads to 'A===' -> binascii.Error (1 char cannot be 1+4k)
        ("https://news.google.com/rss/articles/A", "bad-base64"),
        # blob embeds a google-host URL -> rejected
        (_legacy_link("https://news.google.com/foo/bar"), "google-host"),
        # blob decodes but has no http url
        (_newfmt_link("NOURL"), "no-url"),
        # blob embeds a URL with an empty netloc -> host '' -> rejected
        (_legacy_link("http:///foo"), "no-host"),
    ],
)
def test_decode_embedded_url_rejects(link, why):
    assert _decode_embedded_url(link) is None


def test_decode_embedded_url_all_google_hosts_rejected():
    for host in GOOGLE_HOSTS:
        assert _decode_embedded_url(_legacy_link("https://%s/x" % host)) is None


# --------------------------------------------------------------------------- #
# _is_google
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://news.google.com/rss/articles/x", True),
        ("https://google.com", True),
        ("https://www.google.com/", True),
        ("https://sub.google.com/path", True),
        # documents the suffix quirk: endswith('google.com') matches this too.
        ("https://notgoogle.com/x", True),
        ("https://www.example.com/a", False),
        ("https://reuters.com", False),
        ("not a url", False),
        ("https://", False),
        ("", False),
        (None, False),
    ],
)
def test_is_google(url, expected):
    assert _is_google(url) is expected


# --------------------------------------------------------------------------- #
# fetch_items — title publisher-suffix cleanup
# --------------------------------------------------------------------------- #
def test_title_publisher_suffix_stripped(fake_client, make_result):
    feed = _feed(_item("Big AI Model Released - Reuters", _newfmt_link("t1"), source="Reuters"))
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert items[0]["title"] == "Big AI Model Released"
    assert items[0]["author"] == "Reuters"
    assert items[0]["extra"]["publisher"] == "Reuters"


def test_title_kept_when_source_mismatches_suffix(fake_client, make_result):
    # title ends with ' - Reuters' but the <source> is BBC -> no strip.
    feed = _feed(_item("Headline - Reuters", _newfmt_link("t2"), source="BBC"))
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert items[0]["title"] == "Headline - Reuters"
    assert items[0]["author"] == "BBC"
    assert items[0]["extra"]["publisher"] == "BBC"


def test_title_kept_when_no_publisher(fake_client, make_result):
    # no <source> element -> publisher '' -> nothing stripped, author None.
    feed = _feed(_item("Headline - Reuters", _newfmt_link("t3")))
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert items[0]["title"] == "Headline - Reuters"
    assert items[0]["author"] is None
    assert items[0]["extra"]["publisher"] is None


def test_title_strip_rstrips_residual_space(fake_client, make_result):
    # 'Story  - Reuters' -> strip ' - Reuters' leaves 'Story ' -> rstrip -> 'Story'.
    feed = _feed(_item("Story  - Reuters", _newfmt_link("t4"), source="Reuters"))
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert items[0]["title"] == "Story"


# --------------------------------------------------------------------------- #
# fetch_items — offline decode short-circuits the network
# --------------------------------------------------------------------------- #
def test_offline_decodable_entries_never_resolve(fake_client, make_result):
    def boom(url):
        raise AssertionError("resolve() must not be called for legacy ids: %r" % url)

    links = [_legacy_link("https://ex%d.com/a" % i) for i in range(3)]
    feed = _feed(*[_item("Story %d" % i, links[i], guid="G%d" % i) for i in range(3)])
    client, items = _run(fake_client, make_result, feed, resolver=boom, resolve_top=25)

    assert client.resolved == []  # network seam never touched
    assert len(items) == 3  # every entry survives; none dropped
    for i, it in enumerate(items):
        assert it["raw_url"] == "https://ex%d.com/a" % i
        assert it["extra"]["gnews_url"] == links[i]  # original google link retained


def test_full_item_shape_for_legacy_entry(fake_client, make_result):
    link = _legacy_link("https://www.reuters.com/tech/chip-42")
    feed = _feed(
        _item(
            "Chip News - Reuters",
            link,
            guid="CBMi-XYZ",
            pub="Sat, 04 Jul 2026 10:00:00 GMT",
            source="Reuters",
        )
    )
    _, items = _run(fake_client, make_result, feed, resolve_top=25)
    assert items == [
        {
            "guid": "gnews-CBMi-XYZ",
            "raw_url": "https://www.reuters.com/tech/chip-42",
            "title": "Chip News",
            "author": "Reuters",
            "published_at": "2026-07-04T10:00:00+00:00",
            "points": None,
            "comments": None,
            "extra": {
                "surface": "google-news",
                "publisher": "Reuters",
                "gnews_url": link,
            },
        }
    ]


# --------------------------------------------------------------------------- #
# fetch_items — network resolve + give-up after N consecutive misses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("miss_value", [None, MISS_URL])
def test_network_resolve_gives_up_after_five_misses(fake_client, make_result, capsys, miss_value):
    # 7 undecodable entries, every resolve is a miss (None or a google URL).
    links = [_newfmt_link("miss%d" % i) for i in range(7)]
    feed = _feed(*[_item("Story %d" % i, links[i], guid="G%d" % i) for i in range(7)])
    client, items = _run(
        fake_client, make_result, feed, resolver=lambda u: miss_value, slug="gn-topic"
    )

    # resolve attempted for entries 0..4 only, in document order; then give-up.
    assert client.resolved == links[:RESOLVE_GIVE_UP_AFTER]
    assert len(client.resolved) == RESOLVE_GIVE_UP_AFTER

    err = capsys.readouterr().err
    assert err.count("gnews:") == 1  # printed exactly once
    assert "gn-topic" in err  # message carries the slug
    assert "%d consecutive" % RESOLVE_GIVE_UP_AFTER in err

    # give-up stops resolving but every entry (incl. 5,6 past the cutoff) still
    # yields an item; each keeps its google link and carries no gnews_url.
    assert len(items) == 7
    for i, it in enumerate(items):
        assert it["raw_url"] == links[i]
        assert "gnews_url" not in it["extra"]


def test_miss_counter_resets_on_a_real_resolve(fake_client, make_result, capsys):
    # 9 undecodable entries; the one at idx 4 resolves to a real (non-google)
    # URL, resetting the counter so give-up never triggers and all 9 resolve.
    links = [_newfmt_link("x%d" % i) for i in range(9)]
    good_link = links[4]
    good_url = "https://real.example.com/story"
    feed = _feed(*[_item("Story %d" % i, links[i], guid="G%d" % i) for i in range(9)])

    def resolver(url):
        return good_url if url == good_link else MISS_URL

    client, items = _run(fake_client, make_result, feed, resolver=resolver)

    assert client.resolved == links  # all nine attempted, in order
    assert "gnews:" not in capsys.readouterr().err  # no premature give-up

    # the successful entry carries the resolved URL + gnews_url; others keep google.
    assert items[4]["raw_url"] == good_url
    assert items[4]["extra"]["gnews_url"] == good_link
    for i in range(9):
        if i != 4:
            assert items[i]["raw_url"] == links[i]
            assert "gnews_url" not in items[i]["extra"]


def test_resolve_top_boundary_stops_network(fake_client, make_result, capsys):
    # resolve_top=2 -> only idx 0,1 are eligible; misses never reach give-up.
    links = [_newfmt_link("b%d" % i) for i in range(5)]
    feed = _feed(*[_item("Story %d" % i, links[i], guid="G%d" % i) for i in range(5)])
    client, items = _run(fake_client, make_result, feed, resolver=lambda u: MISS_URL, resolve_top=2)

    assert client.resolved == links[:2]  # entries 2..4 never resolved
    assert "gnews:" not in capsys.readouterr().err
    assert len(items) == 5  # eligibility cap gates resolve, not item emission
    for i, it in enumerate(items):
        assert it["raw_url"] == links[i]


def test_single_successful_resolve(fake_client, make_result):
    link = _newfmt_link("solo")
    feed = _feed(_item("Only Story", link, guid="G0"))
    client, items = _run(
        fake_client, make_result, feed, resolver=lambda u: "https://dest.example.org/a"
    )
    assert client.resolved == [link]
    assert items[0]["raw_url"] == "https://dest.example.org/a"
    assert items[0]["extra"]["gnews_url"] == link


# --------------------------------------------------------------------------- #
# fetch_items — HTTP result handling (mirrors rss)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kw",
    [
        dict(status=304),
        dict(status=304, unchanged=True),
        dict(status=200, unchanged=True, content=b"<rss/>"),
    ],
)
def test_not_modified_returns_empty(fake_client, make_result, kw):
    client = fake_client(responses={FEED_URL: make_result(**kw)})
    assert googlenews.fetch_items(client, {"url": FEED_URL, "slug": "gn"}) == []
    assert client.resolved == []


@pytest.mark.parametrize(
    "kw,expected_msg",
    [
        (dict(status=500, content=b"x"), "HTTP 500"),
        (dict(status=503, content=b"", error="upstream down"), "upstream down"),
        (dict(status=200, content=b""), "HTTP 200"),  # 200 but empty body
        (dict(status=200, content=b"", error="empty"), "empty"),
    ],
)
def test_error_results_raise(fake_client, make_result, kw, expected_msg):
    client = fake_client(responses={FEED_URL: make_result(**kw)})
    with pytest.raises(RuntimeError) as ei:
        googlenews.fetch_items(client, {"url": FEED_URL, "slug": "gn"})
    assert str(ei.value) == expected_msg


# --------------------------------------------------------------------------- #
# fetch_items — parsing edge cases
# --------------------------------------------------------------------------- #
def test_skips_entries_without_link_or_title(fake_client, make_result):
    feed = _feed(
        _item("Valid A", _newfmt_link("a"), guid="GA"),
        _item("No Link B", None, guid=None),  # no <link> and no <guid> -> link None
        _item("   ", _newfmt_link("d"), guid="GD"),  # whitespace title -> skip
        _item("Valid E", _newfmt_link("e"), guid="GE"),
    )
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert [it["guid"] for it in items] == ["gnews-GA", "gnews-GE"]


def test_guid_falls_back_to_link_when_id_missing(fake_client, make_result):
    link = _newfmt_link("noguid")
    feed = _feed(_item("No Guid Story", link))  # no <guid> element
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert items[0]["guid"] == "gnews-%s" % link


def test_entries_capped_at_100(fake_client, make_result):
    feed = _feed(
        *[_item("Story %d" % i, _newfmt_link("cap%d" % i), guid="G%d" % i) for i in range(105)]
    )
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert len(items) == 100
    assert items[0]["guid"] == "gnews-G0"
    assert items[-1]["guid"] == "gnews-G99"


def test_published_at_none_when_no_date(fake_client, make_result):
    feed = _feed(_item("Dateless", _newfmt_link("nd"), guid="G0", pub=None))
    _, items = _run(fake_client, make_result, feed, resolve_top=0)
    assert items[0]["published_at"] is None


def test_no_entries_yields_empty_list(fake_client, make_result):
    _, items = _run(fake_client, make_result, _feed(), resolve_top=0)
    assert items == []


# --------------------------------------------------------------------------- #
# Integration — real feedparser through a real PoliteClient (httpx MockTransport)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_fetch_items_end_to_end_real_feedparser(polite_client_factory, load_bytes):
    pytest.importorskip("feedparser")
    import httpx

    feed = load_bytes("googlenews_topic.rss")
    requested: List[str] = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=feed, headers={"Content-Type": "application/rss+xml"})

    client = polite_client_factory(handler)
    source_row = {"url": "https://news.google.com/rss/topic/top", "slug": "top-stories"}
    items = googlenews.fetch_items(client, source_row)

    # Only the feed URL was fetched — every link decodes offline, so resolve()
    # (which would issue a HEAD) is never called.
    assert requested == ["https://news.google.com/rss/topic/top"]

    assert [it["guid"] for it in items] == [
        "gnews-CBMi-CHIP",
        "gnews-CBMi-MKT",
        "gnews-CBMi-SPRT",
    ]

    first = items[0]
    assert first["raw_url"] == "https://www.reuters.com/technology/chip-deal"
    assert first["title"] == "Global Chip Deal"  # ' - Reuters' stripped
    assert first["author"] == "Reuters"
    assert first["published_at"] == "2026-07-04T10:00:00+00:00"
    assert first["points"] is None and first["comments"] is None
    assert first["extra"]["surface"] == "google-news"
    assert first["extra"]["publisher"] == "Reuters"
    assert first["extra"]["gnews_url"].startswith("https://news.google.com/rss/articles/")

    # middle entry: no <source> -> publisher/author None, still decoded offline.
    assert items[1]["author"] is None
    assert items[1]["extra"]["publisher"] is None
    assert items[1]["raw_url"] == "https://www.bloomberg.com/news/markets-rally"

    assert items[2]["raw_url"] == "https://www.espn.com/story/123"
    assert items[2]["published_at"] == "2026-07-02T08:00:00+00:00"


# --------------------------------------------------------------------------- #
# Live smoke — real news.google.com (deselected by default; env-gated)
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_google_news_smoke(cfg, conn):
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live: set SIGNAL_LIVE=1 to hit real news.google.com")
    from signalpipe.ingest.fetch_http import PoliteClient

    url = "https://news.google.com/rss/search?q=technology&hl=en-US&gl=US&ceid=US:en"
    with PoliteClient(cfg, conn) as client:
        items = googlenews.fetch_items(client, {"url": url, "slug": "gn-live"}, resolve_top=3)

    assert isinstance(items, list)
    assert all("guid" in it and it["extra"]["surface"] == "google-news" for it in items)
