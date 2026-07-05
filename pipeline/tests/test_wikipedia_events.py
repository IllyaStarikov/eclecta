"""Unit + integration tests for ``signalpipe.ingest.wikipedia_events``.

The module fetches Wikipedia ``Portal:Current_events`` day pages through the
MediaWiki parse API and extracts leaf ``<li>`` event bullets. Everything here is
hermetic:

* ``fetch_items`` only reaches the network via the injected client's ``.fetch``;
  we replace it with a tiny in-test ``_FakeClient`` keyed on the exact
  ``api_url(day)`` (``source_row`` is NEVER dereferenced, so we pass ``{}``/``None``).
* ``today=`` is ALWAYS passed explicitly so page titles / urls / guids /
  ``published_at`` are deterministic (the default is wall-clock ``datetime.now``).
* ``_bullet_items`` is exercised directly with recorded/inline MediaWiki markup;
  expected guids are recomputed with ``hashlib.sha1`` over the exact normalized
  text (post unescape / whitespace-collapse / strip), never the raw HTML.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import string
from typing import Any, Callable, Dict, List, Optional

import pytest

from signalpipe.ingest.wikipedia_events import (
    API_URL,
    WIKIMEDIA_SUFFIXES,
    _bullet_items,
    _is_wikimedia,
    api_url,
    fetch_items,
    page_title,
)

# A fixed day used across the pure-parser tests. Single-digit month AND day so we
# also pin the "unpadded day" / zero-padded-ISO distinction.
DAY = datetime.date(2026, 6, 9)
DAY_ISO = "2026-06-09T00:00:00+00:00"
DAY_TAG = "20260609"


# --------------------------------------------------------------------------- #
# Local helpers (writers add helpers in their own file, never edit conftest)
# --------------------------------------------------------------------------- #
def _expected_guid(text: str, day: datetime.date = DAY) -> str:
    """Recompute the guid the way the module does — independently of the parser."""
    return "wiki-%s-%s" % (
        day.strftime("%Y%m%d"),
        hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
    )


def _parse_body(html: str) -> bytes:
    """A MediaWiki parse-API success envelope carrying ``html`` in ``parse.text.*``."""
    return json.dumps({"parse": {"title": "T", "pageid": 1, "text": {"*": html}}}).encode(
        "utf-8"
    )


def _err_body(code: str) -> bytes:
    return json.dumps({"error": {"code": code, "info": "nope"}}).encode("utf-8")


class _FakeClient:
    """Records ``(url, conditional)`` per fetch and returns canned results by URL.

    Distinct from the shared ``fake_client`` fixture only in that it also captures
    the ``conditional`` argument so we can assert the module calls with
    ``conditional=False``.
    """

    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
        default: Any = None,
    ):
        self._responses = dict(responses or {})
        self._default = default
        self.calls: List[Any] = []
        self.requested: List[str] = []

    def fetch(self, url: str, conditional: bool = True):
        self.calls.append((url, conditional))
        self.requested.append(url)
        if url in self._responses:
            value = self._responses[url]
            return value() if callable(value) else value
        if self._default is not None:
            return self._default() if callable(self._default) else self._default
        raise AssertionError("_FakeClient: no canned response for %r" % url)

    def resolve(self, url: str) -> str:  # pragma: no cover - unused by module
        return url

    def close(self) -> None:  # pragma: no cover - trivial
        pass


# --------------------------------------------------------------------------- #
# page_title
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "day,expected",
    [
        (datetime.date(2026, 6, 9), "Portal:Current_events/2026_June_9"),
        (datetime.date(2026, 1, 1), "Portal:Current_events/2026_January_1"),
        (datetime.date(2026, 12, 25), "Portal:Current_events/2026_December_25"),
        (datetime.date(2025, 3, 5), "Portal:Current_events/2025_March_5"),
        # Two-digit day stays unpadded (day, not %d).
        (datetime.date(2024, 11, 30), "Portal:Current_events/2024_November_30"),
    ],
)
def test_page_title_formatting(day, expected):
    assert page_title(day) == expected


def test_page_title_day_is_unpadded():
    # %d would give "09"; the module uses day.day → "9".
    assert page_title(datetime.date(2026, 6, 9)).endswith("_June_9")
    assert "_June_09" not in page_title(datetime.date(2026, 6, 9))


# --------------------------------------------------------------------------- #
# api_url
# --------------------------------------------------------------------------- #
def test_api_url_percent_encodes_title():
    url = api_url(datetime.date(2026, 6, 9))
    assert url == (
        "https://en.wikipedia.org/w/api.php?action=parse"
        "&page=Portal%3ACurrent_events%2F2026_June_9&format=json&prop=text"
    )


def test_api_url_encodes_colon_and_slash_but_keeps_underscores():
    url = api_url(datetime.date(2026, 1, 1))
    assert "%3A" in url  # the ':' after Portal
    assert "%2F" in url  # the '/' before the year
    assert "Current_events" in url  # underscores are unreserved, left as-is
    assert "Portal:" not in url  # raw colon must not survive


# --------------------------------------------------------------------------- #
# _is_wikimedia
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://en.wikipedia.org/wiki/Kharkiv",
        "https://wikipedia.org/",  # bare host == suffix
        "http://wikimedia.org",  # exact-match branch
        "https://commons.wikimedia.org/wiki/File:X",
        "https://www.wikidata.org/wiki/Q1",
        "https://en.wiktionary.org/wiki/word",
        "https://en.wikinews.org/wiki/story",
        "https://en.wikisource.org/wiki/text",
        "https://EN.WIKIPEDIA.ORG/wiki/X",  # host is lowercased
        "https://en.wikipedia.org:443/wiki/X",  # port stripped from hostname
    ],
)
def test_is_wikimedia_true(url):
    assert _is_wikimedia(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://en.wikipedia.org.evil.com/wiki/X",  # dotted-suffix guard
        "https://notwikipedia.org/",  # missing the leading dot
        "https://example.com/",
        "https://www.bbc.com/news/example",
        "https://apnews.com/article/example",
        "/wiki/relative-no-host",  # hostname None -> ""
        "not-a-url",  # hostname None -> ""
    ],
)
def test_is_wikimedia_false(url):
    assert _is_wikimedia(url) is False


# --------------------------------------------------------------------------- #
# _bullet_items — core parser
# --------------------------------------------------------------------------- #
def test_bullet_citation_first_raw_url_and_field_mapping():
    html = (
        "<ul><li>Diplomats from "
        '<a href="/wiki/Geneva" title="Geneva">Geneva</a>'
        " broker a landmark ceasefire agreement between the two nations. "
        '<a rel="nofollow" class="external text" '
        'href="https://www.bbc.com/news/example">(BBC)</a></li></ul>'
    )
    items = _bullet_items(html, DAY)
    assert len(items) == 1
    item = items[0]

    expected_text = (
        "Diplomats from Geneva broker a landmark ceasefire agreement "
        "between the two nations"
    )
    assert item == {
        "guid": _expected_guid(expected_text),
        "raw_url": "https://www.bbc.com/news/example",  # citation wins over wiki
        "title": expected_text,
        "author": None,
        "published_at": DAY_ISO,
        "points": None,
        "comments": None,
        "extra": {
            "surface": "wikipedia-current-events",
            "date": "2026-06-09",
            "wiki_url": "https://en.wikipedia.org/wiki/Geneva",
        },
    }


def test_bullet_title_strips_citation_anchor_and_markup():
    html = (
        "<li>Something notable and sufficiently long happens in the world "
        '<a rel="nofollow" class="external text" href="https://ex.com/x">(Source)</a></li>'
    )
    (item,) = _bullet_items(html, DAY)
    # The "(Source)" external anchor text is gone; no residual markup.
    assert "(Source)" not in item["title"]
    assert "<a" not in item["title"] and "href" not in item["title"]
    assert item["title"] == (
        "Something notable and sufficiently long happens in the world"
    )


def test_bullet_wiki_fallback_when_no_external_citation():
    html = (
        "<li>An international coalition of scientists announces a breakthrough in "
        '<a href="/wiki/Fusion_power" title="Fusion power">fusion power</a> research.</li>'
    )
    (item,) = _bullet_items(html, DAY)
    assert item["raw_url"] == "https://en.wikipedia.org/wiki/Fusion_power"
    assert item["extra"]["wiki_url"] == "https://en.wikipedia.org/wiki/Fusion_power"
    assert item["title"] == (
        "An international coalition of scientists announces a breakthrough "
        "in fusion power research"
    )


def test_bullet_short_wiki_only_dropped_as_topic_header():
    # No citation AND len(text) < 40 -> treated as a topic header, dropped.
    html = '<li><a href="/wiki/Disasters" title="x">Disasters and accidents</a></li>'
    assert _bullet_items(html, DAY) == []


@pytest.mark.parametrize(
    "n,kept",
    [
        (39, False),  # < 40 with no citation -> dropped
        (40, True),  # == 40 -> NOT < 40 -> kept
        (60, True),
    ],
)
def test_bullet_topic_header_length_boundary(n, kept):
    # Empty anchor contributes no visible text, so normalized text is exactly "x"*n.
    html = "<li>" + ("x" * n) + ' <a href="/wiki/Topic" title="t"></a></li>'
    items = _bullet_items(html, DAY)
    if kept:
        assert len(items) == 1
        assert items[0]["title"] == "x" * n
        assert items[0]["raw_url"] == "https://en.wikipedia.org/wiki/Topic"
    else:
        assert items == []


def test_bullet_own_text_cut_at_nested_ul_and_children_parsed_separately():
    html = (
        "<ul><li>Parent own text about a broad topic that is quite long indeed here "
        '<a href="/wiki/Parent" title="Parent">parent</a>'
        "<ul><li>Child event describing a specific happening in the world today. "
        '<a rel="nofollow" class="external text" href="https://example.com/child">(Src)</a>'
        "</li></ul></li></ul>"
    )
    items = _bullet_items(html, DAY)
    assert len(items) == 2

    parent, child = items
    # Parent uses ONLY its own text (before the nested <ul>) — no child leakage.
    assert parent["title"] == (
        "Parent own text about a broad topic that is quite long indeed here parent"
    )
    assert "Child" not in parent["title"]
    assert parent["raw_url"] == "https://en.wikipedia.org/wiki/Parent"

    # The nested child is parsed as its own bullet.
    assert child["title"] == "Child event describing a specific happening in the world today"
    assert child["raw_url"] == "https://example.com/child"


def test_bullet_first_wiki_and_first_citation_win():
    html = (
        "<li>Text "
        '<a href="/wiki/First" title="a">first</a> and '
        '<a href="/wiki/Second" title="b">second</a> more '
        '<a rel="nofollow" class="external text" href="https://ex.com/one">(One)</a> '
        '<a rel="nofollow" class="external text" href="https://ex.com/two">(Two)</a></li>'
    )
    (item,) = _bullet_items(html, DAY)
    assert item["raw_url"] == "https://ex.com/one"  # first external citation
    assert item["extra"]["wiki_url"] == "https://en.wikipedia.org/wiki/First"


def test_bullet_href_is_html_unescaped():
    html = (
        "<li>Event about something notable happening in the world today for sure "
        '<a rel="nofollow" class="external text" '
        'href="https://ex.com/path?a=1&amp;b=2">(S)</a></li>'
    )
    (item,) = _bullet_items(html, DAY)
    assert item["raw_url"] == "https://ex.com/path?a=1&b=2"


def test_bullet_wikimedia_external_link_is_not_a_citation():
    # An http(s) link to a wikimedia host must NOT count as a citation; with no
    # non-wikimedia external and only a short own-text, the bullet is dropped.
    html = (
        "<li>See also "
        '<a rel="nofollow" class="external text" '
        'href="https://en.wikipedia.org/wiki/Foo">Foo</a></li>'
    )
    assert _bullet_items(html, DAY) == []


def test_bullet_text_entities_unescaped_in_title():
    html = (
        "<li>Company X &amp; Company Y announce a merger valued in the billions today "
        '<a rel="nofollow" class="external text" href="https://ex.com/m">(S)</a></li>'
    )
    (item,) = _bullet_items(html, DAY)
    assert "&amp;" not in item["title"]
    assert "Company X & Company Y" in item["title"]
    # Pin the full normalized title: entity decoded, citation anchor gone.
    assert item["title"] == (
        "Company X & Company Y announce a merger valued in the billions today"
    )
    # guid hashes that same decoded text.
    assert item["guid"] == _expected_guid(
        "Company X & Company Y announce a merger valued in the billions today"
    )


def test_bullet_trailing_dash_and_punct_stripped():
    html = (
        "<li>An event of great significance occurred in the capital today "
        '<a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a> —</li>'
    )
    (item,) = _bullet_items(html, DAY)
    assert item["title"].endswith("today")
    assert not item["title"].endswith("—")
    # The trailing " —" is fully stripped (strip set includes space + dashes),
    # leaving no dangling separator.
    assert item["title"] == (
        "An event of great significance occurred in the capital today"
    )


def test_bullet_no_links_is_skipped():
    assert _bullet_items("<li>Just some text with no links here at all.</li>", DAY) == []


def test_bullet_empty_after_strip_is_skipped():
    # raw_url is set (citation present) but the visible text collapses to "" -> skipped.
    html = '<li> <a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a> . </li>'
    assert _bullet_items(html, DAY) == []


def test_bullet_no_li_returns_empty():
    assert _bullet_items("<p>no bullets here</p>", DAY) == []


def test_bullet_title_truncated_at_140():
    long_text = "A" * 200
    html = (
        "<li>" + long_text + " "
        '<a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a></li>'
    )
    (item,) = _bullet_items(html, DAY)
    assert len(item["title"]) == 140
    assert item["title"] == ("A" * 200)[:140]
    # guid hashes the FULL text, not the truncated title.
    assert item["guid"] == _expected_guid("A" * 200)


def test_bullet_published_at_is_day_midnight_utc():
    html = (
        "<li>A sufficiently long event to be kept without any external citation link "
        '<a href="/wiki/Topic" title="t"></a></li>'
    )
    (item,) = _bullet_items(html, datetime.date(2026, 12, 25))
    assert item["published_at"] == "2026-12-25T00:00:00+00:00"
    assert item["extra"]["date"] == "2026-12-25"
    assert item["guid"].startswith("wiki-20261225-")


def test_bullet_guid_is_stable_and_matches_sha1():
    text = "A concrete world event long enough to survive the topic-header filter"
    html = (
        "<li>" + text + " "
        '<a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a></li>'
    )
    a = _bullet_items(html, DAY)
    b = _bullet_items(html, DAY)
    assert a[0]["guid"] == b[0]["guid"] == _expected_guid(text)


def test_bullet_same_text_different_day_differs_only_in_date_prefix():
    text = "A concrete world event long enough to survive the topic-header filter"
    html = (
        "<li>" + text + " "
        '<a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a></li>'
    )
    g1 = _bullet_items(html, datetime.date(2026, 6, 9))[0]["guid"]
    g2 = _bullet_items(html, datetime.date(2026, 6, 10))[0]["guid"]
    assert g1 != g2
    assert g1.startswith("wiki-20260609-") and g2.startswith("wiki-20260610-")
    # Same normalized text -> identical sha1 suffix on both days.
    assert g1.split("-")[-1] == g2.split("-")[-1] == hashlib.sha1(
        text.encode("utf-8")
    ).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# _bullet_items — property (guid == independent sha1 of normalized text)
# --------------------------------------------------------------------------- #
@pytest.mark.property
def test_bullet_guid_matches_independent_sha1_property():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    words = st.lists(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8),
        min_size=6,
        max_size=14,
    )

    @settings(max_examples=60)
    @given(words)
    def check(ws):
        text = " ".join(ws)  # single-space alnum words -> normalization is identity
        html = (
            "<li>" + text + " "
            '<a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a></li>'
        )
        items = _bullet_items(html, DAY)
        assert len(items) == 1
        assert items[0]["guid"] == _expected_guid(text)
        assert items[0]["title"] == text[:140]

    check()


# --------------------------------------------------------------------------- #
# fetch_items — day loop, error handling, stderr/raise semantics
# --------------------------------------------------------------------------- #
def test_fetch_iterates_today_and_yesterday(make_result):
    today = datetime.date(2026, 6, 10)
    d0, d1 = today, today - datetime.timedelta(days=1)
    html0 = (
        "<li>Event happening today of considerable importance and note "
        '<a rel="nofollow" class="external text" href="https://ex.com/today">(S)</a></li>'
    )
    html1 = (
        "<li>Event that happened yesterday of considerable importance too "
        '<a rel="nofollow" class="external text" href="https://ex.com/yday">(S)</a></li>'
    )
    client = _FakeClient(
        responses={
            api_url(d0): make_result(content=_parse_body(html0)),
            api_url(d1): make_result(content=_parse_body(html1)),
        }
    )
    items = fetch_items(client, {}, days=2, today=today)

    assert [i["raw_url"] for i in items] == ["https://ex.com/today", "https://ex.com/yday"]
    assert [i["published_at"] for i in items] == [
        "2026-06-10T00:00:00+00:00",
        "2026-06-09T00:00:00+00:00",
    ]
    assert client.requested == [api_url(d0), api_url(d1)]
    # Always a fresh (non-conditional) GET.
    assert all(cond is False for (_, cond) in client.calls)


def test_fetch_default_days_is_two(make_result):
    today = datetime.date(2026, 6, 10)
    d0, d1 = today, today - datetime.timedelta(days=1)
    empty = make_result(content=_parse_body("<p>none</p>"))
    client = _FakeClient(responses={api_url(d0): empty, api_url(d1): empty})
    fetch_items(client, {}, today=today)  # rely on default days=2
    assert client.requested == [api_url(d0), api_url(d1)]


@pytest.mark.parametrize("days", [0, -1, -5])
def test_fetch_days_clamped_to_one(make_result, days):
    today = datetime.date(2026, 6, 10)
    client = _FakeClient(
        responses={api_url(today): make_result(content=_parse_body("<p>none</p>"))}
    )
    fetch_items(client, {}, days=days, today=today)
    assert client.requested == [api_url(today)]  # range(max(1, days)) -> single day


def test_fetch_source_row_is_never_dereferenced(make_result):
    today = datetime.date(2026, 6, 10)
    html = (
        "<li>A perfectly valid world event long enough to be kept in the digest "
        '<a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a></li>'
    )
    resp = {api_url(today): make_result(content=_parse_body(html))}
    # None and a junk dict both work: source_row is accepted but ignored.
    items_none = fetch_items(_FakeClient(responses=resp), None, days=1, today=today)
    items_junk = fetch_items(
        _FakeClient(responses=resp), {"weird": object()}, days=1, today=today
    )
    assert len(items_none) == 1 and len(items_junk) == 1
    assert items_none[0]["raw_url"] == "https://ex.com/x"


def test_fetch_missing_today_page_returns_yesterday_and_warns(make_result, capsys):
    today = datetime.date(2026, 6, 10)
    d0, d1 = today, today - datetime.timedelta(days=1)
    html1 = (
        "<li>Yesterday's event which is long enough to survive the header filter "
        '<a rel="nofollow" class="external text" href="https://ex.com/yday">(S)</a></li>'
    )
    client = _FakeClient(
        responses={
            api_url(d0): make_result(content=_err_body("missingtitle")),
            api_url(d1): make_result(content=_parse_body(html1)),
        }
    )
    items = fetch_items(client, {}, days=2, today=today)

    assert len(items) == 1
    assert items[0]["raw_url"] == "https://ex.com/yday"
    err = capsys.readouterr().err
    assert "wiki-current-events: 2026-06-10: missingtitle" in err


def test_fetch_partial_success_does_not_raise(make_result, capsys):
    # Today OK, yesterday hard-fails -> still returns today's items + a stderr note.
    today = datetime.date(2026, 6, 10)
    d0, d1 = today, today - datetime.timedelta(days=1)
    html0 = (
        "<li>Today's event which is long enough to survive the header filter here "
        '<a rel="nofollow" class="external text" href="https://ex.com/today">(S)</a></li>'
    )
    client = _FakeClient(
        responses={
            api_url(d0): make_result(content=_parse_body(html0)),
            api_url(d1): make_result(content=None, status=500),
        }
    )
    items = fetch_items(client, {}, days=2, today=today)
    assert [i["raw_url"] for i in items] == ["https://ex.com/today"]
    assert "wiki-current-events: 2026-06-09: HTTP 500" in capsys.readouterr().err


def test_fetch_all_days_fail_raises_aggregated(make_result, capsys):
    today = datetime.date(2026, 6, 10)
    d0, d1 = today, today - datetime.timedelta(days=1)
    client = _FakeClient(
        responses={
            api_url(d0): make_result(content=None, status=500),
            api_url(d1): make_result(content=b"not json at all", status=200),
        }
    )
    with pytest.raises(RuntimeError) as exc:
        fetch_items(client, {}, days=2, today=today)

    msg = str(exc.value)
    assert msg.startswith("wikipedia current events failed:")
    assert "2026-06-10: HTTP 500" in msg
    assert "2026-06-09: bad JSON" in msg
    # On the raise path the per-day print loop is never reached.
    assert capsys.readouterr().err == ""


def test_fetch_empty_body_reports_http_status(make_result):
    today = datetime.date(2026, 6, 10)
    client = _FakeClient(
        responses={api_url(today): make_result(content=b"", status=200)}
    )
    with pytest.raises(RuntimeError, match="2026-06-10: HTTP 200"):
        fetch_items(client, {}, days=1, today=today)


def test_fetch_error_string_preferred_over_status(make_result):
    today = datetime.date(2026, 6, 10)
    client = _FakeClient(
        responses={
            api_url(today): make_result(content=None, status=0, error="ConnectError: boom")
        }
    )
    with pytest.raises(RuntimeError, match="ConnectError: boom"):
        fetch_items(client, {}, days=1, today=today)


def test_fetch_error_key_alone_raises(make_result):
    today = datetime.date(2026, 6, 10)
    client = _FakeClient(
        responses={api_url(today): make_result(content=_err_body("missingtitle"))}
    )
    with pytest.raises(RuntimeError, match="missingtitle"):
        fetch_items(client, {}, days=1, today=today)


def test_fetch_empty_parse_text_raises(make_result):
    today = datetime.date(2026, 6, 10)
    body = json.dumps({"parse": {"title": "T", "text": {}}}).encode("utf-8")
    client = _FakeClient(responses={api_url(today): make_result(content=body)})
    with pytest.raises(RuntimeError, match="empty parse text"):
        fetch_items(client, {}, days=1, today=today)


def test_fetch_missing_parse_key_raises(make_result):
    today = datetime.date(2026, 6, 10)
    body = json.dumps({"batchcomplete": ""}).encode("utf-8")
    client = _FakeClient(responses={api_url(today): make_result(content=body)})
    with pytest.raises(RuntimeError, match="empty parse text"):
        fetch_items(client, {}, days=1, today=today)


def test_fetch_parse_text_as_bare_string_is_accepted(make_result):
    # Some responses put the HTML directly under parse.text (not a {"*": ...} dict);
    # the isinstance(text, dict) guard handles that branch.
    today = datetime.date(2026, 6, 10)
    html = (
        "<li>A bare-string parse payload event long enough to be retained here "
        '<a rel="nofollow" class="external text" href="https://ex.com/bare">(S)</a></li>'
    )
    body = json.dumps({"parse": {"title": "T", "text": html}}).encode("utf-8")
    client = _FakeClient(responses={api_url(today): make_result(content=body)})
    items = fetch_items(client, {}, days=1, today=today)
    assert len(items) == 1
    assert items[0]["raw_url"] == "https://ex.com/bare"


def test_fetch_valid_page_with_no_events_returns_empty_without_raising(make_result):
    # 200 + valid parse HTML but zero qualifying bullets -> no errors, no raise, [].
    today = datetime.date(2026, 6, 10)
    html = '<li><a href="/wiki/Topic" title="t">Short topic header</a></li>'
    client = _FakeClient(responses={api_url(today): make_result(content=_parse_body(html))})
    assert fetch_items(client, {}, days=1, today=today) == []


def test_fetch_success_prints_nothing_to_stderr(make_result, capsys):
    today = datetime.date(2026, 6, 10)
    html = (
        "<li>A clean successful event that is comfortably over forty characters long "
        '<a rel="nofollow" class="external text" href="https://ex.com/x">(S)</a></li>'
    )
    client = _FakeClient(responses={api_url(today): make_result(content=_parse_body(html))})
    fetch_items(client, {}, days=1, today=today)
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# Integration — recorded trimmed MediaWiki day page through fetch_items
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_fetch_recorded_day_page_end_to_end(load_text, make_result):
    day = datetime.date(2026, 6, 9)
    html = load_text("wikipedia_events_day.html")
    client = _FakeClient(
        responses={api_url(day): make_result(content=_parse_body(html))}
    )
    items = fetch_items(client, {}, days=1, today=day)

    # Three event bullets survive; the "Russian invasion of Ukraine" topic header
    # (no citation, short own-text) is dropped.
    assert len(items) == 3
    raw_urls = [i["raw_url"] for i in items]
    assert raw_urls == [
        "https://www.reuters.com/world/ukraine-example",  # citation wins
        "https://apnews.com/article/example",  # citation wins
        "https://en.wikipedia.org/wiki/Anglerfish",  # wiki fallback (no citation)
    ]

    for item in items:
        assert item["guid"].startswith("wiki-20260609-")
        assert item["published_at"] == "2026-06-09T00:00:00+00:00"
        assert item["extra"]["surface"] == "wikipedia-current-events"
        assert item["extra"]["date"] == "2026-06-09"
        assert item["author"] is None
        assert len(item["title"]) <= 140

    # Spot-check mappings that exercise unescaping + wiki_url capture.
    kharkiv = items[0]
    assert kharkiv["extra"]["wiki_url"] == "https://en.wikipedia.org/wiki/Kharkiv"
    # Full title: the "(Reuters)" citation anchor and the /wiki/Kharkiv anchor
    # markup are gone, trailing "." stripped.
    assert kharkiv["title"] == (
        "Russian forces launch a large-scale missile strike on the city of "
        "Kharkiv, killing at least five civilians and wounding dozens more"
    )

    cabinet = items[1]
    assert cabinet["extra"]["wiki_url"] == "https://en.wikipedia.org/wiki/Government_of_Example"
    # &#39; -> ' unescaping across the whole title.
    assert cabinet["title"] == (
        "The Government of Example announces a new cabinet following "
        "last week's general election"
    )

    angler = items[2]
    assert angler["extra"]["wiki_url"] == "https://en.wikipedia.org/wiki/Anglerfish"
    # Wiki-fallback bullet (no external citation). On this realistic bullet the
    # title truncates to EXACTLY 140 chars, cut mid-word ("...the Paci")...
    assert len(angler["title"]) == 140
    assert angler["title"] == (
        "Researchers publish a landmark study describing a newly discovered "
        "species of deep-sea anglerfish living near hydrothermal vents in the Paci"
    )
    # ...but the guid hashes the FULL untruncated text ("...Pacific Ocean").
    assert angler["guid"] == _expected_guid(
        "Researchers publish a landmark study describing a newly discovered "
        "species of deep-sea anglerfish living near hydrothermal vents in the "
        "Pacific Ocean",
        day=day,
    )


# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #
def test_module_constants_shape():
    assert API_URL % "X" == (
        "https://en.wikipedia.org/w/api.php?action=parse&page=X&format=json&prop=text"
    )
    assert "wikipedia.org" in WIKIMEDIA_SUFFIXES
    assert "wikimedia.org" in WIKIMEDIA_SUFFIXES
    assert "wikidata.org" in WIKIMEDIA_SUFFIXES
    assert isinstance(WIKIMEDIA_SUFFIXES, tuple)


# --------------------------------------------------------------------------- #
# Live smoke — real MediaWiki parse API. Deselected by default (-m 'not live')
# and env-guarded so `-m live` without SIGNAL_LIVE skips cleanly.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_current_events_returns_items(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real Wikipedia parse API")

    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    client.host_intervals = {}
    client.default_interval = 0.0
    try:
        items = fetch_items(client, {}, days=2)
    finally:
        client.close()

    assert len(items) > 0
    for item in items:
        assert item["guid"].startswith("wiki-")
        assert item["raw_url"]
        assert item["title"]
        assert item["extra"]["surface"] == "wikipedia-current-events"
