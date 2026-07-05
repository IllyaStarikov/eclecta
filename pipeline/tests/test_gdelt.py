"""Unit tests for ``signalpipe.ingest.gdelt`` — the GDELT 2.0 DOC API (artlist) parser.

Hermetic by construction: ``fetch_items`` only reaches the network through the injected
``PoliteClient.fetch``, replaced here with a ``FakePoliteClient`` keyed on exact URLs. Two
module-computed URLs matter:

* ``query_url(q)`` percent-encodes the WHOLE query (spaces, parens, colons) into the
  ``DOC_API`` template; a source row carrying a full DOC URL fetches that single URL, while a
  bare/empty URL falls back to fetching every ``DEFAULT_QUERIES`` entry (two requests).

Hazards handled here (see briefing):
* ``fetch_items`` mutates ``client.host_intervals`` via ``setdefault`` — the stock
  ``FakePoliteClient`` has no such attribute, so ``_client`` attaches a fresh dict and tests
  assert the pin (and that ``setdefault`` does not clobber a pre-existing value).
* ``res.content`` is treated as BYTES (``res.content[:80].decode(...)`` and ``json.loads``),
  so every canned body is bytes.
* The all-fail ``RuntimeError`` depends on ``len(http_errors) == len(urls)``; single-URL vs
  two-URL sources are parametrized carefully. Non-JSON / no-articles payloads are WARNINGS,
  not HTTP errors, so they never trip the raise.
* Degraded-query warnings print to ``sys.stderr`` — captured via ``capsys``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

from signalpipe.ingest.gdelt import (
    DEFAULT_QUERIES,
    DOC_API,
    _seendate_iso,
    _urls_for_source,
    fetch_items,
    query_url,
)

# The two URLs a bare-endpoint source expands to (DEFAULT_QUERIES fallback).
Q0_URL = query_url(DEFAULT_QUERIES[0])
Q1_URL = query_url(DEFAULT_QUERIES[1])

# A full DOC URL (contains api.gdeltproject.org AND query=) → single fetch.
FULL_URL = query_url("standalone test query")


# --------------------------------------------------------------------------- #
# Local builders
# --------------------------------------------------------------------------- #
def _art(**over: Any) -> Dict[str, Any]:
    """A single GDELT artlist article with sensible defaults.

    Pass ``_drop=[...]`` to model absent keys; pass ``key=None`` for present-but-null.
    """
    drop = over.pop("_drop", [])
    art: Dict[str, Any] = {
        "url": "https://ex.com/a",
        "title": "A Title",
        "seendate": "20260610T083000Z",
        "domain": "ex.com",
        "sourcecountry": "United States",
        "language": "English",
    }
    art.update(over)
    for k in drop:
        art.pop(k, None)
    return art


def _payload(articles: List[Dict[str, Any]]) -> bytes:
    """Encode the artlist envelope GDELT returns: {"articles": [...]}."""
    return json.dumps({"articles": articles}).encode("utf-8")


def _client(fake_client, responses=None, default=None, host_intervals=None):
    """Build a FakePoliteClient and attach the ``host_intervals`` dict fetch_items mutates."""
    c = fake_client(responses=responses, default=default)
    c.host_intervals = {} if host_intervals is None else host_intervals
    return c


def _bare_source() -> Dict[str, Any]:
    """A source row whose URL does not carry a query → expands to DEFAULT_QUERIES."""
    return {"url": ""}


def _full_source() -> Dict[str, Any]:
    """A source row carrying a full DOC query URL → single fetch of that URL."""
    return {"url": FULL_URL}


# --------------------------------------------------------------------------- #
# query_url — full percent-encoding into the DOC_API template
# --------------------------------------------------------------------------- #
def test_query_url_percent_encodes_spaces_and_parens():
    q = "(a OR b) sourcelang:eng"
    result = query_url(q)
    # The encoded query portion must not carry raw spaces/parens/colons.
    encoded = result.split("query=", 1)[1].split("&", 1)[0]
    assert " " not in encoded
    assert "(" not in encoded and ")" not in encoded
    assert ":" not in encoded
    assert "%20" in encoded and "%28" in encoded and "%29" in encoded


def test_query_url_round_trips_via_parse_qs():
    q = "(quantum computing OR semiconductor) sourcelang:eng"
    result = query_url(q)
    # parse_qs decodes %20/%28/... back to the original literal query string.
    assert parse_qs(urlsplit(result).query)["query"][0] == q


def test_query_url_uses_doc_api_template():
    result = query_url("x")
    assert result == DOC_API % "x"  # "x" has no chars needing escaping
    assert result.startswith("https://api.gdeltproject.org/api/v2/doc/doc?query=")
    assert result.endswith("&mode=artlist&format=json&maxrecords=50&timespan=1d")


def test_doc_api_and_default_queries_constants():
    assert "api.gdeltproject.org" in DOC_API
    assert DOC_API.count("%s") == 1
    assert DEFAULT_QUERIES == [
        "(artificial intelligence OR machine learning) sourcelang:eng",
        "(quantum computing OR semiconductor) sourcelang:eng",
    ]


# --------------------------------------------------------------------------- #
# _seendate_iso — strptime + UTC, None on TypeError/ValueError
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("20260610T083000Z", "2026-06-10T08:30:00+00:00"),
        ("20260101T000000Z", "2026-01-01T00:00:00+00:00"),
        ("20261231T235959Z", "2026-12-31T23:59:59+00:00"),
    ],
)
def test_seendate_iso_valid(value, expected):
    assert _seendate_iso(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,               # TypeError inside strptime → None
        "",                 # ValueError (empty) → None
        "not-a-date",       # ValueError (unparseable) → None
        "20260610T083000",  # missing trailing Z → ValueError → None
        "20261301T083000Z",  # month 13 → ValueError → None
        "2026-06-10T08:30:00Z",  # wrong separators → ValueError → None
        12345,              # non-str → TypeError → None
    ],
)
def test_seendate_iso_invalid_returns_none(value):
    assert _seendate_iso(value) is None


# --------------------------------------------------------------------------- #
# _urls_for_source — branch coverage (pure, no client)
# --------------------------------------------------------------------------- #
def test_urls_for_source_full_doc_url_returns_that_url():
    assert _urls_for_source({"url": FULL_URL}, None) == [FULL_URL]


def test_urls_for_source_full_url_ignores_explicit_queries():
    # A full DOC URL short-circuits: queries are irrelevant.
    assert _urls_for_source({"url": FULL_URL}, ["ignored", "also-ignored"]) == [FULL_URL]


def test_urls_for_source_bare_endpoint_falls_back_to_default_queries():
    urls = _urls_for_source({"url": "https://api.gdeltproject.org/api/v2/doc/doc"}, None)
    assert urls == [query_url(q) for q in DEFAULT_QUERIES]
    assert len(urls) == 2


def test_urls_for_source_empty_url_uses_default_queries():
    assert _urls_for_source({"url": ""}, None) == [Q0_URL, Q1_URL]


def test_urls_for_source_none_url_uses_default_queries():
    # source_row["url"] is None → `url = url or ""`.
    assert _urls_for_source({"url": None}, None) == [Q0_URL, Q1_URL]


def test_urls_for_source_explicit_queries_map_per_query():
    urls = _urls_for_source({"url": ""}, ["alpha", "beta gamma"])
    assert urls == [query_url("alpha"), query_url("beta gamma")]


def test_urls_for_source_empty_queries_list_falls_back_to_default():
    # queries == [] is falsy → `queries or DEFAULT_QUERIES` picks the defaults.
    assert _urls_for_source({"url": ""}, []) == [Q0_URL, Q1_URL]


def test_urls_for_source_query_present_but_wrong_host_uses_default():
    # Needs BOTH conditions: host match AND query=. Wrong host → default queries.
    row = {"url": "https://example.com/api?query=foo"}
    assert _urls_for_source(row, None) == [Q0_URL, Q1_URL]


def test_urls_for_source_host_present_but_no_query_uses_default():
    # Host matches but the query component has no `query=` → default queries.
    row = {"url": "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist"}
    assert _urls_for_source(row, None) == [Q0_URL, Q1_URL]


# --------------------------------------------------------------------------- #
# fetch_items — host-interval pin side effect
# --------------------------------------------------------------------------- #
def test_fetch_items_pins_host_interval(fake_client, make_result):
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([_art()]))}
    )
    fetch_items(client, _full_source())
    assert client.host_intervals["api.gdeltproject.org"] == 6.0


def test_fetch_items_setdefault_preserves_existing_interval(fake_client, make_result):
    # setdefault must NOT clobber a pre-existing pin.
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=_payload([_art()]))},
        host_intervals={"api.gdeltproject.org": 3.0},
    )
    fetch_items(client, _full_source())
    assert client.host_intervals["api.gdeltproject.org"] == 3.0


# --------------------------------------------------------------------------- #
# fetch_items — happy path / field mapping (inline)
# --------------------------------------------------------------------------- #
def test_fetch_items_maps_every_field(fake_client, make_result):
    art = _art(
        url="https://ex.com/story",
        title="Big Story",
        seendate="20260610T083000Z",
        domain="ex.com",
        sourcecountry="United States",
        language="English",
    )
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([art]))}
    )
    items = fetch_items(client, _full_source())
    assert items == [
        {
            "guid": "gdelt-https://ex.com/story",
            "raw_url": "https://ex.com/story",
            "title": "Big Story",
            "author": None,
            "published_at": "2026-06-10T08:30:00+00:00",
            "points": None,
            "comments": None,
            "extra": {
                "surface": "gdelt",
                "domain": "ex.com",
                "sourcecountry": "United States",
                "language": "English",
            },
        }
    ]


def test_fetch_items_guid_prefix(fake_client, make_result):
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=_payload([_art(url="https://q.io/z")]))},
    )
    (item,) = fetch_items(client, _full_source())
    assert item["guid"] == "gdelt-https://q.io/z"


def test_fetch_items_strips_url_and_title(fake_client, make_result):
    art = _art(url="  https://ex.com/pad  ", title="  Padded Title \n")
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([art]))}
    )
    (item,) = fetch_items(client, _full_source())
    assert item["raw_url"] == "https://ex.com/pad"
    assert item["guid"] == "gdelt-https://ex.com/pad"
    assert item["title"] == "Padded Title"


def test_fetch_items_extra_fields_default_none_when_absent(fake_client, make_result):
    art = _art(_drop=["domain", "sourcecountry", "language"])
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([art]))}
    )
    (item,) = fetch_items(client, _full_source())
    assert item["extra"] == {
        "surface": "gdelt",
        "domain": None,
        "sourcecountry": None,
        "language": None,
    }


def test_fetch_items_published_at_none_when_seendate_absent(fake_client, make_result):
    art = _art(_drop=["seendate"])
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([art]))}
    )
    (item,) = fetch_items(client, _full_source())
    assert item["published_at"] is None


def test_fetch_items_published_at_none_when_seendate_malformed(fake_client, make_result):
    art = _art(seendate="garbage")
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([art]))}
    )
    (item,) = fetch_items(client, _full_source())
    assert item["published_at"] is None


def test_fetch_items_full_url_is_single_fetch(fake_client, make_result):
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([_art()]))}
    )
    fetch_items(client, _full_source())
    assert client.requested == [FULL_URL]


def test_fetch_items_empty_articles_list_returns_empty(fake_client, make_result):
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([]))}
    )
    assert fetch_items(client, _full_source()) == []


# --------------------------------------------------------------------------- #
# fetch_items — required-field skip filter (url AND title both required)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_url", ["", "   ", None])
def test_fetch_items_skips_blank_or_null_url(fake_client, make_result, bad_url):
    good = _art(url="https://ex.com/keep", title="keep")
    bad = _art(url=bad_url, title="has title but no url")
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([bad, good]))}
    )
    items = fetch_items(client, _full_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/keep"]


@pytest.mark.parametrize("bad_title", ["", "   ", None])
def test_fetch_items_skips_blank_or_null_title(fake_client, make_result, bad_title):
    good = _art(url="https://ex.com/keep", title="keep")
    bad = _art(url="https://ex.com/notitle", title=bad_title)
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([good, bad]))}
    )
    items = fetch_items(client, _full_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/keep"]


def test_fetch_items_skips_missing_url_key(fake_client, make_result):
    good = _art(url="https://ex.com/keep", title="keep")
    bad = _art(_drop=["url"], title="no url key")
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([bad, good]))}
    )
    items = fetch_items(client, _full_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/keep"]


def test_fetch_items_skips_missing_title_key(fake_client, make_result):
    good = _art(url="https://ex.com/keep", title="keep")
    bad = _art(url="https://ex.com/notitle", _drop=["title"])
    client = _client(
        fake_client, responses={FULL_URL: make_result(content=_payload([good, bad]))}
    )
    items = fetch_items(client, _full_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/keep"]


# --------------------------------------------------------------------------- #
# fetch_items — dedup by guid
# --------------------------------------------------------------------------- #
def test_fetch_items_dedups_within_one_payload(fake_client, make_result):
    dup = _art(url="https://ex.com/same", title="Same")
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=_payload([dup, dict(dup)]))},
    )
    items = fetch_items(client, _full_source())
    assert len(items) == 1


def test_fetch_items_dedups_across_queries_and_pins_interval(fake_client, make_result):
    shared = _art(url="https://ex.com/shared", title="Shared Story")
    client = _client(
        fake_client,
        responses={
            Q0_URL: make_result(content=_payload([shared])),
            Q1_URL: make_result(content=_payload([dict(shared)])),
        },
    )
    items = fetch_items(client, _bare_source())
    assert len(items) == 1
    assert items[0]["guid"] == "gdelt-https://ex.com/shared"
    assert client.requested == [Q0_URL, Q1_URL]
    assert client.host_intervals["api.gdeltproject.org"] == 6.0


def test_fetch_items_preserves_order_across_queries(fake_client, make_result):
    a = _art(url="https://ex.com/a", title="A")
    b = _art(url="https://ex.com/b", title="B")
    client = _client(
        fake_client,
        responses={
            Q0_URL: make_result(content=_payload([a])),
            Q1_URL: make_result(content=_payload([b])),
        },
    )
    items = fetch_items(client, _bare_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/a", "https://ex.com/b"]


# --------------------------------------------------------------------------- #
# fetch_items — payload-shape guards (each degrades silently, never crashes)
# --------------------------------------------------------------------------- #
def test_fetch_items_json_list_payload_degrades(fake_client, make_result, capsys):
    # Top-level JSON is a list, not a dict → articles = None → "no articles list".
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=json.dumps([1, 2, 3]).encode("utf-8"))},
    )
    assert fetch_items(client, _full_source()) == []
    assert "no articles list in payload" in capsys.readouterr().err


def test_fetch_items_dict_without_articles_degrades(fake_client, make_result, capsys):
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=json.dumps({"status": "ok"}).encode())},
    )
    assert fetch_items(client, _full_source()) == []
    assert "no articles list in payload" in capsys.readouterr().err


def test_fetch_items_articles_not_a_list_degrades(fake_client, make_result, capsys):
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=json.dumps({"articles": "nope"}).encode())},
    )
    assert fetch_items(client, _full_source()) == []
    assert "no articles list in payload" in capsys.readouterr().err


def test_fetch_items_skips_non_dict_article_elements(fake_client, make_result):
    good = _art(url="https://ex.com/keep", title="keep")
    body = json.dumps({"articles": [123, "str", None, good]}).encode("utf-8")
    client = _client(fake_client, responses={FULL_URL: make_result(content=body)})
    items = fetch_items(client, _full_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/keep"]


# --------------------------------------------------------------------------- #
# fetch_items — non-JSON 200 degrades to a warning, never raises
# --------------------------------------------------------------------------- #
def test_fetch_items_non_json_single_url_returns_empty(fake_client, make_result, capsys):
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=b"You have exceeded your rate limit")},
    )
    # Non-JSON is a warning, not an http_error → no raise even for a lone URL.
    assert fetch_items(client, _full_source()) == []
    err = capsys.readouterr().err
    assert "non-JSON payload" in err
    assert "You have exceeded your rate limit" in err


def test_fetch_items_non_json_snippet_truncated_to_80_bytes(fake_client, make_result, capsys):
    payload = b"X" * 200  # invalid JSON, long enough to exercise the [:80] slice
    client = _client(fake_client, responses={FULL_URL: make_result(content=payload)})
    fetch_items(client, _full_source())
    err = capsys.readouterr().err
    assert "non-JSON payload (" + ("X" * 80) + ")" in err
    assert "X" * 81 not in err


def test_fetch_items_mixed_json_and_non_json_no_raise(fake_client, make_result, capsys):
    good = _art(url="https://ex.com/good", title="Good")
    client = _client(
        fake_client,
        responses={
            Q0_URL: make_result(content=_payload([good])),
            Q1_URL: make_result(content=b"<html>429 Too Many Requests</html>"),
        },
    )
    items = fetch_items(client, _bare_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/good"]
    assert "non-JSON payload" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# fetch_items — HTTP error policy (raise only when ALL queries fail)
# --------------------------------------------------------------------------- #
def test_fetch_items_single_url_all_fail_raises(fake_client, make_result, capsys):
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=b"", status=429, error=None)},
    )
    with pytest.raises(RuntimeError, match="gdelt: all queries failed"):
        fetch_items(client, _full_source())
    # On raise, the degraded-warning print loop is never reached.
    assert "query degraded" not in capsys.readouterr().err


def test_fetch_items_all_fail_message_joins_errors(fake_client, make_result):
    client = _client(
        fake_client,
        responses={
            Q0_URL: make_result(content=None, status=500, error="boom one"),
            Q1_URL: make_result(content=None, status=503, error="boom two"),
        },
    )
    with pytest.raises(RuntimeError) as exc:
        fetch_items(client, _bare_source())
    msg = str(exc.value)
    assert "boom one" in msg and "boom two" in msg
    assert "; " in msg


def test_fetch_items_error_message_preferred_over_status(fake_client, make_result):
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=None, status=0, error="ConnectError: x")},
    )
    with pytest.raises(RuntimeError, match="ConnectError: x"):
        fetch_items(client, _full_source())


def test_fetch_items_status_message_used_when_no_error(fake_client, make_result):
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=None, status=502, error=None)},
    )
    with pytest.raises(RuntimeError, match="HTTP 502"):
        fetch_items(client, _full_source())


def test_fetch_items_empty_body_200_counts_as_http_error(fake_client, make_result):
    # status 200 but empty body → `not res.content` → http_error "HTTP 200" → single URL raises.
    client = _client(
        fake_client,
        responses={FULL_URL: make_result(content=b"", status=200, error=None)},
    )
    with pytest.raises(RuntimeError, match="HTTP 200"):
        fetch_items(client, _full_source())


def test_fetch_items_partial_http_failure_no_raise_but_warns(fake_client, make_result, capsys):
    good = _art(url="https://ex.com/good", title="Good")
    client = _client(
        fake_client,
        responses={
            Q0_URL: make_result(content=_payload([good])),
            Q1_URL: make_result(content=None, status=500, error="upstream 500"),
        },
    )
    items = fetch_items(client, _bare_source())
    assert [i["raw_url"] for i in items] == ["https://ex.com/good"]
    err = capsys.readouterr().err
    assert "query degraded (upstream 500)" in err


def test_fetch_items_http_error_and_warning_both_printed(fake_client, make_result, capsys):
    # Three explicit queries: one JSON-with-article, one HTTP failure, one non-JSON body.
    # The good one prevents the all-fail raise, so BOTH diagnostics reach stderr
    # (the print loop iterates http_errors + warnings).
    good = _art(url="https://ex.com/good", title="Good")
    qs = ["good", "boom", "junk"]
    urls = [query_url(q) for q in qs]
    client = _client(
        fake_client,
        responses={
            urls[0]: make_result(content=_payload([good])),
            urls[1]: make_result(content=None, status=503, error="down"),
            urls[2]: make_result(content=b"not json at all"),
        },
    )
    items = fetch_items(client, {"url": ""}, queries=qs)
    assert [i["raw_url"] for i in items] == ["https://ex.com/good"]
    err = capsys.readouterr().err
    assert "query degraded (down)" in err
    assert "non-JSON payload" in err


# --------------------------------------------------------------------------- #
# fetch_items — recorded artlist fixture (field mapping + skip filter together)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_fetch_items_from_recorded_fixture(fake_client, make_result, load_bytes):
    body = load_bytes("gdelt_artlist.json")
    client = _client(fake_client, responses={FULL_URL: make_result(content=body)})
    items = fetch_items(client, _full_source())

    # Two well-formed articles; the empty-url and empty-title rows are dropped.
    assert [i["raw_url"] for i in items] == [
        "https://example.com/ai-news",
        "https://news.example.org/quantum",
    ]
    assert items[0] == {
        "guid": "gdelt-https://example.com/ai-news",
        "raw_url": "https://example.com/ai-news",
        "title": "AI Breakthrough Announced",
        "author": None,
        "published_at": "2026-06-10T08:30:00+00:00",
        "points": None,
        "comments": None,
        "extra": {
            "surface": "gdelt",
            "domain": "example.com",
            "sourcecountry": "United States",
            "language": "English",
        },
    }
    assert items[1]["published_at"] == "2026-06-11T12:00:00+00:00"
    assert items[1]["extra"]["sourcecountry"] == "United Kingdom"


# --------------------------------------------------------------------------- #
# Live smoke — real GDELT DOC API. Deselected by default and env-guarded.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_gdelt_fetch(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real GDELT DOC API")

    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    client.host_intervals = {}
    client.default_interval = 0.0
    try:
        items = fetch_items(client, {"url": ""})
    finally:
        client.close()

    for item in items:
        assert item["guid"].startswith("gdelt-")
        assert item["raw_url"]
        assert item["title"]
