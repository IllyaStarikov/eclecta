"""Unit tests for ``signalpipe.ingest.devto`` — the dev.to (Forem) ingest parser.

Hermetic by construction: ``fetch_items`` only touches the network through the injected
``PoliteClient.fetch``, replaced here with a ``FakePoliteClient`` keyed on the single module
constant ``ARTICLES_URL`` (``source_row`` is entirely unused by this module, so pagination
is a single fetch). The response body is a BARE JSON array of article objects — not wrapped
in a ``{"hits": ...}`` envelope like the HN parser — so builders emit ``[...]`` directly.

``_tags`` is the only nontrivial pure logic: it accepts ``tag_list`` as either a real list
(passed through as a copy) or the historical Forem comma-string form (split/trimmed/empties
dropped), falling back to the ``tags`` key and finally ``[]``. Both shapes get focused
parametrized tests plus a hypothesis property test guarded so the suite still runs without it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest

from signalpipe.ingest.devto import ARTICLES_URL, _tags, fetch_items


# --------------------------------------------------------------------------- #
# Local builders — construct Forem /api/articles response bodies inline.
# --------------------------------------------------------------------------- #
def _article(**over: Any) -> Dict[str, Any]:
    """A single dev.to article with sensible defaults; override via kwargs.

    To model a truly ABSENT key, pass its name through the ``_drop`` list. Passing
    ``key=None`` models a present-but-null field (a distinct case for e.g. ``user``).
    """
    drop = over.pop("_drop", [])
    article: Dict[str, Any] = {
        "id": 12345,
        "title": "A story about Rust",
        "url": "https://dev.to/foo/a-story-about-rust",
        "user": {"username": "foo"},
        "published_timestamp": "2026-07-01T10:00:00Z",
        "published_at": "2026-07-01T09:00:00Z",
        "positive_reactions_count": 128,
        "comments_count": 12,
        "tag_list": ["rust", "webdev"],
        "reading_time_minutes": 7,
    }
    article.update(over)
    for k in drop:
        article.pop(k, None)
    return article


def _body(articles: List[Dict[str, Any]]) -> bytes:
    """Encode a list of articles as the bare JSON array the Forem API returns."""
    return json.dumps(articles).encode("utf-8")


def _responses(make_result, articles: List[Dict[str, Any]], status: int = 200):
    """Map ARTICLES_URL -> a FetchResult carrying the given articles."""
    return {ARTICLES_URL: make_result(content=_body(articles), status=status)}


# --------------------------------------------------------------------------- #
# _tags — list passthrough, comma-string parsing, fallback, empty
# --------------------------------------------------------------------------- #
def test_tags_list_passthrough_returns_copy():
    original = ["x", "y", "z"]
    result = _tags({"tag_list": original})
    assert result == ["x", "y", "z"]
    # list(tags) makes a copy — mutating the result must not touch the source.
    assert result is not original
    result.append("mutated")
    assert original == ["x", "y", "z"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a, b ,c", ["a", "b", "c"]),  # historical comma-string form
        ("a,,b, ,c", ["a", "b", "c"]),  # empties (blank between commas) dropped
        (" a , ,b, ", ["a", "b"]),  # leading/trailing padding + blanks
        ("solo", ["solo"]),  # single tag, no comma
        (",,,", []),  # only separators → nothing survives
        ("   ", []),  # whitespace-only → nothing survives
        ("", []),  # empty string is falsy → falls through to []
    ],
)
def test_tags_comma_string_is_split_and_trimmed(raw, expected):
    assert _tags({"tag_list": raw}) == expected


def test_tags_falls_back_to_tags_key_when_tag_list_absent():
    assert _tags({"tags": ["fallback", "two"]}) == ["fallback", "two"]


def test_tags_empty_tag_list_falls_back_to_tags_key():
    # tag_list == [] is falsy → `tag_list or tags` picks the tags key.
    assert _tags({"tag_list": [], "tags": ["kept"]}) == ["kept"]


def test_tags_empty_string_tag_list_falls_back_to_tags_key():
    # tag_list == "" is falsy → falls through to `tags` before the isinstance check.
    assert _tags({"tag_list": "", "tags": ["kept"]}) == ["kept"]


def test_tags_tags_key_can_also_be_a_comma_string():
    # The fallback value is also fed through the isinstance(str) split.
    assert _tags({"tags": "p, q ,r"}) == ["p", "q", "r"]


def test_tags_both_missing_returns_empty_list():
    assert _tags({}) == []


def test_tags_tag_list_preferred_over_tags():
    assert _tags({"tag_list": ["primary"], "tags": ["ignored"]}) == ["primary"]


# --------------------------------------------------------------------------- #
# _tags — property tests (hypothesis; skipped cleanly if the lib is absent)
# --------------------------------------------------------------------------- #
@pytest.mark.property
def test_tags_list_passthrough_property():
    pytest.importorskip("hypothesis")
    from hypothesis import given
    from hypothesis import strategies as st

    @given(st.lists(st.text()))
    def check(lst):
        result = _tags({"tag_list": list(lst)})
        assert result == list(lst)

    check()


@pytest.mark.property
def test_tags_comma_string_roundtrip_property():
    pytest.importorskip("hypothesis")
    from hypothesis import given
    from hypothesis import strategies as st

    # Clean tags: non-empty after strip, no commas. Joining with ", " then splitting
    # on "," and trimming must round-trip back to the original clean list.
    clean_tag = (
        st.text(alphabet=st.characters(blacklist_characters=","), min_size=1)
        .map(lambda s: s.strip())
        .filter(lambda s: s != "")
    )

    @given(st.lists(clean_tag))
    def check(tags):
        joined = ", ".join(tags)
        assert _tags({"tag_list": joined}) == tags

    check()


# --------------------------------------------------------------------------- #
# fetch_items — happy path & full field mapping
# --------------------------------------------------------------------------- #
def test_happy_path_maps_every_field(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article()]))
    items = fetch_items(client, {})

    assert len(items) == 1
    assert items[0] == {
        "guid": "devto-12345",
        "raw_url": "https://dev.to/foo/a-story-about-rust",
        "title": "A story about Rust",
        "author": "foo",
        "published_at": "2026-07-01T10:00:00Z",
        "points": 128,
        "comments": 12,
        "extra": {
            "surface": "devto",
            "tags": ["rust", "webdev"],
            "reading_time_minutes": 7,
        },
    }


def test_guid_uses_devto_prefix_with_raw_id(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article(id=98765)]))
    (item,) = fetch_items(client, {})
    assert item["guid"] == "devto-98765"


def test_multiple_articles_preserve_order(fake_client, make_result):
    a = _article(id=1, title="one")
    b = _article(id=2, title="two")
    c = _article(id=3, title="three")
    client = fake_client(responses=_responses(make_result, [a, b, c]))
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["devto-1", "devto-2", "devto-3"]
    assert [i["title"] for i in items] == ["one", "two", "three"]


def test_title_is_stripped(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article(title="  Padded \n")]))
    (item,) = fetch_items(client, {})
    assert item["title"] == "Padded"


def test_fetch_uses_articles_url_unconditionally(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article()]))
    fetch_items(client, {})
    # Single hardcoded endpoint; no pagination.
    assert client.requested == [ARTICLES_URL]


def test_fetch_is_non_conditional(fake_client, make_result):
    # dev.to "top" must be a fresh GET, not a cache-conditional one that could 304.
    # FakePoliteClient does not record the flag, so subclass it to capture the call.
    calls = []

    class RecordingClient(fake_client):
        def fetch(self, url, conditional=True):
            calls.append((url, conditional))
            return super().fetch(url, conditional)

    client = RecordingClient(responses=_responses(make_result, [_article()]))
    fetch_items(client, {})
    assert calls == [(ARTICLES_URL, False)]


def test_empty_array_returns_empty_list(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, []))
    assert fetch_items(client, {}) == []


# --------------------------------------------------------------------------- #
# fetch_items — required-field skip filter (id AND title AND url all required)
# --------------------------------------------------------------------------- #
def test_article_missing_id_is_skipped(fake_client, make_result):
    good = _article(id=1, title="kept")
    bad = _article(_drop=["id"], title="no id")
    client = fake_client(responses=_responses(make_result, [bad, good]))
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["devto-1"]


def test_article_missing_title_is_skipped(fake_client, make_result):
    good = _article(id=1, title="kept")
    bad = _article(id=2, _drop=["title"])
    client = fake_client(responses=_responses(make_result, [good, bad]))
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["devto-1"]


def test_article_missing_url_is_skipped(fake_client, make_result):
    good = _article(id=1, title="kept")
    bad = _article(id=2, _drop=["url"])
    client = fake_client(responses=_responses(make_result, [good, bad]))
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["devto-1"]


@pytest.mark.parametrize("bad_title", ["", "   ", "\t\n ", None])
def test_blank_or_none_title_is_skipped(fake_client, make_result, bad_title):
    client = fake_client(responses=_responses(make_result, [_article(title=bad_title)]))
    assert fetch_items(client, {}) == []


def test_id_zero_is_skipped(fake_client, make_result):
    # id == 0 is falsy → `not aid` drops it even though the key exists.
    client = fake_client(responses=_responses(make_result, [_article(id=0)]))
    assert fetch_items(client, {}) == []


@pytest.mark.parametrize("bad_url", ["", None])
def test_falsy_url_is_skipped(fake_client, make_result, bad_url):
    client = fake_client(responses=_responses(make_result, [_article(url=bad_url)]))
    assert fetch_items(client, {}) == []


def test_null_id_is_skipped(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article(id=None)]))
    assert fetch_items(client, {}) == []


# --------------------------------------------------------------------------- #
# fetch_items — author (None-safe on missing/null user)
# --------------------------------------------------------------------------- #
def test_author_from_user_username(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article(user={"username": "alice"})]))
    (item,) = fetch_items(client, {})
    assert item["author"] == "alice"


def test_missing_user_yields_none_author(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article(_drop=["user"])]))
    (item,) = fetch_items(client, {})
    assert item["author"] is None


def test_null_user_yields_none_author(fake_client, make_result):
    # user is present but null → `(None or {}).get("username")` → None (no crash).
    client = fake_client(responses=_responses(make_result, [_article(user=None)]))
    (item,) = fetch_items(client, {})
    assert item["author"] is None


def test_user_without_username_yields_none_author(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article(user={"name": "x"})]))
    (item,) = fetch_items(client, {})
    assert item["author"] is None


# --------------------------------------------------------------------------- #
# fetch_items — published_at resolution (timestamp preferred over published_at)
# --------------------------------------------------------------------------- #
def test_published_timestamp_preferred_when_both_present(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article()]))
    (item,) = fetch_items(client, {})
    assert item["published_at"] == "2026-07-01T10:00:00Z"  # the timestamp, not published_at


def test_published_at_used_when_timestamp_absent(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, [_article(_drop=["published_timestamp"])])
    )
    (item,) = fetch_items(client, {})
    assert item["published_at"] == "2026-07-01T09:00:00Z"


def test_published_at_used_when_timestamp_falsy(fake_client, make_result):
    # published_timestamp == "" is falsy → `ts or published_at` picks published_at.
    client = fake_client(responses=_responses(make_result, [_article(published_timestamp="")]))
    (item,) = fetch_items(client, {})
    assert item["published_at"] == "2026-07-01T09:00:00Z"


def test_published_at_none_when_both_absent(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, [_article(_drop=["published_timestamp", "published_at"])])
    )
    (item,) = fetch_items(client, {})
    assert item["published_at"] is None


# --------------------------------------------------------------------------- #
# fetch_items — points / comments / extra
# --------------------------------------------------------------------------- #
def test_points_and_comments_map_from_counts(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result,
            [_article(positive_reactions_count=42, comments_count=9)],
        )
    )
    (item,) = fetch_items(client, {})
    assert item["points"] == 42
    assert item["comments"] == 9


def test_points_and_comments_zero_preserved(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result,
            [_article(positive_reactions_count=0, comments_count=0)],
        )
    )
    (item,) = fetch_items(client, {})
    assert item["points"] == 0
    assert item["comments"] == 0


def test_absent_counts_become_none(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result,
            [_article(_drop=["positive_reactions_count", "comments_count"])],
        )
    )
    (item,) = fetch_items(client, {})
    assert item["points"] is None
    assert item["comments"] is None


def test_extra_carries_surface_tags_and_reading_time(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result,
            [_article(tag_list="python, ml", reading_time_minutes=3)],
        )
    )
    (item,) = fetch_items(client, {})
    assert item["extra"] == {
        "surface": "devto",
        "tags": ["python", "ml"],
        "reading_time_minutes": 3,
    }


def test_absent_reading_time_becomes_none(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, [_article(_drop=["reading_time_minutes"])])
    )
    (item,) = fetch_items(client, {})
    assert item["extra"]["reading_time_minutes"] is None


def test_absent_tags_yield_empty_tag_list(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article(_drop=["tag_list"])]))
    (item,) = fetch_items(client, {})
    assert item["extra"]["tags"] == []


# --------------------------------------------------------------------------- #
# fetch_items — source_row is unused
# --------------------------------------------------------------------------- #
def test_source_row_is_ignored(fake_client, make_result):
    client = fake_client(responses=_responses(make_result, [_article()]))
    weird = {"url": "ignored", "mode": "whatever", "slug": "x", "id": 7}
    items = fetch_items(client, weird)
    # The article is parsed normally; nothing from source_row leaks into output.
    assert len(items) == 1
    assert items[0]["guid"] == "devto-12345"
    assert items[0]["raw_url"] == "https://dev.to/foo/a-story-about-rust"
    assert client.requested == [ARTICLES_URL]


# --------------------------------------------------------------------------- #
# fetch_items — failure handling (non-200 / empty body → RuntimeError)
# --------------------------------------------------------------------------- #
def test_non_200_without_error_uses_http_message(fake_client, make_result):
    client = fake_client(
        responses={ARTICLES_URL: make_result(content=None, status=503, error=None)}
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 503$"):
        fetch_items(client, {})


def test_error_message_preferred_over_http_status(fake_client, make_result):
    client = fake_client(
        responses={ARTICLES_URL: make_result(content=None, status=500, error="upstream exploded")}
    )
    with pytest.raises(RuntimeError, match="upstream exploded"):
        fetch_items(client, {})


def test_empty_body_with_200_raises(fake_client, make_result):
    # status 200 but content empty → `not res.content` triggers; error None → "HTTP 200".
    client = fake_client(responses={ARTICLES_URL: make_result(content=b"", status=200, error=None)})
    with pytest.raises(RuntimeError, match=r"^HTTP 200$"):
        fetch_items(client, {})


def test_none_body_with_200_raises(fake_client, make_result):
    client = fake_client(
        responses={ARTICLES_URL: make_result(content=None, status=200, error=None)}
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 200$"):
        fetch_items(client, {})


def test_transport_error_status_zero_raises(fake_client, make_result):
    client = fake_client(
        responses={ARTICLES_URL: make_result(content=None, status=0, error="ConnectError: boom")}
    )
    with pytest.raises(RuntimeError, match="ConnectError: boom"):
        fetch_items(client, {})


# --------------------------------------------------------------------------- #
# Module constant
# --------------------------------------------------------------------------- #
def test_articles_url_constant():
    assert ARTICLES_URL == "https://dev.to/api/articles?top=1&per_page=50"


# --------------------------------------------------------------------------- #
# Live smoke — real Forem API. Deselected by default (-m 'not live') and further
# env-guarded so `-m live` on a box without SIGNAL_LIVE skips cleanly.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_top_articles(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real dev.to Forem API")

    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    client.host_intervals = {}
    client.default_interval = 0.0
    try:
        items = fetch_items(client, {})
    finally:
        client.close()

    assert len(items) > 0
    for item in items:
        assert item["guid"].startswith("devto-")
        assert item["raw_url"]
        assert item["title"]
