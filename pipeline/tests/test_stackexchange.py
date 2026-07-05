"""Unit tests for ``signalpipe.ingest.stackexchange`` — the Stack Exchange 2.3
hot-questions ingest parser.

Everything is hermetic. ``fetch_items`` reaches the network only through the injected
``PoliteClient.fetch``, which we replace with a ``FakePoliteClient`` keyed on the module
constant ``HOT_URL`` (``source_row`` is entirely unused by this module). Epoch->ISO
conversion is deterministic: the code feeds an explicit ``creation_date`` into
``datetime.fromtimestamp(..., utc)`` — never wall-clock — so expected ISO strings are pinned.

DOCSTRING/CODE MISMATCH (a real latent bug): the module header claims a ``backoff`` field
is honored by "returning early", but the code only logs a stderr warning and keeps parsing
the same response. These tests assert the ACTUAL behavior (items are still returned).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest

from signalpipe.ingest.stackexchange import HOT_URL, fetch_items


# --------------------------------------------------------------------------- #
# Local builders — construct SE API response bodies inline for hermeticity.
# --------------------------------------------------------------------------- #
def _q(**over: Any) -> Dict[str, Any]:
    """A single hot-question object with sensible defaults; override via kwargs.

    Pass keys through the ``_drop`` list to model a truly ABSENT field (vs. a key
    present but falsy, which you model by passing ``key=None``/``0``/``""``).
    """
    drop = over.pop("_drop", [])
    q: Dict[str, Any] = {
        "question_id": 78901234,
        "title": "How do I parse JSON in Python?",
        "link": "https://stackoverflow.com/q/78901234",
        "score": 15,
        "answer_count": 3,
        "view_count": 1200,
        "is_answered": True,
        "creation_date": 1751630400,  # 2025-07-04T12:00:00+00:00 UTC
        "tags": ["python", "json"],
        "owner": {"display_name": "Ada"},
    }
    q.update(over)
    for k in drop:
        q.pop(k, None)
    return q


def _body(items: List[Dict[str, Any]], **top: Any) -> bytes:
    """Serialize an API envelope: ``{"items": [...], **top}`` as JSON bytes.

    ``top`` carries envelope-level fields such as ``backoff``/``quota_remaining``/
    ``quota_max``.
    """
    payload: Dict[str, Any] = {"items": items}
    payload.update(top)
    return json.dumps(payload).encode("utf-8")


def _client(fake_client, make_result, items, status=200, error=None, **top):
    """A ``FakePoliteClient`` keyed on HOT_URL carrying the given items + envelope."""
    content = _body(items, **top) if status == 200 else None
    return fake_client(
        responses={HOT_URL: make_result(content=content, status=status, error=error)}
    )


class _RecordingClient:
    """Minimal PoliteClient stand-in that records the exact ``fetch`` call args.

    ``FakePoliteClient`` only records URLs, so it cannot witness the ``conditional``
    keyword. This captures ``(url, conditional)`` so a test can pin that the SE hot
    feed is fetched UNconditionally (no If-None-Match revalidation).
    """

    def __init__(self, result):
        self._result = result
        self.calls: List[Any] = []

    def fetch(self, url, conditional=True):
        self.calls.append((url, conditional))
        return self._result


# --------------------------------------------------------------------------- #
# Happy path & field mapping
# --------------------------------------------------------------------------- #
def test_single_question_maps_every_field(fake_client, make_result):
    client = _client(fake_client, make_result, [_q()])
    items = fetch_items(client, {})

    assert len(items) == 1
    assert items[0] == {
        "guid": "so-78901234",
        "raw_url": "https://stackoverflow.com/q/78901234",
        "title": "How do I parse JSON in Python?",
        "author": "Ada",
        "published_at": "2025-07-04T12:00:00+00:00",
        "points": 15,
        "comments": 3,
        "extra": {
            "surface": "stackexchange",
            "site": "stackoverflow",
            "answer_count": 3,
            "view_count": 1200,
            "is_answered": True,
            "tags": ["python", "json"],
        },
    }


def test_only_hot_url_is_requested(fake_client, make_result):
    client = _client(fake_client, make_result, [_q()])
    fetch_items(client, {})
    assert client.requested == [HOT_URL]


def test_hot_feed_is_fetched_unconditionally(make_result):
    # The single fetch must pass conditional=False (hot list is never revalidated
    # via a cached ETag; we always want the fresh ranking).
    client = _RecordingClient(make_result(content=_body([_q()]), status=200))
    fetch_items(client, {})
    assert client.calls == [(HOT_URL, False)]


def test_multiple_questions_preserve_order(fake_client, make_result):
    qs = [
        _q(question_id=1, title="one", link="https://so/1"),
        _q(question_id=2, title="two", link="https://so/2"),
        _q(question_id=3, title="three", link="https://so/3"),
    ]
    client = _client(fake_client, make_result, qs)
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["so-1", "so-2", "so-3"]
    assert [i["title"] for i in items] == ["one", "two", "three"]


def test_guid_uses_question_id(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(question_id=42)])
    (item,) = fetch_items(client, {})
    assert item["guid"] == "so-42"


def test_source_row_is_ignored(fake_client, make_result):
    # A wildly different source_row must not change the requested URL or output.
    client = _client(fake_client, make_result, [_q()])
    items = fetch_items(client, {"url": "ignored", "mode": "whatever", "slug": "x"})
    assert len(items) == 1
    assert client.requested == [HOT_URL]


def test_source_row_none_is_accepted(fake_client, make_result):
    # source_row is never dereferenced, so even None is fine.
    client = _client(fake_client, make_result, [_q()])
    items = fetch_items(client, None)
    assert len(items) == 1


# --------------------------------------------------------------------------- #
# Title HTML-unescape (the main pure hazard) + stripping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Foo &amp; Bar", "Foo & Bar"),
        ("&lt;div&gt;", "<div>"),
        ("It&#39;s working", "It's working"),
        ("A &quot;quoted&quot; word", 'A "quoted" word'),
        ("2 &lt; 3 &amp;&amp; 4 &gt; 1", "2 < 3 && 4 > 1"),
        ("caf&eacute;", "café"),
        ("&#x1F600; emoji", "\U0001f600 emoji"),
        ("no entities here", "no entities here"),
    ],
)
def test_title_html_unescape(fake_client, make_result, raw, expected):
    client = _client(fake_client, make_result, [_q(title=raw)])
    (item,) = fetch_items(client, {})
    assert item["title"] == expected


def test_title_outer_whitespace_is_stripped(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(title="   Padded &amp; Title \n")])
    (item,) = fetch_items(client, {})
    assert item["title"] == "Padded & Title"


def test_strip_runs_before_unescape(fake_client, make_result):
    # Order matters: the code strips the RAW title, then unescapes. A trailing
    # `&nbsp;` is literal text at strip time (not stripped) and only becomes a
    # non-breaking space (U+00A0) after unescape → it survives on the tail.
    # If unescape ran first, the U+00A0 would be whitespace and get stripped away
    # to "x". Pinning "x\xa0" nails the actual (strip-then-unescape) ordering.
    client = _client(fake_client, make_result, [_q(title="x&nbsp;")])
    (item,) = fetch_items(client, {})
    assert item["title"] == "x\xa0"


# --------------------------------------------------------------------------- #
# Skip filter — requires question_id AND title AND link
# --------------------------------------------------------------------------- #
def test_missing_question_id_is_dropped(fake_client, make_result):
    good = _q(question_id=7, title="kept", link="https://so/7")
    bad = _q(title="no id", link="https://so/x", _drop=["question_id"])
    client = _client(fake_client, make_result, [bad, good])
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["so-7"]


def test_missing_title_is_dropped(fake_client, make_result):
    good = _q(question_id=7, title="kept", link="https://so/7")
    bad = _q(question_id=8, link="https://so/8", _drop=["title"])
    client = _client(fake_client, make_result, [good, bad])
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["so-7"]


def test_missing_link_is_dropped(fake_client, make_result):
    good = _q(question_id=7, title="kept", link="https://so/7")
    bad = _q(question_id=8, title="has title", _drop=["link"])
    client = _client(fake_client, make_result, [good, bad])
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["so-7"]


def test_three_malformed_questions_all_dropped(fake_client, make_result):
    q_no_id = _q(_drop=["question_id"])
    q_no_title = _q(question_id=100, _drop=["title"])
    q_no_link = _q(question_id=101, _drop=["link"])
    client = _client(fake_client, make_result, [q_no_id, q_no_title, q_no_link])
    assert fetch_items(client, {}) == []


def test_zero_question_id_is_dropped(fake_client, make_result):
    # question_id == 0 is falsy → `not qid` drops it even though the key exists.
    client = _client(fake_client, make_result, [_q(question_id=0)])
    assert fetch_items(client, {}) == []


def test_none_question_id_is_dropped(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(question_id=None)])
    assert fetch_items(client, {}) == []


def test_empty_link_is_dropped(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(link="")])
    assert fetch_items(client, {}) == []


def test_none_link_is_dropped(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(link=None)])
    assert fetch_items(client, {}) == []


@pytest.mark.parametrize("blank", ["", "   ", "\t\n ", None])
def test_blank_or_none_title_is_dropped(fake_client, make_result, blank):
    # "" / whitespace strip to "" → falsy; None → `(None or "").strip()` → "".
    client = _client(fake_client, make_result, [_q(title=blank)])
    assert fetch_items(client, {}) == []


def test_entity_only_title_survives(fake_client, make_result):
    # A title that is purely an entity strips to non-empty text after unescape.
    client = _client(fake_client, make_result, [_q(title="&amp;")])
    (item,) = fetch_items(client, {})
    assert item["title"] == "&"


# --------------------------------------------------------------------------- #
# author = (owner or {}).get('display_name') — None-safe
# --------------------------------------------------------------------------- #
def test_owner_absent_yields_none_author(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(_drop=["owner"])])
    (item,) = fetch_items(client, {})
    assert item["author"] is None


def test_owner_none_yields_none_author(fake_client, make_result):
    # owner explicitly null → `(None or {}).get(...)` → None (does not raise).
    client = _client(fake_client, make_result, [_q(owner=None)])
    (item,) = fetch_items(client, {})
    assert item["author"] is None


def test_owner_without_display_name_yields_none_author(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(owner={"user_id": 5})])
    (item,) = fetch_items(client, {})
    assert item["author"] is None


def test_owner_display_name_is_used(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(owner={"display_name": "Grace"})])
    (item,) = fetch_items(client, {})
    assert item["author"] == "Grace"


# --------------------------------------------------------------------------- #
# creation_date epoch -> ISO (None-safe)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "epoch,expected_iso",
    [
        (1751630400, "2025-07-04T12:00:00+00:00"),
        (1704067200, "2024-01-01T00:00:00+00:00"),
        (1, "1970-01-01T00:00:01+00:00"),
    ],
)
def test_creation_date_epoch_to_iso_utc(fake_client, make_result, epoch, expected_iso):
    client = _client(fake_client, make_result, [_q(creation_date=epoch)])
    (item,) = fetch_items(client, {})
    assert item["published_at"] == expected_iso


def test_creation_date_absent_yields_none_published(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(_drop=["creation_date"])])
    (item,) = fetch_items(client, {})
    assert item["published_at"] is None


def test_creation_date_zero_yields_none_published(fake_client, make_result):
    # `if created` treats epoch 0 as falsy → published_at None.
    client = _client(fake_client, make_result, [_q(creation_date=0)])
    (item,) = fetch_items(client, {})
    assert item["published_at"] is None


# --------------------------------------------------------------------------- #
# points / comments / extra passthrough
# --------------------------------------------------------------------------- #
def test_absent_score_and_answer_count_become_none(fake_client, make_result):
    client = _client(
        fake_client, make_result, [_q(_drop=["score", "answer_count"])]
    )
    (item,) = fetch_items(client, {})
    assert item["points"] is None
    assert item["comments"] is None
    # extra.answer_count mirrors the (absent) answer_count → also None.
    assert item["extra"]["answer_count"] is None


def test_zero_score_and_answer_count_preserved(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(score=0, answer_count=0)])
    (item,) = fetch_items(client, {})
    assert item["points"] == 0
    assert item["comments"] == 0
    assert item["extra"]["answer_count"] == 0


def test_comments_equals_answer_count(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(answer_count=9)])
    (item,) = fetch_items(client, {})
    assert item["comments"] == 9
    assert item["extra"]["answer_count"] == 9


def test_extra_optional_fields_default_none_and_tags_empty(fake_client, make_result):
    client = _client(
        fake_client,
        make_result,
        [_q(_drop=["view_count", "is_answered", "tags"])],
    )
    (item,) = fetch_items(client, {})
    extra = item["extra"]
    assert extra["view_count"] is None
    assert extra["is_answered"] is None
    assert extra["tags"] == []  # `q.get("tags", [])` default is []
    assert extra["surface"] == "stackexchange"
    assert extra["site"] == "stackoverflow"


def test_tags_list_passed_through(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(tags=["rust", "async", "tokio"])])
    (item,) = fetch_items(client, {})
    assert item["extra"]["tags"] == ["rust", "async", "tokio"]


def test_is_answered_false_preserved(fake_client, make_result):
    client = _client(fake_client, make_result, [_q(is_answered=False)])
    (item,) = fetch_items(client, {})
    assert item["extra"]["is_answered"] is False


# --------------------------------------------------------------------------- #
# items container edge cases
# --------------------------------------------------------------------------- #
def test_empty_items_list_returns_empty(fake_client, make_result):
    client = _client(fake_client, make_result, [])
    assert fetch_items(client, {}) == []


def test_missing_items_key_returns_empty(fake_client, make_result):
    client = fake_client(
        responses={HOT_URL: make_result(content=b'{"quota_remaining": 5}', status=200)}
    )
    assert fetch_items(client, {}) == []


# --------------------------------------------------------------------------- #
# backoff — DOCSTRING LIES: code logs to stderr but does NOT early-return.
# --------------------------------------------------------------------------- #
def test_backoff_still_returns_items_and_warns(fake_client, make_result, capsys):
    client = _client(
        fake_client,
        make_result,
        [_q(question_id=55, title="kept", link="https://so/55")],
        backoff=5,
        quota_remaining=42,
        quota_max=300,
    )
    items = fetch_items(client, {})

    # The real behavior: parsing continues, items ARE returned (no early return).
    assert [i["guid"] for i in items] == ["so-55"]

    err = capsys.readouterr().err
    assert "stackexchange:" in err
    assert "backoff of 5s" in err
    assert "quota 42/300" in err
    assert "parsing this response and stopping" in err


def test_backoff_zero_prints_no_warning(fake_client, make_result, capsys):
    # `if backoff:` → 0 is falsy, so no stderr warning.
    client = _client(fake_client, make_result, [_q()], backoff=0)
    items = fetch_items(client, {})
    assert len(items) == 1
    assert capsys.readouterr().err == ""


def test_no_backoff_field_prints_no_warning(fake_client, make_result, capsys):
    client = _client(fake_client, make_result, [_q()])
    fetch_items(client, {})
    assert capsys.readouterr().err == ""


def test_backoff_with_multiple_questions_returns_all(fake_client, make_result, capsys):
    qs = [
        _q(question_id=1, title="one", link="https://so/1"),
        _q(question_id=2, title="two", link="https://so/2"),
    ]
    client = _client(
        fake_client, make_result, qs, backoff=10, quota_remaining=0, quota_max=300
    )
    items = fetch_items(client, {})
    assert [i["guid"] for i in items] == ["so-1", "so-2"]
    assert "backoff of 10s" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Failure handling — non-200 / empty body raise RuntimeError
# --------------------------------------------------------------------------- #
def test_non_200_without_error_uses_http_message(fake_client, make_result):
    client = fake_client(
        responses={HOT_URL: make_result(content=None, status=503, error=None)}
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 503$"):
        fetch_items(client, {})


def test_error_message_is_preferred_over_http_status(fake_client, make_result):
    client = fake_client(
        responses={
            HOT_URL: make_result(content=None, status=500, error="upstream exploded")
        }
    )
    with pytest.raises(RuntimeError, match="upstream exploded"):
        fetch_items(client, {})


def test_empty_body_with_200_raises(fake_client, make_result):
    # status 200 but empty content → `not res.content` triggers; error None → "HTTP 200".
    client = fake_client(
        responses={HOT_URL: make_result(content=b"", status=200, error=None)}
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 200$"):
        fetch_items(client, {})


def test_transport_error_status_zero_surfaces_error(fake_client, make_result):
    client = fake_client(
        responses={
            HOT_URL: make_result(content=None, status=0, error="ConnectError: boom")
        }
    )
    with pytest.raises(RuntimeError, match="ConnectError: boom"):
        fetch_items(client, {})


def test_non_200_with_content_still_raises(fake_client, make_result):
    # A 429 that carries a body still fails the `status != 200` guard.
    client = fake_client(
        responses={
            HOT_URL: make_result(content=_body([_q()]), status=429, error=None)
        }
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 429$"):
        fetch_items(client, {})


# --------------------------------------------------------------------------- #
# Module constant
# --------------------------------------------------------------------------- #
def test_hot_url_constant_shape():
    assert HOT_URL == (
        "https://api.stackexchange.com/2.3/questions"
        "?order=desc&sort=hot&site=stackoverflow&pagesize=50"
    )


# --------------------------------------------------------------------------- #
# Live smoke — real api.stackexchange.com (300/day anon quota; run sparingly).
# Deselected by default (-m 'not live') and additionally env-guarded so `-m live`
# on a box without SIGNAL_LIVE skips cleanly.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_hot_questions_returns_items(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real Stack Exchange API")

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
        assert item["guid"].startswith("so-")
        assert item["raw_url"]
        assert item["title"]
        assert item["extra"]["site"] == "stackoverflow"
