"""Tests for ``signalpipe.ingest.reddit`` — the three-mode subreddit fetcher.

reddit is the ONLY ingest fetcher that reads ``source_row`` fields (mode/slug/url),
so these tests exercise both the slug parser and every mode branch of
``fetch_items``: the oauth stub RuntimeError, the rss delegation (with url rewrite),
and the public_json listing parse + normalization.

All network is faked. Unit tests use a local recording client that returns a canned
``FetchResult``; the rss branch is unit-tested by monkeypatching the lazily-imported
``signalpipe.ingest.rss.fetch_feed_items`` and integration-tested end-to-end through a
real ``PoliteClient`` (httpx MockTransport) + real feedparser.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Callable, List, Optional, Tuple

import pytest

from signalpipe.ingest import reddit


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
class RecordingClient:
    """Minimal PoliteClient stand-in that records ``(url, conditional)`` per call.

    Unlike the shared ``fake_client`` it captures the ``conditional`` kwarg, which
    matters here: public_json fetches with ``conditional=False`` and that is a branch
    worth asserting.
    """

    def __init__(self, result: Any):
        self._result = result
        self.calls: List[Tuple[str, bool]] = []

    def fetch(self, url: str, conditional: bool = True):
        self.calls.append((url, conditional))
        return self._result() if callable(self._result) else self._result


def _listing(children: List[dict]) -> dict:
    return {"kind": "Listing", "data": {"children": children}}


# --------------------------------------------------------------------------- #
# _sub_from_source — slug parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "slug,expected",
    [
        ("reddit-python", "python"),
        ("reddit-r-place", "r-place"),
        ("programming", "programming"),          # no marker -> unchanged
        ("reddit-", ""),                          # marker with empty remainder
        ("my-reddit-sub", "sub"),                # marker not anchored at start
        ("reddit-a-reddit-b", "a-reddit-b"),     # splits on FIRST marker only
    ],
)
def test_sub_from_source(slug, expected):
    assert reddit._sub_from_source({"slug": slug}) == expected


# --------------------------------------------------------------------------- #
# Mode resolution: precedence + case-fold
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("row_mode", ["oauth", "OAUTH", "Oauth"])
def test_oauth_mode_from_row_raises(row_mode):
    with pytest.raises(RuntimeError) as ei:
        reddit.fetch_items(object(), {"mode": row_mode, "slug": "reddit-x", "url": "u"})
    msg = str(ei.value)
    assert "reddit.com/prefs/apps" in msg
    assert "oauth" in msg


def test_oauth_mode_from_arg_when_row_mode_none():
    # row mode is None -> the arg ('oauth') supplies the mode.
    with pytest.raises(RuntimeError) as ei:
        reddit.fetch_items(
            object(), {"mode": None, "slug": "reddit-x", "url": "u"}, mode="oauth"
        )
    assert "reddit.com/prefs/apps" in str(ei.value)


def test_row_mode_overrides_arg(monkeypatch):
    # row mode 'RSS' beats arg 'oauth': resolves to rss, NOT the oauth RuntimeError.
    from signalpipe.ingest import rss as rss_mod

    monkeypatch.setattr(rss_mod, "fetch_feed_items", lambda client, row: ["ok"])
    out = reddit.fetch_items(
        object(), {"mode": "RSS", "slug": "reddit-py", "url": "u"}, mode="oauth"
    )
    assert out == ["ok"]


def test_both_none_defaults_to_public_json(make_result):
    # arg None + row None -> the 'public_json' literal fallback: fetch IS called.
    client = RecordingClient(make_result(content=b'{"data":{"children":[]}}'))
    items = reddit.fetch_items(
        client, {"mode": None, "url": "u", "slug": "reddit-x"}, mode=None
    )
    assert items == []
    assert client.calls == [("u", False)]


# --------------------------------------------------------------------------- #
# rss mode — url rewrite + delegation (unit, monkeypatched feed)
# --------------------------------------------------------------------------- #
def test_rss_mode_rewrites_url_and_delegates(monkeypatch):
    from signalpipe.ingest import rss as rss_mod

    captured: dict = {}
    sentinel = [{"guid": "sentinel"}]

    def fake_feed(client, row):
        captured["client"] = client
        captured["row"] = row
        return sentinel

    monkeypatch.setattr(rss_mod, "fetch_feed_items", fake_feed)

    client = object()
    source_row = {"mode": "rss", "slug": "reddit-python", "url": "https://original/feed"}
    result = reddit.fetch_items(client, source_row)

    assert result is sentinel                      # passthrough of delegate return
    assert captured["client"] is client            # same client handed to the feed path
    assert captured["row"]["url"] == "https://www.reddit.com/r/python/top/.rss?t=day"
    assert captured["row"]["url"].endswith("/r/python/top/.rss?t=day")
    # the row passed to rss is a COPY — original source_row is untouched.
    assert source_row["url"] == "https://original/feed"


def test_rss_mode_url_uses_parsed_sub(monkeypatch):
    from signalpipe.ingest import rss as rss_mod

    captured: dict = {}
    monkeypatch.setattr(
        rss_mod, "fetch_feed_items", lambda c, row: captured.update(row=row) or []
    )
    # slug WITHOUT the reddit- prefix is used verbatim as the subreddit.
    reddit.fetch_items(object(), {"mode": "rss", "slug": "MachineLearning", "url": "x"})
    assert (
        captured["row"]["url"]
        == "https://www.reddit.com/r/MachineLearning/top/.rss?t=day"
    )


# --------------------------------------------------------------------------- #
# public_json — happy path parse + normalization
# --------------------------------------------------------------------------- #
def test_public_json_happy_path(make_result, load_json):
    payload = load_json("reddit_top.json")
    url = "https://www.reddit.com/r/python/top.json?t=day&limit=25"
    client = RecordingClient(make_result(content=json.dumps(payload), status=200))
    source_row = {"mode": None, "slug": "reddit-python", "url": url}

    items = reddit.fetch_items(client, source_row)

    # exactly one fetch, conditional disabled for the JSON endpoint.
    assert client.calls == [(url, False)]
    assert len(items) == 2

    self_post, link_post = items
    assert self_post == {
        "guid": "t3_self1",
        "raw_url": "https://www.reddit.com/r/python/comments/self1/a_self_post/",
        "title": "A self post that needs stripping",       # stripped
        "author": "alice",
        "published_at": "2023-11-14T22:13:20+00:00",         # created_utc -> ISO UTC
        "points": 321,                                        # <- score
        "comments": 45,                                       # <- num_comments
        "extra": {
            "discussion_url": "https://www.reddit.com/r/python/comments/self1/a_self_post/",
            "subreddit": "python",
            "upvote_ratio": 0.98,
            "surface": "reddit",
        },
    }

    # link post: raw_url is the external url; discussion_url is still the permalink.
    assert link_post["guid"] == "t3_link1"
    assert link_post["raw_url"] == "https://example.com/article"
    assert (
        link_post["extra"]["discussion_url"]
        == "https://www.reddit.com/r/python/comments/link1/external/"
    )
    assert link_post["points"] == 210
    assert link_post["comments"] == 12
    assert link_post["published_at"] == "2023-11-14T08:20:00+00:00"


@pytest.mark.parametrize(
    "data,expected_raw",
    [
        # self post -> permalink regardless of the url field
        (
            {"name": "t3_a", "title": "self", "is_self": True,
             "permalink": "/p/", "url": "https://ext/x"},
            "https://www.reddit.com/p/",
        ),
        # link post -> the url field
        (
            {"name": "t3_b", "title": "link", "is_self": False,
             "permalink": "/p/", "url": "https://ext/x"},
            "https://ext/x",
        ),
        # link post with no url -> falls back to permalink
        (
            {"name": "t3_c", "title": "link no url", "is_self": False, "permalink": "/p/"},
            "https://www.reddit.com/p/",
        ),
        # is_self missing -> falsy -> treated like a link post
        (
            {"name": "t3_d", "title": "no is_self", "permalink": "/p/", "url": "https://ext/y"},
            "https://ext/y",
        ),
    ],
)
def test_raw_url_resolution(make_result, data, expected_raw):
    client = RecordingClient(make_result(content=json.dumps(_listing([{"data": data}]))))
    items = reddit.fetch_items(client, {"mode": None, "url": "u", "slug": "reddit-x"})
    assert items[0]["raw_url"] == expected_raw


def test_skips_malformed_children_and_handles_defaults(make_result):
    payload = _listing(
        [
            {"data": {"title": "no name", "permalink": "/x"}},          # missing name -> skip
            {"data": {"name": "t3_nt", "title": "   ", "permalink": "/y"}},  # blank title -> skip
            {"data": {}},                                               # empty data -> skip
            {},                                                         # child.get('data') None -> skip
            {"data": {"name": "t3_ok", "title": "Kept", "permalink": "/r/py/comments/ok/"}},
        ]
    )
    client = RecordingClient(make_result(content=json.dumps(payload)))
    items = reddit.fetch_items(client, {"mode": None, "url": "u", "slug": "reddit-py"})

    assert [it["guid"] for it in items] == ["t3_ok"]
    kept = items[0]
    assert kept["published_at"] is None       # no created_utc
    assert kept["points"] is None             # no score
    assert kept["comments"] is None           # no num_comments
    assert kept["author"] is None             # no author
    assert kept["raw_url"] == "https://www.reddit.com/r/py/comments/ok/"
    assert kept["extra"] == {
        "discussion_url": "https://www.reddit.com/r/py/comments/ok/",
        "subreddit": None,
        "upvote_ratio": None,
        "surface": "reddit",
    }


def test_created_utc_zero_is_treated_as_missing(make_result):
    # created_utc == 0 is falsy, so the `if created` guard yields published_at None.
    payload = _listing([{"data": {"name": "t3_z", "title": "Epoch zero", "created_utc": 0}}])
    client = RecordingClient(make_result(content=json.dumps(payload)))
    items = reddit.fetch_items(client, {"mode": None, "url": "u", "slug": "reddit-x"})
    assert items[0]["published_at"] is None


def test_permalink_missing_defaults_to_bare_host(make_result):
    # No permalink field -> '%s' % '' -> the bare host, and (self) raw_url matches.
    payload = _listing([{"data": {"name": "t3_np", "title": "No permalink", "is_self": True}}])
    client = RecordingClient(make_result(content=json.dumps(payload)))
    items = reddit.fetch_items(client, {"mode": None, "url": "u", "slug": "reddit-x"})
    assert items[0]["raw_url"] == "https://www.reddit.com"
    assert items[0]["extra"]["discussion_url"] == "https://www.reddit.com"


@pytest.mark.parametrize(
    "body",
    [b"{}", b'{"data": null}', b'{"data": {"children": []}}'],
)
def test_empty_or_missing_listing_yields_no_items(make_result, body):
    client = RecordingClient(make_result(content=body))
    items = reddit.fetch_items(client, {"mode": None, "url": "u", "slug": "reddit-x"})
    assert items == []


@pytest.mark.parametrize(
    "kw,expected_msg",
    [
        (dict(status=500, content=b"x", error=None), "HTTP 500"),        # non-200, no error string
        (dict(status=503, content=b"", error="upstream down"), "upstream down"),  # error wins
        (dict(status=200, content=b"", error=None), "HTTP 200"),          # 200 but empty body
        (dict(status=200, content=b"", error="empty body"), "empty body"),
    ],
)
def test_public_json_error_results_raise(make_result, kw, expected_msg):
    client = RecordingClient(make_result(**kw))
    with pytest.raises(RuntimeError) as ei:
        reddit.fetch_items(client, {"mode": None, "url": "u", "slug": "reddit-x"})
    assert str(ei.value) == expected_msg


# --------------------------------------------------------------------------- #
# sqlite3.Row support — reddit is the only fetcher that reads row fields
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_public_json_accepts_sqlite_row(conn, seed, make_result):
    src_id = seed.source(
        slug="reddit-python", mode=None, url="https://www.reddit.com/r/python/top.json"
    )
    row = conn.execute("SELECT * FROM sources WHERE id=?", (src_id,)).fetchone()
    assert isinstance(row, sqlite3.Row)

    payload = {"data": {"children": [{"data": {"name": "t3_r", "title": "From a Row"}}]}}
    client = RecordingClient(make_result(content=json.dumps(payload)))
    items = reddit.fetch_items(client, row)

    assert client.calls == [("https://www.reddit.com/r/python/top.json", False)]
    assert items[0]["guid"] == "t3_r"


@pytest.mark.integration
def test_rss_mode_accepts_sqlite_row(conn, seed, monkeypatch):
    from signalpipe.ingest import rss as rss_mod

    captured: dict = {}

    def fake_feed(client, row):
        captured["row"] = row
        return []

    monkeypatch.setattr(rss_mod, "fetch_feed_items", fake_feed)

    src_id = seed.source(slug="reddit-python", mode="rss", url="https://original/feed")
    row = conn.execute("SELECT * FROM sources WHERE id=?", (src_id,)).fetchone()
    assert isinstance(row, sqlite3.Row)

    reddit.fetch_items(object(), row)
    # dict(sqlite3.Row) is copied + rewritten; original DB url is not mutated.
    assert captured["row"]["url"] == "https://www.reddit.com/r/python/top/.rss?t=day"


# --------------------------------------------------------------------------- #
# rss mode — end-to-end through a real PoliteClient + real feedparser
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_rss_mode_end_to_end_real_feedparser(polite_client_factory, load_bytes):
    pytest.importorskip("feedparser")
    import httpx

    atom = load_bytes("reddit_top.rss")
    seen: dict = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200, content=atom, headers={"Content-Type": "application/atom+xml"}
        )

    client = polite_client_factory(handler)
    source_row = {
        "mode": "rss",
        "slug": "reddit-python",
        "url": "https://placeholder.invalid/feed",
    }

    items = reddit.fetch_items(client, source_row)

    # the url was rewritten to the reddit .rss endpoint before the real fetch.
    assert seen["url"] == "https://www.reddit.com/r/python/top/.rss?t=day"
    # blank-title entry is skipped by the generic feed parser.
    assert [it["guid"] for it in items] == ["t3_self1", "t3_link1"]

    first = items[0]
    assert first["raw_url"] == "https://www.reddit.com/r/python/comments/self1/a_self_post/"
    assert first["title"] == "A self post via the Atom feed"
    assert first["author"] == "/u/alice"
    assert first["published_at"] == "2023-11-14T22:13:20+00:00"
    assert first["points"] is None
    assert first["comments"] is None
    assert first["extra"] == {"bozo": False}

    assert items[1]["raw_url"] == "https://example.com/article"
    assert items[1]["published_at"] == "2023-11-14T08:20:00+00:00"


# --------------------------------------------------------------------------- #
# Live smoke — real reddit.com (deselected by default; env-gated)
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_public_json_smoke(cfg, conn):
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live: set SIGNAL_LIVE=1 to hit real reddit.com (7s host interval)")
    from signalpipe.ingest.fetch_http import PoliteClient

    with PoliteClient(cfg, conn) as client:
        source_row = {
            "mode": "public_json",
            "slug": "reddit-python",
            "url": "https://www.reddit.com/r/python/top.json?t=day&limit=5",
        }
        items = reddit.fetch_items(client, source_row)

    assert isinstance(items, list)
    assert all("guid" in it and "raw_url" in it for it in items)
