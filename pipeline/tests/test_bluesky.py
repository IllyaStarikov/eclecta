"""Unit tests for ``signalpipe.ingest.bluesky`` — the Bluesky trending-TOPICS parser.

Everything is hermetic: ``fetch_items`` only touches the network through the injected
``PoliteClient.fetch``, which we replace with a ``FakePoliteClient`` keyed on the module
constant ``TRENDS_URL`` (``source_row`` is entirely unused by this module).

The whole contract is "never throw, degrade to ``[]`` with a stderr warning", so most of
these tests exercise a single defensive branch and confirm no exception escapes. Expected
values are derived from the real code path in ``bluesky.py`` (guid is ``bsky-<topic.lower()>``,
raw_url is ``SEARCH_URL % quote(topic)``, feed_link is ``https://bsky.app`` + link).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

import pytest

from signalpipe.ingest.bluesky import SEARCH_URL, TRENDS_URL, fetch_items


# --------------------------------------------------------------------------- #
# Local builders — construct getTrendingTopics response bodies inline.
# --------------------------------------------------------------------------- #
def _topic(**over: Any) -> Dict[str, Any]:
    """A single trending-topic entry with sensible defaults; override via kwargs.

    Pass ``_drop=[...]`` to model a truly absent key.
    """
    drop = over.pop("_drop", [])
    t: Dict[str, Any] = {
        "topic": "Compilers",
        "displayName": "Compiler News",
        "link": "/search?q=Compilers",
    }
    t.update(over)
    for k in drop:
        t.pop(k, None)
    return t


def _body(topics: Any) -> bytes:
    """Serialize a ``{"topics": ...}`` payload to bytes (topics may be any JSON value)."""
    return json.dumps({"topics": topics}).encode("utf-8")


def _client(fake_client, make_result, *, content: Any, status: int = 200,
            error: Optional[str] = None):
    """Build a FakePoliteClient with a single canned response keyed on ``TRENDS_URL``."""
    return fake_client(
        responses={TRENDS_URL: make_result(content=content, status=status, error=error)}
    )


# --------------------------------------------------------------------------- #
# Happy path & full field mapping
# --------------------------------------------------------------------------- #
def test_happy_path_maps_every_field(fake_client, make_result):
    client = _client(fake_client, make_result, content=_body([_topic()]))
    items = fetch_items(client, {})

    assert len(items) == 1
    assert items[0] == {
        "guid": "bsky-compilers",
        "raw_url": "https://bsky.app/search?q=Compilers",
        "title": "Compiler News",
        "author": None,
        "published_at": None,
        "points": None,
        "comments": None,
        "extra": {
            "surface": "bluesky",
            "aggregator_self_link": True,
            "feed_link": "https://bsky.app/search?q=Compilers",
        },
    }
    # Exactly one request, to the unspecced trends endpoint.
    assert client.requested == [TRENDS_URL]


def test_non_article_fields_are_always_none(fake_client, make_result):
    client = _client(fake_client, make_result, content=_body([_topic()]))
    (item,) = fetch_items(client, {})
    # Topics are phrases, not articles — these are hard-coded None.
    assert item["author"] is None
    assert item["published_at"] is None
    assert item["points"] is None
    assert item["comments"] is None


def test_multiple_topics_preserve_order(fake_client, make_result):
    topics = [
        _topic(topic="Rust", displayName="Rust"),
        _topic(topic="Go", displayName="Golang"),
        _topic(topic="Zig", displayName="Zig lang"),
    ]
    client = _client(fake_client, make_result, content=_body(topics))
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["bsky-rust", "bsky-go", "bsky-zig"]
    assert [i["title"] for i in items] == ["Rust", "Golang", "Zig lang"]


def test_happy_path_is_silent_on_stderr(fake_client, make_result, capsys):
    client = _client(fake_client, make_result, content=_body([_topic()]))
    fetch_items(client, {})
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# title / displayName fallback + stripping
# --------------------------------------------------------------------------- #
def test_display_name_missing_title_falls_back_to_topic(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic="Rust", _drop=["displayName"])]),
    )
    (item,) = fetch_items(client, {})
    assert item["title"] == "Rust"
    assert item["guid"] == "bsky-rust"


def test_display_name_empty_string_falls_back_to_topic(fake_client, make_result):
    # displayName "" is falsy → `t.get("displayName") or topic` picks the topic.
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic="Go", displayName="")]),
    )
    (item,) = fetch_items(client, {})
    assert item["title"] == "Go"


def test_display_name_none_falls_back_to_topic(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic="Elixir", displayName=None)]),
    )
    (item,) = fetch_items(client, {})
    assert item["title"] == "Elixir"


def test_display_name_is_stripped(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(displayName="  Padded News \n")]),
    )
    (item,) = fetch_items(client, {})
    assert item["title"] == "Padded News"


def test_topic_is_stripped_for_guid_and_raw_url(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic="  Padded  ", displayName="Name")]),
    )
    (item,) = fetch_items(client, {})
    assert item["guid"] == "bsky-padded"
    assert item["raw_url"] == "https://bsky.app/search?q=Padded"
    assert item["title"] == "Name"


def test_topic_lower_normalizes_guid_casing(fake_client, make_result):
    # guid uses topic.lower(); raw_url preserves the original casing (quote is case-preserving).
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic="MixedCASE", displayName="D")]),
    )
    (item,) = fetch_items(client, {})
    assert item["guid"] == "bsky-mixedcase"
    assert item["raw_url"] == "https://bsky.app/search?q=MixedCASE"


# --------------------------------------------------------------------------- #
# Skip filter — non-dict entries, missing/blank topic, blank title
# --------------------------------------------------------------------------- #
def test_skips_non_dict_and_empty_entries_keeps_valid(fake_client, make_result):
    topics = ["a bare string", 5, None, {}, _topic(topic="Valid", displayName="Valid Topic")]
    client = _client(fake_client, make_result, content=_body(topics))
    items = fetch_items(client, {})
    assert len(items) == 1
    assert items[0]["guid"] == "bsky-valid"
    assert items[0]["title"] == "Valid Topic"


def test_entry_missing_topic_is_dropped(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([{"displayName": "Only a name, no topic"}]),
    )
    assert fetch_items(client, {}) == []


@pytest.mark.parametrize("blank", ["", "   ", "\t\n ", "\xa0"])
def test_whitespace_or_empty_topic_is_dropped(fake_client, make_result, blank):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic=blank, displayName="Has a name")]),
    )
    assert fetch_items(client, {}) == []


def test_topic_none_is_dropped(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic=None, displayName="Name")]),
    )
    assert fetch_items(client, {}) == []


@pytest.mark.parametrize("blank_title", ["   ", "\t\n ", "\xa0"])
def test_whitespace_only_display_name_drops_entry(fake_client, make_result, blank_title):
    # A truthy-but-blank displayName wins over the topic fallback, then strips to ""
    # → `not title` drops the entry even though a real topic exists.
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic="Valid", displayName=blank_title)]),
    )
    assert fetch_items(client, {}) == []


def test_all_entries_skipped_returns_empty_without_warning(fake_client, make_result, capsys):
    topics = ["str", 5, None, {}, {"topic": ""}]
    client = _client(fake_client, make_result, content=_body(topics))
    assert fetch_items(client, {}) == []
    # A valid (if unproductive) topics list is not a warning condition.
    assert capsys.readouterr().err == ""


def test_empty_topics_list_returns_empty_without_warning(fake_client, make_result, capsys):
    client = _client(fake_client, make_result, content=_body([]))
    assert fetch_items(client, {}) == []
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# feed_link construction from the `link` field
# --------------------------------------------------------------------------- #
def test_feed_link_prefixes_bsky_host(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(link="/topic/ai")]),
    )
    (item,) = fetch_items(client, {})
    assert item["extra"]["feed_link"] == "https://bsky.app/topic/ai"


def test_feed_link_none_when_link_absent(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(_drop=["link"])]),
    )
    (item,) = fetch_items(client, {})
    assert item["extra"]["feed_link"] is None


def test_feed_link_none_when_link_empty_string(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(link="")]),
    )
    (item,) = fetch_items(client, {})
    assert item["extra"]["feed_link"] is None


def test_feed_link_none_when_link_null(fake_client, make_result):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(link=None)]),
    )
    (item,) = fetch_items(client, {})
    assert item["extra"]["feed_link"] is None


def test_extra_marks_aggregator_self_link(fake_client, make_result):
    client = _client(fake_client, make_result, content=_body([_topic()]))
    (item,) = fetch_items(client, {})
    assert item["extra"]["surface"] == "bluesky"
    assert item["extra"]["aggregator_self_link"] is True


# --------------------------------------------------------------------------- #
# URL encoding of the search self-link
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "topic,encoded",
    [
        ("AI & ML", "AI%20%26%20ML"),
        ("C#", "C%23"),
        ("100%", "100%25"),
        ("q?x", "q%3Fx"),
        ("a=b", "a%3Db"),
        ("a/b", "a/b"),  # quote() default safe='/' leaves slashes intact
        ("café", "caf%C3%A9"),  # non-ASCII → UTF-8 percent-encoded
    ],
)
def test_special_chars_are_url_encoded(fake_client, make_result, topic, encoded):
    client = _client(
        fake_client, make_result,
        content=_body([_topic(topic=topic, displayName="D")]),
    )
    (item,) = fetch_items(client, {})
    assert item["raw_url"] == "https://bsky.app/search?q=" + encoded
    assert item["raw_url"] == SEARCH_URL % quote(topic)


# --------------------------------------------------------------------------- #
# NEVER RAISES — non-200 / empty body
# --------------------------------------------------------------------------- #
def test_non_200_returns_empty_and_warns(fake_client, make_result, capsys):
    client = _client(fake_client, make_result, content=None, status=503)
    assert fetch_items(client, {}) == []
    err = capsys.readouterr().err
    assert "bsky: trends fetch failed" in err
    assert "HTTP 503" in err


def test_non_200_short_circuits_even_with_a_body(fake_client, make_result, capsys):
    # A 404 carrying a valid-looking body still degrades (status guard fires first).
    client = _client(
        fake_client, make_result,
        content=_body([_topic()]), status=404,
    )
    assert fetch_items(client, {}) == []
    assert "HTTP 404" in capsys.readouterr().err


def test_error_message_is_preferred_over_http_status(fake_client, make_result, capsys):
    client = _client(
        fake_client, make_result,
        content=None, status=0, error="ConnectError: boom",
    )
    assert fetch_items(client, {}) == []
    err = capsys.readouterr().err
    assert "ConnectError: boom" in err
    assert "HTTP 0" not in err  # error string wins over the status fallback


def test_empty_body_with_200_returns_empty_and_warns(fake_client, make_result, capsys):
    # status 200 but empty content → `not res.content` fires; error None → "HTTP 200".
    client = _client(fake_client, make_result, content=b"", status=200)
    assert fetch_items(client, {}) == []
    err = capsys.readouterr().err
    assert "bsky: trends fetch failed" in err
    assert "HTTP 200" in err


# --------------------------------------------------------------------------- #
# NEVER RAISES — malformed JSON / wrong-shaped payload
# --------------------------------------------------------------------------- #
def test_invalid_json_returns_empty_and_warns(fake_client, make_result, capsys):
    client = _client(fake_client, make_result, content=b"oops not json")
    assert fetch_items(client, {}) == []
    assert "bsky: unexpected payload" in capsys.readouterr().err


@pytest.mark.parametrize("content", [b"[]", b'"a string"', b"123", b"null", b"true"])
def test_non_object_toplevel_json_returns_empty_and_warns(
    fake_client, make_result, capsys, content
):
    # json.loads succeeds but the result has no .get → AttributeError branch.
    client = _client(fake_client, make_result, content=content)
    assert fetch_items(client, {}) == []
    assert "bsky: unexpected payload" in capsys.readouterr().err


def test_missing_topics_key_returns_empty_and_warns(fake_client, make_result, capsys):
    client = _client(fake_client, make_result, content=b"{}")
    assert fetch_items(client, {}) == []
    assert "bsky: no topics list in response" in capsys.readouterr().err


@pytest.mark.parametrize(
    "topics_value",
    ['{"a": 1}', '"a string"', "5", "null", "true"],
)
def test_topics_not_a_list_returns_empty_and_warns(
    fake_client, make_result, capsys, topics_value
):
    content = ('{"topics": %s}' % topics_value).encode("utf-8")
    client = _client(fake_client, make_result, content=content)
    assert fetch_items(client, {}) == []
    assert "bsky: no topics list in response" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# NEVER RAISES — umbrella: every malformed input yields a list, no exception
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "content,status",
    [
        (None, 503),
        (b"", 200),
        (b"oops", 200),
        (b"[]", 200),
        (b"{}", 200),
        (b"null", 200),
        (b'{"topics": 5}', 200),
        (b'{"topics": {"a": 1}}', 200),
        (b'{"topics": "str"}', 200),
        (b'{"topics": null}', 200),
    ],
)
def test_never_raises_always_returns_list(fake_client, make_result, content, status):
    client = _client(fake_client, make_result, content=content, status=status)
    result = fetch_items(client, {})  # must not raise
    assert isinstance(result, list)
    assert result == []


# --------------------------------------------------------------------------- #
# source_row is entirely unused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source_row",
    [{}, None, {"mode": "whatever"}, {"url": "ignored", "slug": "x"}, 12345],
)
def test_source_row_is_ignored(fake_client, make_result, source_row):
    client = _client(fake_client, make_result, content=_body([_topic()]))
    items = fetch_items(client, source_row)
    assert len(items) == 1
    assert items[0]["guid"] == "bsky-compilers"
    assert client.requested == [TRENDS_URL]


def test_fetch_is_unconditional(fake_client, make_result):
    # The parser requests the endpoint exactly once and without conditional caching.
    client = _client(fake_client, make_result, content=_body([_topic()]))
    fetch_items(client, {})
    assert client.requested == [TRENDS_URL]


# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #
def test_module_constants_shape():
    assert TRENDS_URL == (
        "https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics"
    )
    assert SEARCH_URL == "https://bsky.app/search?q=%s"
    assert SEARCH_URL % "hello" == "https://bsky.app/search?q=hello"


# --------------------------------------------------------------------------- #
# Property — quote() encodes losslessly; guid/raw_url invariants hold.
# --------------------------------------------------------------------------- #
@pytest.mark.property
def test_quote_roundtrip_property(fake_client, make_result):
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    # Exclude surrogates ("Cs") — they can't be UTF-8 encoded by quote(), and the
    # product code encodes OUTSIDE its try/except (that is a real, separate edge, not
    # what this invariant covers). Exclude control chars ("Cc") so stripping is
    # predictable. Require a stripped, non-empty topic so the entry survives.
    alphabet = st.characters(blacklist_categories=("Cs", "Cc"))

    @settings(max_examples=200)
    @given(st.text(alphabet=alphabet, min_size=1).filter(lambda s: s == s.strip() and s != ""))
    def _check(topic):
        client = fake_client(
            responses={TRENDS_URL: make_result(content=_body([{"topic": topic}]), status=200)}
        )
        items = fetch_items(client, {})
        assert len(items) == 1
        item = items[0]
        assert item["guid"] == "bsky-" + topic.lower()
        assert item["raw_url"] == "https://bsky.app/search?q=" + quote(topic)
        # The encoded query round-trips back to the exact topic.
        assert unquote(item["raw_url"].split("q=", 1)[1]) == topic

    _check()


# --------------------------------------------------------------------------- #
# Live smoke — real (UNSPECCED) Bluesky endpoint. Deselected by default and
# additionally env-guarded so `-m live` without SIGNAL_LIVE skips cleanly.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_trending_topics_tolerant(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real Bluesky unspecced endpoint")

    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    client.host_intervals = {}
    client.default_interval = 0.0
    try:
        items = fetch_items(client, {})
    finally:
        client.close()

    # Endpoint is UNSPECCED and may 404/return nothing — tolerate empty, but any
    # item that IS returned must satisfy the documented shape.
    assert isinstance(items, list)
    for item in items:
        assert item["guid"].startswith("bsky-")
        assert item["raw_url"].startswith("https://bsky.app/search?q=")
        assert item["title"]
        assert item["extra"]["surface"] == "bluesky"
        assert item["extra"]["aggregator_self_link"] is True
