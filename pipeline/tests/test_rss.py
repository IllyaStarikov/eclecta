"""Unit tests for ``signalpipe.ingest.rss`` — the generic RSS/Atom fetcher.

This is the workhorse for ~95% of sources: ``PoliteClient.fetch`` (injected, faked
here) plus a lazy ``feedparser.parse`` into normalized raw-item dicts. Everything is
hermetic — the only I/O boundary is ``client.fetch``, replaced with a
``FakePoliteClient`` (or a raw ``make_result``-fed fake), and ``feedparser`` runs
purely in-process on inline XML byte constants.

Covered behavior (derived by READING the source + probing feedparser 6.0.12):

* ``_entry_time`` — ``published_parsed`` preferred, ``updated_parsed`` fallback,
  ``None`` when neither present, ``None`` on ValueError/OverflowError (out-of-range).
* ``fetch_feed_items`` — RSS 2.0 + Atom happy paths (field-by-field), guid = ``id``
  else ``link`` fallback, ``author`` -> None when absent, entry filtering (drop
  missing link/title, strip whitespace title), 100-entry cap, 304/unchanged -> [],
  non-200/empty -> RuntimeError, and bozo (malformed-but-parseable) -> ``extra.bozo``.
"""

from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from signalpipe.ingest.rss import _entry_time, fetch_feed_items

FEED_URL = "https://example.com/feed.xml"


def _src(url: str = FEED_URL) -> Dict[str, Any]:
    """A minimal source_row — fetch_feed_items only reads ['url']."""
    return {"url": url, "slug": "example", "mode": None}


# --------------------------------------------------------------------------- #
# Inline feed fixtures (byte constants — hermetic, no fixture files needed)
# --------------------------------------------------------------------------- #
RSS_2_0 = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <link>https://example.com</link>
    <description>An example feed</description>
    <item>
      <title>First Post</title>
      <link>https://example.com/first</link>
      <guid>tag:example.com,2026:first</guid>
      <author>jane@example.com (Jane Doe)</author>
      <pubDate>Wed, 10 Jun 2026 08:30:00 GMT</pubDate>
    </item>
    <item>
      <title>Second Post</title>
      <link>https://example.com/second</link>
      <pubDate>Thu, 11 Jun 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <link href="https://example.com/"/>
  <id>urn:uuid:feed</id>
  <updated>2026-06-11T10:00:00Z</updated>
  <entry>
    <title>Atom Post</title>
    <link href="https://example.com/atom-post"/>
    <id>urn:uuid:entry-1</id>
    <author><name>Atom Author</name></author>
    <published>2026-06-11T09:15:00Z</published>
    <updated>2026-06-11T10:00:00Z</updated>
  </entry>
  <entry>
    <title>Updated Only</title>
    <link href="https://example.com/updated-only"/>
    <id>urn:uuid:entry-2</id>
    <updated>2026-06-12T08:00:00Z</updated>
  </entry>
</feed>
"""

# Filtering feed:
#  * item 1: link present, NO title              -> dropped (title falsy)
#  * item 2: title present, NO link / NO guid     -> dropped (link falsy)
#  * item 3: whitespace-only title, link present  -> dropped (strip() -> "")
#  * item 4: good; guid isPermaLink="false" so id != link (exercises guid=id branch)
FILTER_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>F</title><link>https://e.com</link><description>d</description>
<item><link>https://e.com/no-title</link><guid>g1</guid></item>
<item><title>No Link Here</title></item>
<item><title>   </title><link>https://e.com/ws</link></item>
<item><title>Good One</title><link>https://e.com/good</link><guid isPermaLink="false">gid-4</guid></item>
</channel></rss>
"""

# Malformed-but-parseable: the raw '&' in the channel title trips the strict SAX
# parser (bozo=1) but feedparser's loose fallback still yields the entry.
BOZO_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Bad & Feed</title><link>https://e.com</link><description>d</description>
<item><title>Tom & Jerry</title><link>https://e.com/tj</link><guid>gg</guid>
<pubDate>Wed, 10 Jun 2026 08:30:00 GMT</pubDate></item>
</channel></rss>
"""


def _big_feed(n: int) -> bytes:
    items = "".join(
        "<item><title>Post %d</title><link>https://e.com/p%d</link>"
        "<guid isPermaLink='false'>g%d</guid></item>" % (i, i, i)
        for i in range(n)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel><title>F</title>'
        "<link>https://e.com</link><description>d</description>"
        "%s</channel></rss>" % items
    ).encode("utf-8")


# --------------------------------------------------------------------------- #
# _entry_time — pure function, no client
# --------------------------------------------------------------------------- #
def _st(year: int, mon: int, day: int, hour: int, minute: int, sec: int) -> time.struct_time:
    return time.struct_time((year, mon, day, hour, minute, sec, 0, 0, 0))


def test_entry_time_published_parsed_present():
    entry = {"published_parsed": _st(2026, 6, 10, 8, 30, 0)}
    assert _entry_time(entry) == "2026-06-10T08:30:00+00:00"


def test_entry_time_prefers_published_over_updated():
    entry = {
        "published_parsed": _st(2026, 6, 10, 8, 30, 0),
        "updated_parsed": _st(2020, 1, 1, 0, 0, 0),
    }
    assert _entry_time(entry) == "2026-06-10T08:30:00+00:00"


def test_entry_time_falls_back_to_updated_parsed():
    entry = {"updated_parsed": _st(2026, 6, 12, 8, 0, 0)}
    assert _entry_time(entry) == "2026-06-12T08:00:00+00:00"


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"published_parsed": None},
        {"published_parsed": None, "updated_parsed": None},
        {"title": "no dates here"},
    ],
)
def test_entry_time_none_when_no_dates(entry):
    assert _entry_time(entry) is None


@pytest.mark.parametrize("year", [10000, 999999])
def test_entry_time_out_of_range_returns_none(year):
    # calendar.timegm succeeds but datetime.fromtimestamp raises
    # ValueError (year > 9999) / OverflowError (huge) -> caught -> None.
    entry = {"published_parsed": _st(year, 1, 1, 0, 0, 0)}
    assert _entry_time(entry) is None


def test_entry_time_utc_normalization_across_midnight():
    # 23:30 UTC stays 23:30 UTC (timegm treats struct_time as UTC, no local shift).
    entry = {"published_parsed": _st(2026, 12, 31, 23, 30, 0)}
    assert _entry_time(entry) == "2026-12-31T23:30:00+00:00"


# --------------------------------------------------------------------------- #
# fetch_feed_items — happy paths
# --------------------------------------------------------------------------- #
def test_fetch_feed_items_rss_happy_path(fake_client, make_result):
    client = fake_client(default=make_result(content=RSS_2_0, status=200))
    items = fetch_feed_items(client, _src())

    assert client.requested == [FEED_URL]
    assert len(items) == 2

    first, second = items
    assert first == {
        "guid": "tag:example.com,2026:first",
        "raw_url": "https://example.com/first",
        "title": "First Post",
        "author": "jane@example.com (Jane Doe)",
        "published_at": "2026-06-10T08:30:00+00:00",
        "points": None,
        "comments": None,
        "extra": {"bozo": False},
    }
    # Second item has no <guid> -> guid falls back to the link; no <author> -> None.
    assert second["guid"] == "https://example.com/second"
    assert second["raw_url"] == "https://example.com/second"
    assert second["title"] == "Second Post"
    assert second["author"] is None
    assert second["published_at"] == "2026-06-11T09:00:00+00:00"
    assert second["extra"] == {"bozo": False}


def test_fetch_feed_items_atom_happy_path(fake_client, make_result):
    client = fake_client(default=make_result(content=ATOM, status=200))
    items = fetch_feed_items(client, _src())

    assert len(items) == 2
    first, second = items

    assert first["guid"] == "urn:uuid:entry-1"
    assert first["raw_url"] == "https://example.com/atom-post"
    assert first["title"] == "Atom Post"
    assert first["author"] == "Atom Author"
    assert first["published_at"] == "2026-06-11T09:15:00+00:00"
    assert first["extra"] == {"bozo": False}
    assert first["points"] is None and first["comments"] is None

    # Second Atom entry has only <updated> -> _entry_time uses updated fallback.
    assert second["guid"] == "urn:uuid:entry-2"
    assert second["raw_url"] == "https://example.com/updated-only"
    assert second["title"] == "Updated Only"
    assert second["author"] is None
    assert second["published_at"] == "2026-06-12T08:00:00+00:00"
    assert second["points"] is None and second["comments"] is None
    assert second["extra"] == {"bozo": False}


def test_fetch_feed_items_reads_only_url_key(fake_client, make_result):
    # source_row is dict-like; only ['url'] is consulted and it drives the fetch.
    other = "https://other.example.org/atom"
    client = fake_client(responses={other: make_result(content=ATOM, status=200)})
    items = fetch_feed_items(client, {"url": other})
    assert client.requested == [other]
    # The body fetched from ['url'] is what actually gets parsed (not merely counted).
    assert len(items) == 2
    assert items[0]["raw_url"] == "https://example.com/atom-post"
    assert items[0]["guid"] == "urn:uuid:entry-1"


# --------------------------------------------------------------------------- #
# fetch_feed_items — entry filtering + cap
# --------------------------------------------------------------------------- #
def test_fetch_feed_items_filters_and_strips(fake_client, make_result):
    client = fake_client(default=make_result(content=FILTER_FEED, status=200))
    items = fetch_feed_items(client, _src())

    # Only the one fully-formed entry survives.
    assert len(items) == 1
    only = items[0]
    assert only["title"] == "Good One"
    assert only["raw_url"] == "https://e.com/good"
    # guid comes from the (non-permalink) <guid> id, NOT the link.
    assert only["guid"] == "gid-4"
    assert only["published_at"] is None


def test_fetch_feed_items_caps_at_100(fake_client, make_result):
    client = fake_client(default=make_result(content=_big_feed(120), status=200))
    items = fetch_feed_items(client, _src())
    assert len(items) == 100
    # Cap is a head slice — first 100 entries in document order.
    assert items[0]["title"] == "Post 0"
    assert items[-1]["title"] == "Post 99"


def test_fetch_feed_items_under_cap_returns_all(fake_client, make_result):
    client = fake_client(default=make_result(content=_big_feed(5), status=200))
    items = fetch_feed_items(client, _src())
    assert len(items) == 5
    # All five survive and stay in document order (no cap, no reordering).
    assert [it["title"] for it in items] == ["Post %d" % i for i in range(5)]
    assert items[0]["guid"] == "g0" and items[-1]["guid"] == "g4"


# --------------------------------------------------------------------------- #
# fetch_feed_items — short-circuit / error control flow
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,unchanged",
    [
        (304, False),  # 304 alone
        (200, True),  # body-hash short-circuit
        (304, True),  # both
    ],
)
def test_fetch_feed_items_unchanged_returns_empty(fake_client, make_result, status, unchanged):
    client = fake_client(default=make_result(content=RSS_2_0, status=status, unchanged=unchanged))
    assert fetch_feed_items(client, _src()) == []


@pytest.mark.parametrize(
    "kwargs,expected_msg",
    [
        (dict(status=500, content=None, error="HTTP 500"), "HTTP 500"),
        (dict(status=0, content=None, error="ConnectError: boom"), "ConnectError: boom"),
        (dict(status=200, content=None, error=None), "HTTP 200"),  # empty body, no error
        (dict(status=200, content=b"", error=None), "HTTP 200"),  # empty bytes
        (dict(status=503, content=None, error=None), "HTTP 503"),  # non-200, no error text
    ],
)
def test_fetch_feed_items_error_raises_runtimeerror(fake_client, make_result, kwargs, expected_msg):
    client = fake_client(default=make_result(**kwargs))
    with pytest.raises(RuntimeError) as exc:
        fetch_feed_items(client, _src())
    assert str(exc.value) == expected_msg


# --------------------------------------------------------------------------- #
# fetch_feed_items — bozo (malformed but parseable)
# --------------------------------------------------------------------------- #
def test_fetch_feed_items_bozo_feed_still_yields_entries(fake_client, make_result):
    client = fake_client(default=make_result(content=BOZO_FEED, status=200))
    items = fetch_feed_items(client, _src())

    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Tom & Jerry"
    assert it["raw_url"] == "https://e.com/tj"
    assert it["extra"] == {"bozo": True}


def test_fetch_feed_items_well_formed_feed_reports_bozo_false(fake_client, make_result):
    client = fake_client(default=make_result(content=RSS_2_0, status=200))
    items = fetch_feed_items(client, _src())
    assert all(it["extra"]["bozo"] is False for it in items)


def test_fetch_feed_items_empty_but_valid_feed_returns_empty(fake_client, make_result):
    empty = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<rss version="2.0"><channel><title>Empty</title>'
        b"<link>https://e.com</link><description>d</description></channel></rss>"
    )
    client = fake_client(default=make_result(content=empty, status=200))
    assert fetch_feed_items(client, _src()) == []
