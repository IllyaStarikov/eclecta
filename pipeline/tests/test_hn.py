"""Unit tests for ``signalpipe.ingest.hn`` — the Hacker News Algolia ingest parser.

Everything is hermetic: ``fetch_items`` only touches the network through the injected
``PoliteClient.fetch``, which we replace with a ``FakePoliteClient`` keyed on the module
constant ``ALGOLIA % page`` (``source_row`` is entirely unused by this module). Epoch->ISO
conversion is deterministic because the code feeds an explicit ``created_at_i`` into
``datetime.fromtimestamp(..., utc)`` — never wall-clock — so expected ISO strings are pinned.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import pytest

from signalpipe.ingest.hn import ALGOLIA, ITEM_URL, fetch_items


# --------------------------------------------------------------------------- #
# Local builders — construct Algolia response bodies inline for hermeticity.
# --------------------------------------------------------------------------- #
def _hit(**over: Any) -> Dict[str, Any]:
    """A single Algolia front-page hit with sensible defaults; override via kwargs.

    Passing ``key=None`` (or popping a key via the ``_drop`` list) models an ABSENT
    field. To represent a truly absent key, pass it through ``_drop``.
    """
    drop = over.pop("_drop", [])
    hit: Dict[str, Any] = {
        "objectID": "42",
        "title": "A story about compilers",
        "url": "https://example.com/story",
        "author": "pg",
        "created_at_i": 1704067200,  # 2024-01-01T00:00:00+00:00 UTC
        "points": 128,
        "num_comments": 64,
    }
    hit.update(over)
    for k in drop:
        hit.pop(k, None)
    return hit


def _body(hits: List[Dict[str, Any]]) -> bytes:
    return json.dumps({"hits": hits}).encode("utf-8")


def _page_responses(make_result, pages_hits: Dict[int, List[Dict[str, Any]]]):
    """Map ALGOLIA%page -> a 200 FetchResult carrying the given hits, per page index."""
    return {
        ALGOLIA % page: make_result(content=_body(hits), status=200)
        for page, hits in pages_hits.items()
    }


# --------------------------------------------------------------------------- #
# Happy path & field mapping
# --------------------------------------------------------------------------- #
def test_single_hit_maps_every_field(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {0: [_hit()]}))
    items = fetch_items(client, {}, pages=1)

    assert len(items) == 1
    item = items[0]
    assert item == {
        "guid": "hn-42",
        "raw_url": "https://example.com/story",
        "title": "A story about compilers",
        "author": "pg",
        "published_at": "2024-01-01T00:00:00+00:00",
        "points": 128,
        "comments": 64,
        "extra": {
            "discussion_url": "https://news.ycombinator.com/item?id=42",
            "surface": "hn",
        },
    }


def test_discussion_url_uses_item_url_template(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(make_result, {0: [_hit(objectID="9001")]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["extra"]["discussion_url"] == "https://news.ycombinator.com/item?id=9001"
    assert item["guid"] == "hn-9001"


def test_two_page_happy_path_concatenates_in_order(fake_client, make_result):
    p0 = [_hit(objectID="1", title="one"), _hit(objectID="2", title="two")]
    p1 = [_hit(objectID="3", title="three")]
    client = fake_client(responses=_page_responses(make_result, {0: p0, 1: p1}))

    items = fetch_items(client, {}, pages=2)

    assert [i["guid"] for i in items] == ["hn-1", "hn-2", "hn-3"]
    assert [i["title"] for i in items] == ["one", "two", "three"]
    # 0-indexed pagination: page 0 then page 1.
    assert client.requested == [ALGOLIA % 0, ALGOLIA % 1]


def test_source_row_is_ignored(fake_client, make_result):
    # A wildly different source_row must not change the requested URL or output.
    client = fake_client(responses=_page_responses(make_result, {0: [_hit()]}))
    items = fetch_items(client, {"url": "ignored", "mode": "whatever", "slug": "x"}, pages=1)
    assert len(items) == 1
    assert client.requested == [ALGOLIA % 0]


def test_fetch_is_unconditional(fake_client, make_result):
    # The ranked front page must always be fetched fresh: the module passes
    # conditional=False so it is never served from the etag/if-modified cache.
    # PoliteClient.fetch defaults conditional=True, so a regression that dropped
    # the explicit flag would be invisible to every other test — pin it here.
    seen_conditional: List[bool] = []

    class Recorder(fake_client):
        def fetch(self, url: str, conditional: bool = True):
            seen_conditional.append(conditional)
            return super().fetch(url, conditional)

    client = Recorder(responses=_page_responses(make_result, {0: [_hit()], 1: []}))
    fetch_items(client, {}, pages=2)
    assert seen_conditional == [False, False]


# --------------------------------------------------------------------------- #
# raw_url fallback (self-posts)
# --------------------------------------------------------------------------- #
def test_self_post_without_url_falls_back_to_discussion(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(
            make_result, {0: [_hit(objectID="777", _drop=["url"])]}
        )
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] == "https://news.ycombinator.com/item?id=777"
    assert item["raw_url"] == item["extra"]["discussion_url"]


def test_empty_string_url_falls_back_to_discussion(fake_client, make_result):
    # hit['url'] present but falsy ("") → `hit.get("url") or discussion` picks discussion.
    client = fake_client(
        responses=_page_responses(make_result, {0: [_hit(objectID="5", url="")]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] == "https://news.ycombinator.com/item?id=5"


def test_present_url_is_preferred_over_discussion(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(
            make_result, {0: [_hit(url="https://real.example/post")]}
        )
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] == "https://real.example/post"
    assert item["raw_url"] != item["extra"]["discussion_url"]


# --------------------------------------------------------------------------- #
# Skip filter — missing objectID or title
# --------------------------------------------------------------------------- #
def test_hit_missing_objectid_is_dropped(fake_client, make_result):
    good = _hit(objectID="good", title="kept")
    bad = _hit(title="has title but no id", _drop=["objectID"])
    client = fake_client(responses=_page_responses(make_result, {0: [bad, good]}))
    items = fetch_items(client, {}, pages=1)
    assert [i["guid"] for i in items] == ["hn-good"]


def test_hit_missing_title_is_dropped(fake_client, make_result):
    good = _hit(objectID="good", title="kept")
    bad = _hit(objectID="hasid", _drop=["title"])
    client = fake_client(responses=_page_responses(make_result, {0: [good, bad]}))
    items = fetch_items(client, {}, pages=1)
    assert [i["guid"] for i in items] == ["hn-good"]


@pytest.mark.parametrize("blank_title", ["", "   ", "\t\n ", "\xa0"])
def test_whitespace_only_title_is_dropped(fake_client, make_result, blank_title):
    bad = _hit(objectID="ws", title=blank_title)
    client = fake_client(responses=_page_responses(make_result, {0: [bad]}))
    assert fetch_items(client, {}, pages=1) == []


def test_none_title_is_dropped(fake_client, make_result):
    bad = _hit(objectID="nt", title=None)
    client = fake_client(responses=_page_responses(make_result, {0: [bad]}))
    assert fetch_items(client, {}, pages=1) == []


def test_title_is_stripped(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(
            make_result, {0: [_hit(title="   Padded Title \n")]}
        )
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["title"] == "Padded Title"


def test_empty_objectid_string_is_dropped(fake_client, make_result):
    # objectID == "" is falsy → `not object_id` drops it even though the key exists.
    bad = _hit(objectID="", title="present")
    client = fake_client(responses=_page_responses(make_result, {0: [bad]}))
    assert fetch_items(client, {}, pages=1) == []


# --------------------------------------------------------------------------- #
# published_at / epoch conversion
# --------------------------------------------------------------------------- #
def test_created_at_i_absent_yields_none_published(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(
            make_result, {0: [_hit(_drop=["created_at_i"])]}
        )
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["published_at"] is None


def test_created_at_i_zero_yields_none_published(fake_client, make_result):
    # `if created` treats epoch 0 as falsy → published_at None (documented behavior).
    client = fake_client(
        responses=_page_responses(make_result, {0: [_hit(created_at_i=0)]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["published_at"] is None


@pytest.mark.parametrize(
    "epoch,expected_iso",
    [
        (1704067200, "2024-01-01T00:00:00+00:00"),
        (1751630400, "2025-07-04T12:00:00+00:00"),
        (1, "1970-01-01T00:00:01+00:00"),
    ],
)
def test_epoch_to_iso_is_utc(fake_client, make_result, epoch, expected_iso):
    client = fake_client(
        responses=_page_responses(make_result, {0: [_hit(created_at_i=epoch)]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["published_at"] == expected_iso


# --------------------------------------------------------------------------- #
# Optional numeric / author fields default to None when absent
# --------------------------------------------------------------------------- #
def test_absent_optional_fields_become_none(fake_client, make_result):
    hit = _hit(_drop=["author", "points", "num_comments"])
    client = fake_client(responses=_page_responses(make_result, {0: [hit]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["author"] is None
    assert item["points"] is None
    assert item["comments"] is None


def test_points_and_comments_zero_are_preserved(fake_client, make_result):
    hit = _hit(points=0, num_comments=0)
    client = fake_client(responses=_page_responses(make_result, {0: [hit]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["points"] == 0
    assert item["comments"] == 0


# --------------------------------------------------------------------------- #
# hits container edge cases
# --------------------------------------------------------------------------- #
def test_empty_hits_list_returns_empty(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {0: []}))
    assert fetch_items(client, {}, pages=1) == []


def test_missing_hits_key_returns_empty(fake_client, make_result):
    client = fake_client(
        responses={ALGOLIA % 0: make_result(content=b'{"nope": 1}', status=200)}
    )
    assert fetch_items(client, {}, pages=1) == []


# --------------------------------------------------------------------------- #
# Pagination shape & clamping
# --------------------------------------------------------------------------- #
@pytest.mark.property
@pytest.mark.parametrize(
    "pages,expected_pages",
    [
        (1, [0]),
        (2, [0, 1]),
        (3, [0, 1, 2]),
        (0, [0]),   # max(1, 0) → single page 0
        (-1, [0]),  # max(1, -1) → single page 0
        (-5, [0]),
    ],
)
def test_pagination_is_zero_indexed_and_clamped(
    fake_client, make_result, pages, expected_pages
):
    # Every page returns an empty (but valid) body so no page 500s out.
    responses = _page_responses(make_result, {p: [] for p in expected_pages})
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, pages=pages)
    assert items == []
    assert client.requested == [ALGOLIA % p for p in expected_pages]


def test_default_pages_is_two(fake_client, make_result):
    responses = _page_responses(make_result, {0: [], 1: []})
    client = fake_client(responses=responses)
    fetch_items(client, {})  # rely on default pages=2
    assert client.requested == [ALGOLIA % 0, ALGOLIA % 1]


# --------------------------------------------------------------------------- #
# Failure handling — a bad page aborts the whole fetch (no partial return)
# --------------------------------------------------------------------------- #
def test_second_page_failure_aborts_and_discards_page_one(fake_client, make_result):
    responses = {
        ALGOLIA % 0: make_result(content=_body([_hit(objectID="1")]), status=200),
        ALGOLIA % 1: make_result(content=None, status=500, error=None),
    }
    client = fake_client(responses=responses)
    with pytest.raises(RuntimeError) as exc:
        fetch_items(client, {}, pages=2)
    assert "HTTP 500" in str(exc.value)
    # Both pages were requested, but nothing is returned (exception, not partial list).
    assert client.requested == [ALGOLIA % 0, ALGOLIA % 1]


def test_non_200_status_without_error_uses_http_message(fake_client, make_result):
    client = fake_client(
        responses={ALGOLIA % 0: make_result(content=None, status=503, error=None)}
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 503$"):
        fetch_items(client, {}, pages=1)


def test_error_message_is_preferred_over_http_status(fake_client, make_result):
    client = fake_client(
        responses={
            ALGOLIA % 0: make_result(
                content=None, status=500, error="upstream exploded"
            )
        }
    )
    with pytest.raises(RuntimeError, match="upstream exploded"):
        fetch_items(client, {}, pages=1)


def test_empty_body_with_200_status_raises(fake_client, make_result):
    # status is 200 but content is empty → `not res.content` triggers; error None → "HTTP 200".
    client = fake_client(
        responses={ALGOLIA % 0: make_result(content=b"", status=200, error=None)}
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 200$"):
        fetch_items(client, {}, pages=1)


def test_transport_error_status_zero_raises(fake_client, make_result):
    # status 0 (transport error) with an error string → error string surfaces.
    client = fake_client(
        responses={
            ALGOLIA % 0: make_result(content=None, status=0, error="ConnectError: boom")
        }
    )
    with pytest.raises(RuntimeError, match="ConnectError: boom"):
        fetch_items(client, {}, pages=1)


def test_first_page_failure_makes_no_further_requests(fake_client, make_result):
    # If page 0 fails we abort immediately — page 1 is never requested.
    responses = {
        ALGOLIA % 0: make_result(content=None, status=503, error=None),
        ALGOLIA % 1: make_result(content=_body([_hit()]), status=200),
    }
    client = fake_client(responses=responses)
    with pytest.raises(RuntimeError):
        fetch_items(client, {}, pages=2)
    assert client.requested == [ALGOLIA % 0]


# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #
def test_module_constants_shape():
    assert ALGOLIA % 0 == (
        "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50&page=0"
    )
    assert ITEM_URL % "12345" == "https://news.ycombinator.com/item?id=12345"


# --------------------------------------------------------------------------- #
# Live smoke — real Algolia front page. Deselected by default (-m 'not live')
# and additionally env-guarded so `-m live` on a box without SIGNAL_LIVE skips.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_front_page_returns_items(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real HN Algolia API")

    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    client.host_intervals = {}
    client.default_interval = 0.0
    try:
        items = fetch_items(client, {}, pages=1)
    finally:
        client.close()

    assert len(items) > 0
    for item in items:
        assert item["guid"].startswith("hn-")
        assert item["raw_url"]
        assert item["title"]
