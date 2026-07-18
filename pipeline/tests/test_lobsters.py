"""Unit tests for ``signalpipe.ingest.lobsters`` — the Lobsters /hottest.json parser.

Hermetic: ``fetch_items`` only touches the network through the injected
``PoliteClient.fetch``, replaced here with a ``FakePoliteClient`` keyed on the module
constant ``HOTTEST % page`` (``source_row`` is entirely unused by this module).

Deliberate contrasts with ``hn.py`` that these tests pin down:

* **1-indexed pagination** — ``range(1, max(1, pages) + 1)`` so page numbers start at 1
  (HN is 0-indexed). ``pages <= 0`` clamps to a single page 1.
* **Bare-array root JSON** — the body is a top-level ``[...]`` list of stories, iterated
  directly, not an object with a ``hits`` key.
* **``published_at`` passthrough** — ``story['created_at']`` flows straight through with
  NO parsing/validation, so even malformed or empty values survive verbatim.
* **``author`` defaults to ``''``** (empty string), not ``None``.
* **``extra.tags`` defaults to ``[]``** when the key is absent.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest

from signalpipe.ingest.lobsters import HOTTEST, fetch_items


# --------------------------------------------------------------------------- #
# Local builders — construct Lobsters response bodies inline for hermeticity.
# --------------------------------------------------------------------------- #
def _story(**over: Any) -> Dict[str, Any]:
    """A single Lobsters /hottest.json story with sensible defaults.

    Override any field via kwargs. To model a truly ABSENT key, list it in ``_drop``.
    Passing ``key=None`` models a present-but-null value (distinct behavior for
    ``.get(k, default)`` calls that only fall back when the key is missing).
    """
    drop = over.pop("_drop", [])
    story: Dict[str, Any] = {
        "short_id": "abc123",
        "title": "A story about Lisp",
        "url": "https://example.com/story",
        "comments_url": "https://lobste.rs/s/abc123",
        "submitter_user": "alice",
        "created_at": "2026-07-01T09:30:00.000-05:00",
        "score": 42,
        "comment_count": 7,
        "tags": ["lisp", "programming"],
    }
    story.update(over)
    for k in drop:
        story.pop(k, None)
    return story


def _body(stories: List[Dict[str, Any]]) -> bytes:
    """Encode a list of stories as the bare top-level JSON array Lobsters returns."""
    return json.dumps(stories).encode("utf-8")


def _page_responses(make_result, pages_stories: Dict[int, List[Dict[str, Any]]]):
    """Map HOTTEST%page -> a 200 FetchResult carrying the given stories, per page."""
    return {
        HOTTEST % page: make_result(content=_body(stories), status=200)
        for page, stories in pages_stories.items()
    }


# --------------------------------------------------------------------------- #
# Happy path & full field mapping
# --------------------------------------------------------------------------- #
def test_single_story_maps_every_field(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {1: [_story()]}))
    items = fetch_items(client, {}, pages=1)

    assert len(items) == 1
    assert items[0] == {
        "guid": "lob-abc123",
        "raw_url": "https://example.com/story",
        "title": "A story about Lisp",
        "author": "alice",
        "published_at": "2026-07-01T09:30:00.000-05:00",
        "points": 42,
        "comments": 7,
        "extra": {
            "discussion_url": "https://lobste.rs/s/abc123",
            "tags": ["lisp", "programming"],
            "surface": "lobsters",
        },
    }


def test_guid_uses_short_id(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {1: [_story(short_id="xyz789")]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["guid"] == "lob-xyz789"


def test_discussion_url_is_comments_url_even_when_url_present(fake_client, make_result):
    # extra.discussion_url is always comments_url; raw_url is the article url.
    client = fake_client(
        responses=_page_responses(
            make_result,
            {1: [_story(url="https://a.example/post", comments_url="https://lobste.rs/s/q")]},
        )
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] == "https://a.example/post"
    assert item["extra"]["discussion_url"] == "https://lobste.rs/s/q"


def test_two_page_happy_path_concatenates_in_order(fake_client, make_result):
    p1 = [_story(short_id="1", title="one"), _story(short_id="2", title="two")]
    p2 = [_story(short_id="3", title="three")]
    client = fake_client(responses=_page_responses(make_result, {1: p1, 2: p2}))

    items = fetch_items(client, {}, pages=2)

    assert [i["guid"] for i in items] == ["lob-1", "lob-2", "lob-3"]
    assert [i["title"] for i in items] == ["one", "two", "three"]
    # 1-indexed pagination: page 1 then page 2 (contrast with HN's 0-index).
    assert client.requested == [HOTTEST % 1, HOTTEST % 2]


def test_source_row_is_ignored(fake_client, make_result):
    # A wildly different source_row must not change the requested URL OR any output
    # field: source_row["url"]="ignored" must never leak into raw_url/discussion_url.
    client = fake_client(responses=_page_responses(make_result, {1: [_story()]}))
    items = fetch_items(client, {"url": "ignored", "mode": "whatever", "slug": "x"}, pages=1)
    assert client.requested == [HOTTEST % 1]
    assert items == [
        {
            "guid": "lob-abc123",
            "raw_url": "https://example.com/story",
            "title": "A story about Lisp",
            "author": "alice",
            "published_at": "2026-07-01T09:30:00.000-05:00",
            "points": 42,
            "comments": 7,
            "extra": {
                "discussion_url": "https://lobste.rs/s/abc123",
                "tags": ["lisp", "programming"],
                "surface": "lobsters",
            },
        }
    ]


def test_fetch_is_unconditional(fake_client, make_result):
    # Lobsters fetches must pass conditional=False. A regression to PoliteClient's
    # conditional=True default would let a 304/empty-body response slip in and trip the
    # `not res.content` guard, raising spuriously — so pin the exact flag per page.
    seen: List[bool] = []

    class RecordingClient(fake_client):  # fake_client fixture IS the class object
        def fetch(self, url, conditional=True):
            seen.append(conditional)
            return super().fetch(url, conditional=conditional)

    client = RecordingClient(responses=_page_responses(make_result, {1: [], 2: []}))
    fetch_items(client, {}, pages=2)
    assert seen == [False, False]


# --------------------------------------------------------------------------- #
# raw_url fallback (url -> comments_url)
# --------------------------------------------------------------------------- #
def test_missing_url_falls_back_to_comments_url(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {1: [_story(_drop=["url"])]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] == "https://lobste.rs/s/abc123"
    assert item["raw_url"] == item["extra"]["discussion_url"]


def test_empty_url_falls_back_to_comments_url(fake_client, make_result):
    # url present but falsy ("") → `story.get("url") or comments_url` picks comments_url.
    client = fake_client(responses=_page_responses(make_result, {1: [_story(url="")]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] == "https://lobste.rs/s/abc123"


def test_present_url_is_preferred_over_comments_url(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(make_result, {1: [_story(url="https://real.example/x")]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] == "https://real.example/x"
    # discussion_url still comes from comments_url (the default), independent of url.
    assert item["extra"]["discussion_url"] == "https://lobste.rs/s/abc123"
    assert item["raw_url"] != item["extra"]["discussion_url"]


def test_missing_both_url_and_comments_url_yields_none(fake_client, make_result):
    # A story with neither url nor comments_url is still kept (short_id+title present),
    # but raw_url and discussion_url are both None.
    story = _story(_drop=["url", "comments_url"])
    client = fake_client(responses=_page_responses(make_result, {1: [story]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["raw_url"] is None
    assert item["extra"]["discussion_url"] is None


# --------------------------------------------------------------------------- #
# Skip filter — missing/blank short_id or title
# --------------------------------------------------------------------------- #
def test_story_missing_short_id_is_dropped(fake_client, make_result):
    good = _story(short_id="keep", title="kept")
    bad = _story(_drop=["short_id"], title="has title but no id")
    client = fake_client(responses=_page_responses(make_result, {1: [bad, good]}))
    items = fetch_items(client, {}, pages=1)
    assert [i["guid"] for i in items] == ["lob-keep"]


def test_story_missing_title_is_dropped(fake_client, make_result):
    good = _story(short_id="keep", title="kept")
    bad = _story(short_id="hasid", _drop=["title"])
    client = fake_client(responses=_page_responses(make_result, {1: [good, bad]}))
    items = fetch_items(client, {}, pages=1)
    assert [i["guid"] for i in items] == ["lob-keep"]


@pytest.mark.parametrize("blank_title", ["", "   ", "\t\n "])
def test_whitespace_only_title_is_dropped(fake_client, make_result, blank_title):
    bad = _story(short_id="ws", title=blank_title)
    client = fake_client(responses=_page_responses(make_result, {1: [bad]}))
    assert fetch_items(client, {}, pages=1) == []


def test_none_title_is_dropped(fake_client, make_result):
    bad = _story(short_id="nt", title=None)
    client = fake_client(responses=_page_responses(make_result, {1: [bad]}))
    assert fetch_items(client, {}, pages=1) == []


def test_empty_short_id_is_dropped(fake_client, make_result):
    # short_id == "" is falsy → `not short_id` drops it even though the key exists.
    bad = _story(short_id="", title="present")
    client = fake_client(responses=_page_responses(make_result, {1: [bad]}))
    assert fetch_items(client, {}, pages=1) == []


def test_none_short_id_is_dropped(fake_client, make_result):
    bad = _story(short_id=None, title="present")
    client = fake_client(responses=_page_responses(make_result, {1: [bad]}))
    assert fetch_items(client, {}, pages=1) == []


def test_title_is_stripped(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(make_result, {1: [_story(title="   Padded Title \n")]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["title"] == "Padded Title"


# --------------------------------------------------------------------------- #
# author — defaults to '' (empty string), never None
# --------------------------------------------------------------------------- #
def test_author_defaults_to_empty_string_when_absent(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(make_result, {1: [_story(_drop=["submitter_user"])]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["author"] == ""


@pytest.mark.parametrize("submitter", [None, ""])
def test_author_falsy_becomes_empty_string(fake_client, make_result, submitter):
    client = fake_client(
        responses=_page_responses(make_result, {1: [_story(submitter_user=submitter)]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["author"] == ""


def test_author_present_is_preserved(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(make_result, {1: [_story(submitter_user="bob")]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["author"] == "bob"


# --------------------------------------------------------------------------- #
# published_at — raw passthrough, NO parsing/validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "created",
    [
        "2026-07-01T09:30:00.000-05:00",
        "2026-07-04T12:00:00Z",
        "not-a-date",  # malformed flows straight through untouched
        "2026-13-99T99:99:99",  # nonsense datetime survives verbatim
        "",  # empty string is NOT coerced to None (contrast with hn)
    ],
)
def test_published_at_passes_through_raw(fake_client, make_result, created):
    client = fake_client(responses=_page_responses(make_result, {1: [_story(created_at=created)]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["published_at"] == created


def test_created_at_absent_yields_none_published(fake_client, make_result):
    client = fake_client(
        responses=_page_responses(make_result, {1: [_story(_drop=["created_at"])]})
    )
    (item,) = fetch_items(client, {}, pages=1)
    assert item["published_at"] is None


# --------------------------------------------------------------------------- #
# Optional numeric fields — score/comment_count -> points/comments
# --------------------------------------------------------------------------- #
def test_absent_score_and_comment_count_become_none(fake_client, make_result):
    story = _story(_drop=["score", "comment_count"])
    client = fake_client(responses=_page_responses(make_result, {1: [story]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["points"] is None
    assert item["comments"] is None


def test_zero_score_and_comment_count_are_preserved(fake_client, make_result):
    story = _story(score=0, comment_count=0)
    client = fake_client(responses=_page_responses(make_result, {1: [story]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["points"] == 0
    assert item["comments"] == 0


# --------------------------------------------------------------------------- #
# extra.tags — default [] on absence; present values pass through unchanged
# --------------------------------------------------------------------------- #
def test_tags_absent_defaults_to_empty_list(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {1: [_story(_drop=["tags"])]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["extra"]["tags"] == []


def test_tags_none_passes_through_as_none(fake_client, make_result):
    # key present with None value → `.get("tags", [])` returns None, NOT the [] default.
    client = fake_client(responses=_page_responses(make_result, {1: [_story(tags=None)]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["extra"]["tags"] is None


def test_tags_list_passes_through(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {1: [_story(tags=["go", "rust"])]}))
    (item,) = fetch_items(client, {}, pages=1)
    assert item["extra"]["tags"] == ["go", "rust"]


# --------------------------------------------------------------------------- #
# Root-array container edge cases
# --------------------------------------------------------------------------- #
def test_empty_array_returns_empty(fake_client, make_result):
    client = fake_client(responses=_page_responses(make_result, {1: []}))
    assert fetch_items(client, {}, pages=1) == []


# --------------------------------------------------------------------------- #
# Pagination shape & clamping — 1-indexed (contrast: hn is 0-indexed)
# --------------------------------------------------------------------------- #
@pytest.mark.property
@pytest.mark.parametrize(
    "pages,expected_pages",
    [
        (1, [1]),
        (2, [1, 2]),
        (3, [1, 2, 3]),
        (0, [1]),  # max(1, 0) → single page 1
        (-1, [1]),  # max(1, -1) → single page 1
        (-5, [1]),
    ],
)
def test_pagination_is_one_indexed_and_clamped(fake_client, make_result, pages, expected_pages):
    # Every page returns an empty (but valid) array so no page fails.
    responses = _page_responses(make_result, {p: [] for p in expected_pages})
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, pages=pages)
    assert items == []
    assert client.requested == [HOTTEST % p for p in expected_pages]


def test_default_pages_is_two(fake_client, make_result):
    responses = _page_responses(make_result, {1: [], 2: []})
    client = fake_client(responses=responses)
    fetch_items(client, {})  # rely on default pages=2
    assert client.requested == [HOTTEST % 1, HOTTEST % 2]


# --------------------------------------------------------------------------- #
# Failure handling — a bad page aborts the whole fetch (no partial return)
# --------------------------------------------------------------------------- #
def test_second_page_failure_aborts_and_discards_page_one(fake_client, make_result):
    responses = {
        HOTTEST % 1: make_result(content=_body([_story(short_id="1")]), status=200),
        HOTTEST % 2: make_result(content=None, status=500, error=None),
    }
    client = fake_client(responses=responses)
    with pytest.raises(RuntimeError) as exc:
        fetch_items(client, {}, pages=2)
    assert "HTTP 500" in str(exc.value)
    # Both pages were requested, but nothing is returned (exception, not partial list).
    assert client.requested == [HOTTEST % 1, HOTTEST % 2]


def test_non_200_status_without_error_uses_http_message(fake_client, make_result):
    client = fake_client(responses={HOTTEST % 1: make_result(content=None, status=503, error=None)})
    with pytest.raises(RuntimeError, match=r"^HTTP 503$"):
        fetch_items(client, {}, pages=1)


def test_error_message_is_preferred_over_http_status(fake_client, make_result):
    client = fake_client(
        responses={HOTTEST % 1: make_result(content=None, status=500, error="upstream exploded")}
    )
    with pytest.raises(RuntimeError, match="upstream exploded"):
        fetch_items(client, {}, pages=1)


def test_empty_body_with_200_status_raises(fake_client, make_result):
    # status 200 but empty content → `not res.content` triggers; error None → "HTTP 200".
    client = fake_client(responses={HOTTEST % 1: make_result(content=b"", status=200, error=None)})
    with pytest.raises(RuntimeError, match=r"^HTTP 200$"):
        fetch_items(client, {}, pages=1)


def test_transport_error_status_zero_raises(fake_client, make_result):
    # status 0 (transport error) with an error string → error string surfaces.
    client = fake_client(
        responses={HOTTEST % 1: make_result(content=None, status=0, error="ConnectError: boom")}
    )
    with pytest.raises(RuntimeError, match="ConnectError: boom"):
        fetch_items(client, {}, pages=1)


def test_first_page_failure_makes_no_further_requests(fake_client, make_result):
    # If page 1 fails we abort immediately — page 2 is never requested.
    responses = {
        HOTTEST % 1: make_result(content=None, status=503, error=None),
        HOTTEST % 2: make_result(content=_body([_story()]), status=200),
    }
    client = fake_client(responses=responses)
    with pytest.raises(RuntimeError):
        fetch_items(client, {}, pages=2)
    assert client.requested == [HOTTEST % 1]


# --------------------------------------------------------------------------- #
# Module constant
# --------------------------------------------------------------------------- #
def test_module_constant_shape():
    assert HOTTEST % 1 == "https://lobste.rs/hottest.json?page=1"
    assert HOTTEST % 2 == "https://lobste.rs/hottest.json?page=2"


# --------------------------------------------------------------------------- #
# Live smoke — real lobste.rs /hottest.json. Deselected by default (-m 'not live')
# and additionally env-guarded so `-m live` on a box without SIGNAL_LIVE skips.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_hottest_returns_items(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real lobste.rs API")

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
        assert item["guid"].startswith("lob-")
        assert item["title"]
        assert item["extra"]["surface"] == "lobsters"
