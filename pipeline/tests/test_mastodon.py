"""Unit tests for ``signalpipe.ingest.mastodon`` — the Mastodon trending-links parser.

Fully hermetic: ``fetch_items`` reaches the network only through the injected
``PoliteClient.fetch``, which we replace with a ``FakePoliteClient`` keyed on the module
constant ``TRENDS_URL % instance``. ``source_row`` is unused by this module (the
``instances`` arg drives every request), so we pass a bare ``{}``. Per-instance failures
are written to ``sys.stderr`` (not injectable) so we assert them via ``capsys``.

Behaviour derived directly from the source, NOT the docstring:
* ``guid == raw_url == url`` (the trending URL is the identity), url/title ``.strip()``ed.
* ``points = _to_int(history[0]['uses'])``; ``extra.accounts = _to_int(history[0]['accounts'])``.
* ``comments`` is always ``None``; ``published_at`` passes through raw.
* ``history = entry.get('history') or [{}]`` guards ``None``/empty-list -> counts ``None``.
* Cross-instance dedup keeps the item with strictly-greater ``uses`` (ties keep the first).
* Error aggregation: non-200 / empty body / bad-JSON / non-list payload are recorded and
  skipped. If *every* instance failed (``errors and not by_guid``) it raises RuntimeError;
  otherwise it prints each error to stderr and returns the survivors.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest

from signalpipe.ingest.mastodon import TRENDS_URL, _to_int, fetch_items


# --------------------------------------------------------------------------- #
# Local builders — construct trending-link payloads inline for hermeticity.
# --------------------------------------------------------------------------- #
def _link(**over: Any) -> Dict[str, Any]:
    """A single ``/api/v1/trends/links`` entry with sane defaults; override via kwargs.

    Pass a key list via ``_drop`` to model a truly ABSENT field (vs a present-but-None
    value passed directly).
    """
    drop = over.pop("_drop", [])
    entry: Dict[str, Any] = {
        "url": "https://example.com/a",
        "title": "A trending article",
        "author_name": "Jane",
        "published_at": "2026-07-01T00:00:00Z",
        "provider_name": "Example News",
        "history": [{"uses": "9", "accounts": "4"}],
    }
    entry.update(over)
    for k in drop:
        entry.pop(k, None)
    return entry


def _body(entries: List[Dict[str, Any]]) -> bytes:
    return json.dumps(entries).encode("utf-8")


def _responses(make_result, bodies: Dict[str, Any]) -> Dict[str, Any]:
    """Map ``instance -> FetchResult`` keyed by ``TRENDS_URL % instance``.

    A value that is a ``list`` is JSON-encoded into a 200 result; ``bytes`` is wrapped
    verbatim; anything else (already a ``FetchResult``) is passed straight through.
    """
    out: Dict[str, Any] = {}
    for instance, value in bodies.items():
        if isinstance(value, list):
            res = make_result(content=_body(value), status=200)
        elif isinstance(value, (bytes, str)):
            res = make_result(content=value, status=200)
        else:
            res = value
        out[TRENDS_URL % instance] = res
    return out


# --------------------------------------------------------------------------- #
# _to_int — tolerant string->int coercion
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("42", 42),
        (42, 42),
        (0, 0),
        ("0", 0),
        ("-5", -5),
        (" 7 ", 7),          # int() strips surrounding whitespace
        (1.9, 1),            # float truncates toward zero
        (None, None),        # TypeError
        ("abc", None),       # ValueError
        ("", None),          # ValueError
        ("1.0", None),       # ValueError (decimal string)
        ("1e3", None),       # ValueError (scientific string)
        ([], None),          # TypeError (non-numeric type)
        ({}, None),          # TypeError
    ],
)
def test_to_int_tolerance(value, expected):
    assert _to_int(value) == expected


@pytest.mark.property
def test_to_int_property_roundtrip():
    """Ints pass through unchanged; pure-decimal strings parse to their int value."""
    pytest.importorskip("hypothesis")
    from hypothesis import given, strategies as st

    @given(st.integers())
    def _check(n):
        assert _to_int(n) == n
        assert _to_int(str(n)) == n

    _check()


# --------------------------------------------------------------------------- #
# Happy path & field mapping
# --------------------------------------------------------------------------- #
def test_single_instance_maps_every_field(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, {"mastodon.social": [_link()]})
    )
    items = fetch_items(client, {}, instances=["mastodon.social"])

    assert len(items) == 1
    assert items[0] == {
        "guid": "https://example.com/a",
        "raw_url": "https://example.com/a",
        "title": "A trending article",
        "author": "Jane",
        "published_at": "2026-07-01T00:00:00Z",
        "points": 9,
        "comments": None,
        "extra": {
            "surface": "mastodon",
            "instance": "mastodon.social",
            "provider": "Example News",
            "accounts": 4,
        },
    }
    # Exactly one request, to the trends endpoint for that instance.
    assert client.requested == [TRENDS_URL % "mastodon.social"]


def test_default_instance_when_none(fake_client, make_result):
    """``instances=None`` falls back to the single default ``mastodon.social``."""
    client = fake_client(
        responses=_responses(make_result, {"mastodon.social": [_link()]})
    )
    items = fetch_items(client, {}, instances=None)

    assert client.requested == [TRENDS_URL % "mastodon.social"]
    assert [it["extra"]["instance"] for it in items] == ["mastodon.social"]


def test_default_instance_when_empty_list(fake_client, make_result):
    """A falsy (empty) ``instances`` also falls back to the default."""
    client = fake_client(
        responses=_responses(make_result, {"mastodon.social": [_link()]})
    )
    fetch_items(client, {}, instances=[])
    assert client.requested == [TRENDS_URL % "mastodon.social"]


def test_url_and_title_are_stripped(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result,
            {"mastodon.social": [_link(url="  https://ex.com/x  ", title="  Padded  ")]},
        )
    )
    items = fetch_items(client, {}, instances=["mastodon.social"])
    assert items[0]["guid"] == "https://ex.com/x"
    assert items[0]["raw_url"] == "https://ex.com/x"
    assert items[0]["title"] == "Padded"


def test_missing_author_name_becomes_none(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(_drop=["author_name"])]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["author"] is None


def test_empty_author_name_becomes_none(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(author_name="")]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["author"] is None


def test_missing_provider_is_none(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(_drop=["provider_name"])]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["extra"]["provider"] is None


def test_published_at_passed_through_raw_none(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(_drop=["published_at"])]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["published_at"] is None


# --------------------------------------------------------------------------- #
# history guard -> None counts
# --------------------------------------------------------------------------- #
def test_empty_history_yields_none_counts(fake_client, make_result):
    """``history=[]`` -> ``[{}]`` guard -> uses/accounts both ``None``."""
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(history=[])]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["points"] is None
    assert items[0]["extra"]["accounts"] is None


def test_none_history_yields_none_counts(fake_client, make_result):
    """``history=None`` -> ``[{}]`` guard -> uses/accounts both ``None``."""
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(history=None)]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["points"] is None
    assert items[0]["extra"]["accounts"] is None


def test_missing_history_key_yields_none_counts(fake_client, make_result):
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(_drop=["history"])]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["points"] is None
    assert items[0]["extra"]["accounts"] is None


def test_history_bucket_missing_uses_yields_none_points(fake_client, make_result):
    """A present bucket lacking ``uses`` -> ``history[0].get('uses')`` is None -> None."""
    client = fake_client(
        responses=_responses(make_result, {"m": [_link(history=[{"accounts": "3"}])]})
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["points"] is None
    assert items[0]["extra"]["accounts"] == 3


def test_junk_uses_string_yields_none_points(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result, {"m": [_link(history=[{"uses": "lots", "accounts": "x"}])]}
        )
    )
    items = fetch_items(client, {}, instances=["m"])
    assert items[0]["points"] is None
    assert items[0]["extra"]["accounts"] is None


# --------------------------------------------------------------------------- #
# Entry filtering
# --------------------------------------------------------------------------- #
def test_entry_missing_url_skipped(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result, {"m": [_link(_drop=["url"]), _link(url="https://keep.me/1")]}
        )
    )
    items = fetch_items(client, {}, instances=["m"])
    assert [it["guid"] for it in items] == ["https://keep.me/1"]


def test_entry_missing_title_skipped(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result,
            {"m": [_link(url="https://drop.me/2", _drop=["title"]),
                   _link(url="https://keep.me/2")]},
        )
    )
    items = fetch_items(client, {}, instances=["m"])
    assert [it["guid"] for it in items] == ["https://keep.me/2"]


def test_entry_blank_url_or_title_skipped(fake_client, make_result):
    client = fake_client(
        responses=_responses(
            make_result,
            {"m": [_link(url="   "), _link(title="   "), _link(url="https://ok.me/3")]},
        )
    )
    items = fetch_items(client, {}, instances=["m"])
    assert [it["guid"] for it in items] == ["https://ok.me/3"]


def test_empty_list_payload_returns_empty(fake_client, make_result):
    """Valid empty array: no entries, no errors -> ``[]`` with no raise/print."""
    client = fake_client(responses=_responses(make_result, {"m": []}))
    assert fetch_items(client, {}, instances=["m"]) == []


# --------------------------------------------------------------------------- #
# Cross-instance dedup
# --------------------------------------------------------------------------- #
def test_cross_instance_dedup_keeps_higher_uses(fake_client, make_result):
    """Same URL on two instances: the strictly-greater ``uses`` wins, with its instance."""
    url = "https://shared.example/story"
    responses = _responses(
        make_result,
        {
            "a.example": [_link(url=url, history=[{"uses": "5", "accounts": "2"}])],
            "b.example": [_link(url=url, history=[{"uses": "9", "accounts": "7"}])],
        },
    )
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["a.example", "b.example"])

    assert len(items) == 1
    assert items[0]["guid"] == url
    assert items[0]["points"] == 9
    assert items[0]["extra"]["instance"] == "b.example"
    assert items[0]["extra"]["accounts"] == 7


def test_cross_instance_dedup_lower_second_does_not_overwrite(fake_client, make_result):
    """When the second instance has a lower count, the first (higher) is retained."""
    url = "https://shared.example/story"
    responses = _responses(
        make_result,
        {
            "a.example": [_link(url=url, history=[{"uses": "9"}])],
            "b.example": [_link(url=url, history=[{"uses": "5"}])],
        },
    )
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["a.example", "b.example"])

    assert len(items) == 1
    assert items[0]["points"] == 9
    assert items[0]["extra"]["instance"] == "a.example"


def test_cross_instance_tie_keeps_first(fake_client, make_result):
    """Equal ``uses`` is NOT strictly greater, so the first instance is kept."""
    url = "https://shared.example/story"
    responses = _responses(
        make_result,
        {
            "a.example": [_link(url=url, history=[{"uses": "7"}])],
            "b.example": [_link(url=url, history=[{"uses": "7"}])],
        },
    )
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["a.example", "b.example"])

    assert len(items) == 1
    assert items[0]["extra"]["instance"] == "a.example"


def test_none_points_does_not_overwrite_existing(fake_client, make_result):
    """A later None-count duplicate ((None or 0)=0) never displaces a real prior count."""
    url = "https://shared.example/story"
    responses = _responses(
        make_result,
        {
            "a.example": [_link(url=url, history=[{"uses": "4"}])],
            "b.example": [_link(url=url, history=[])],  # uses -> None
        },
    )
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["a.example", "b.example"])

    assert len(items) == 1
    assert items[0]["points"] == 4
    assert items[0]["extra"]["instance"] == "a.example"


def test_distinct_urls_across_instances_all_kept(fake_client, make_result):
    responses = _responses(
        make_result,
        {
            "a.example": [_link(url="https://a.example/one")],
            "b.example": [_link(url="https://b.example/two")],
        },
    )
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["a.example", "b.example"])
    assert sorted(it["guid"] for it in items) == [
        "https://a.example/one",
        "https://b.example/two",
    ]
    # One request per instance, in order.
    assert client.requested == [
        TRENDS_URL % "a.example",
        TRENDS_URL % "b.example",
    ]


# --------------------------------------------------------------------------- #
# Error aggregation: partial failure -> survivors + stderr
# --------------------------------------------------------------------------- #
def test_partial_failure_returns_survivors_and_warns(fake_client, make_result, capsys):
    responses = {
        TRENDS_URL % "bad.example": make_result(status=500),
        TRENDS_URL % "good.example": make_result(
            content=_body([_link(url="https://good.example/item")]), status=200
        ),
    }
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["bad.example", "good.example"])

    assert [it["guid"] for it in items] == ["https://good.example/item"]
    err = capsys.readouterr().err
    assert "mastodon: instance failed (bad.example: HTTP 500)" in err
    # The healthy instance is not reported as a failure.
    assert "good.example" not in err


def test_error_message_prefers_result_error_text(fake_client, make_result, capsys):
    """When ``res.error`` is set it is used verbatim (over the ``HTTP <status>`` fallback)."""
    responses = {
        TRENDS_URL % "down.example": make_result(status=0, error="timeout"),
        TRENDS_URL % "good.example": make_result(
            content=_body([_link(url="https://good.example/item")]), status=200
        ),
    }
    client = fake_client(responses=responses)
    fetch_items(client, {}, instances=["down.example", "good.example"])
    err = capsys.readouterr().err
    assert "mastodon: instance failed (down.example: timeout)" in err


def test_empty_body_with_200_is_an_error(fake_client, make_result, capsys):
    """status 200 but empty content trips ``not res.content`` -> 'HTTP 200' error."""
    responses = {
        TRENDS_URL % "empty.example": make_result(content=b"", status=200),
        TRENDS_URL % "good.example": make_result(
            content=_body([_link(url="https://good.example/item")]), status=200
        ),
    }
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["empty.example", "good.example"])
    assert [it["guid"] for it in items] == ["https://good.example/item"]
    err = capsys.readouterr().err
    assert "mastodon: instance failed (empty.example: HTTP 200)" in err


def test_bad_json_counted_as_error(fake_client, make_result, capsys):
    responses = {
        TRENDS_URL % "junk.example": make_result(content=b"not json", status=200),
        TRENDS_URL % "good.example": make_result(
            content=_body([_link(url="https://good.example/item")]), status=200
        ),
    }
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["junk.example", "good.example"])
    assert [it["guid"] for it in items] == ["https://good.example/item"]
    err = capsys.readouterr().err
    assert "junk.example: bad JSON" in err


def test_non_list_payload_counted_as_error(fake_client, make_result, capsys):
    """A JSON object (dict), not an array -> 'unexpected payload shape'."""
    responses = {
        TRENDS_URL % "obj.example": make_result(content=b"{}", status=200),
        TRENDS_URL % "good.example": make_result(
            content=_body([_link(url="https://good.example/item")]), status=200
        ),
    }
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["obj.example", "good.example"])
    assert [it["guid"] for it in items] == ["https://good.example/item"]
    err = capsys.readouterr().err
    assert "obj.example: unexpected payload shape" in err


def test_json_null_payload_counted_as_error(fake_client, make_result, capsys):
    """``b'null'`` decodes to None (truthy body, valid JSON) -> not-a-list branch."""
    responses = {
        TRENDS_URL % "null.example": make_result(content=b"null", status=200),
        TRENDS_URL % "good.example": make_result(
            content=_body([_link(url="https://good.example/item")]), status=200
        ),
    }
    client = fake_client(responses=responses)
    items = fetch_items(client, {}, instances=["null.example", "good.example"])
    assert [it["guid"] for it in items] == ["https://good.example/item"]
    assert "null.example: unexpected payload shape" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Error aggregation: total failure -> RuntimeError (nothing printed)
# --------------------------------------------------------------------------- #
def test_all_instances_fail_raises(fake_client, make_result, capsys):
    responses = {
        TRENDS_URL % "a.example": make_result(status=500),
        TRENDS_URL % "b.example": make_result(status=503),
    }
    client = fake_client(responses=responses)
    with pytest.raises(RuntimeError) as excinfo:
        fetch_items(client, {}, instances=["a.example", "b.example"])

    msg = str(excinfo.value)
    assert msg.startswith("all mastodon instances failed:")
    assert "a.example: HTTP 500" in msg
    assert "b.example: HTTP 503" in msg
    # The raise pre-empts the stderr print loop.
    assert capsys.readouterr().err == ""


def test_single_instance_failure_raises(fake_client, make_result):
    """One instance, non-200: errors non-empty, by_guid empty -> raise."""
    client = fake_client(
        responses={TRENDS_URL % "only.example": make_result(status=404)}
    )
    with pytest.raises(RuntimeError, match="all mastodon instances failed"):
        fetch_items(client, {}, instances=["only.example"])


def test_success_with_no_qualifying_entries_and_a_failure_raises(
    fake_client, make_result
):
    """errors non-empty AND by_guid empty (all entries filtered out) -> raise.

    The 200 instance returns only entries missing a title, so it contributes nothing;
    combined with a failing instance this trips the all-failed guard.
    """
    responses = {
        TRENDS_URL % "thin.example": make_result(
            content=_body([_link(_drop=["title"])]), status=200
        ),
        TRENDS_URL % "down.example": make_result(status=500),
    }
    client = fake_client(responses=responses)
    with pytest.raises(RuntimeError, match="all mastodon instances failed"):
        fetch_items(client, {}, instances=["thin.example", "down.example"])


# --------------------------------------------------------------------------- #
# Live smoke test — real network; opt-in only.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_trends_smoke(cfg):  # pragma: no cover - opt-in network path
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test; set SIGNAL_LIVE=1 to hit real mastodon.social")

    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, None)  # real host, no cache
    try:
        items = fetch_items(client, {}, instances=["mastodon.social"])
    finally:
        client.close()
    assert isinstance(items, list)
    for it in items:
        assert it["guid"] == it["raw_url"]
        assert it["extra"]["surface"] == "mastodon"
