"""Unit + integration tests for ``signalpipe.ingest.arxiv``.

``arxiv.fetch_items`` is a thin wrapper over ``rss.fetch_feed_items``: it tags
every normalized item with ``extra['surface'] = 'arxiv'`` and then drops entries
whose title carries arXiv's *replacement* suffix (e.g. ``(arXiv:2501.1v2 [cs.AI]
UPDATED)``). Its only real logic is therefore:

1. the ``_UPDATED_RE`` anchor (matches only the exact end-of-title arXiv
   replacement shape, never a bare word ``UPDATED`` somewhere in the title), and
2. the in-place mutation of the ``extra`` dict that ``rss`` produced — the
   ``'surface'`` key is added *beside* the ``'bozo'`` key ``rss`` already set.

Two complementary strategies keep this hermetic:

* **isolation** — monkeypatch ``arxiv.rss_mod.fetch_feed_items`` to return canned
  item dicts, so the tag/filter logic is exercised with zero I/O; and
* **end-to-end** — drive the real ``rss`` + ``feedparser`` path with a
  ``FakePoliteClient`` serving a recorded arXiv RDF/RSS fixture.

No test performs real network I/O.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pytest

from signalpipe.ingest import arxiv
from signalpipe.ingest.arxiv import _UPDATED_RE, fetch_items

ARXIV_URL = "https://rss.arxiv.org/rss/cs.AI"


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
def _item(title: str, extra: Optional[Dict[str, Any]] = None, **over: Any) -> Dict[str, Any]:
    """Build a normalized item dict shaped like ``rss.fetch_feed_items`` output.

    ``extra`` defaults to ``{'bozo': False}`` to mirror what ``rss`` always sets;
    pass an explicit dict to model a pre-existing ``surface`` or extra fields.
    """
    item: Dict[str, Any] = {
        "guid": "http://arxiv.org/abs/0000.0000v1",
        "raw_url": "http://arxiv.org/abs/0000.0000v1",
        "title": title,
        "author": None,
        "published_at": None,
        "points": None,
        "comments": None,
        "extra": {"bozo": False} if extra is None else extra,
    }
    item.update(over)
    return item


def _install_rss(
    monkeypatch,
    items: List[Dict[str, Any]],
    recorder: Optional[List[Tuple[Any, Any]]] = None,
) -> None:
    """Replace ``arxiv.rss_mod.fetch_feed_items`` with a stub returning ``items``.

    If ``recorder`` is given, each call appends ``(client, source_row)`` to it so a
    test can assert the wrapper delegated its arguments verbatim.
    """

    def _fake(client, source_row):
        if recorder is not None:
            recorder.append((client, source_row))
        return items

    monkeypatch.setattr(arxiv.rss_mod, "fetch_feed_items", _fake)


# --------------------------------------------------------------------------- #
# _UPDATED_RE — the replacement-suffix anchor (pure, no I/O)
# --------------------------------------------------------------------------- #
_MATCHES = [
    "Improved bounds. (arXiv:2507.01222v2 [cs.LG] UPDATED)",
    "Foo (arXiv:2501.12345v2 [cs.AI] UPDATED)",
    "Foo (arXiv:2501.1v3 UPDATED)",                      # no category bracket
    "X (arXiv:2501.1v10 [math.CO] UPDATED)",
    "Multi (arXiv:2501.1v2 [cs.AI cs.LG] UPDATED)",       # multi-token bracket
    "Trailing space (arXiv:2501.1v3 UPDATED)   ",         # \s*$ absorbs trailing ws
    "Two spaces (arXiv:2501.1v3  UPDATED)",               # \s+ matches >1 space
    "Older technique revisited. (arXiv:2506.09999v3 UPDATED)",
]

_NON_MATCHES = [
    "UPDATED benchmark for X",                            # bare word, no suffix
    "Title (arXiv:2501.12345v1 [cs.AI])",                 # suffix but no UPDATED
    "An UPDATED Benchmark. (arXiv:2501.1v1 [cs.AI])",     # UPDATED mid-title only
    "Foo (arXiv:2501.1v3 UPDATED) trailing text",         # not anchored to end
    "Foo (arXiv:2501.1v3 updated)",                       # lowercase, case-sensitive
    "(arXiv: 2501.1v3 UPDATED)",                          # whitespace right after colon
    "Just a normal paper title",
    "",
]


@pytest.mark.parametrize("title", _MATCHES)
def test_updated_re_matches_replacement_suffixes(title):
    assert _UPDATED_RE.search(title) is not None


@pytest.mark.parametrize("title", _NON_MATCHES)
def test_updated_re_ignores_non_replacements(title):
    assert _UPDATED_RE.search(title) is None


def test_updated_re_is_compiled_and_end_anchored():
    assert isinstance(_UPDATED_RE, re.Pattern)
    # The pattern is anchored to end-of-string; a valid suffix followed by text
    # must not match (already covered above, re-asserted here as the contract).
    assert _UPDATED_RE.search("A (arXiv:1v2 [cs.AI] UPDATED) and more") is None


# --------------------------------------------------------------------------- #
# fetch_items — tagging (isolated: rss stubbed)
# --------------------------------------------------------------------------- #
def test_fetch_items_tags_surface_and_preserves_bozo(monkeypatch):
    _install_rss(monkeypatch, [_item("A normal paper", extra={"bozo": False})])
    (item,) = fetch_items(object(), {"url": ARXIV_URL})

    assert item["extra"]["surface"] == "arxiv"
    # The 'bozo' key rss set must survive alongside the new 'surface' key.
    assert item["extra"]["bozo"] is False
    assert set(item["extra"]) == {"bozo", "surface"}


def test_fetch_items_tags_every_surviving_item(monkeypatch):
    _install_rss(
        monkeypatch,
        [
            _item("First paper", extra={"bozo": True}),
            _item("Second paper", extra={"bozo": False}),
        ],
    )
    items = fetch_items(object(), {"url": ARXIV_URL})
    assert [i["title"] for i in items] == ["First paper", "Second paper"]
    assert all(i["extra"]["surface"] == "arxiv" for i in items)
    # bozo values are per-item and untouched by the tagging.
    assert [i["extra"]["bozo"] for i in items] == [True, False]


def test_fetch_items_overwrites_preexisting_surface(monkeypatch):
    # If rss somehow set a different surface, arxiv forces it to 'arxiv'.
    _install_rss(
        monkeypatch, [_item("Paper", extra={"bozo": False, "surface": "rss"})]
    )
    (item,) = fetch_items(object(), {"url": ARXIV_URL})
    assert item["extra"]["surface"] == "arxiv"


def test_fetch_items_mutates_extra_dict_in_place(monkeypatch):
    # The wrapper mutates the *same* dict rss built (aliasing), it does not copy.
    extra = {"bozo": False}
    canned = _item("A normal paper", extra=extra)
    _install_rss(monkeypatch, [canned])

    (item,) = fetch_items(object(), {"url": ARXIV_URL})

    assert item is canned                     # same object flows out
    assert item["extra"] is extra             # same extra dict, mutated in place
    assert extra["surface"] == "arxiv"


# --------------------------------------------------------------------------- #
# fetch_items — UPDATED filtering (isolated: rss stubbed)
# --------------------------------------------------------------------------- #
def test_fetch_items_filters_updated_after_tagging(monkeypatch):
    normal = _item("A brand new result. (arXiv:2507.01111v1 [cs.AI])")
    updated = _item("Improved bounds. (arXiv:2507.01222v2 [cs.LG] UPDATED)")
    _install_rss(monkeypatch, [normal, updated])

    items = fetch_items(object(), {"url": ARXIV_URL})

    assert len(items) == 1
    assert items[0] is normal
    assert items[0]["extra"]["surface"] == "arxiv"


def test_fetch_items_keeps_word_updated_that_is_not_the_suffix(monkeypatch):
    # A title containing UPDATED mid-title (not the arXiv replacement suffix) stays.
    kept = _item("An UPDATED Benchmark for Reasoning. (arXiv:2507.02222v1 [cs.AI])")
    dropped = _item("Older technique. (arXiv:2506.09999v3 UPDATED)")
    _install_rss(monkeypatch, [kept, dropped])

    items = fetch_items(object(), {"url": ARXIV_URL})
    assert [i["title"] for i in items] == [kept["title"]]


def test_fetch_items_all_updated_returns_empty(monkeypatch):
    _install_rss(
        monkeypatch,
        [
            _item("A. (arXiv:1.1v2 [cs.AI] UPDATED)"),
            _item("B. (arXiv:2.2v3 UPDATED)"),
        ],
    )
    assert fetch_items(object(), {"url": ARXIV_URL}) == []


def test_fetch_items_empty_feed_returns_empty(monkeypatch):
    _install_rss(monkeypatch, [])
    assert fetch_items(object(), {"url": ARXIV_URL}) == []


def test_fetch_items_preserves_order_of_survivors(monkeypatch):
    _install_rss(
        monkeypatch,
        [
            _item("one"),
            _item("drop. (arXiv:9.9v2 [cs.AI] UPDATED)"),
            _item("two"),
            _item("three"),
        ],
    )
    items = fetch_items(object(), {"url": ARXIV_URL})
    assert [i["title"] for i in items] == ["one", "two", "three"]


# --------------------------------------------------------------------------- #
# fetch_items — delegation to rss.fetch_feed_items
# --------------------------------------------------------------------------- #
def test_fetch_items_passes_client_and_source_row_through(monkeypatch):
    calls: List[Tuple[Any, Any]] = []
    _install_rss(monkeypatch, [_item("Paper")], recorder=calls)

    client = object()
    source_row = {"url": ARXIV_URL, "slug": "arxiv-cs-ai", "mode": None}
    fetch_items(client, source_row)

    assert len(calls) == 1
    got_client, got_row = calls[0]
    assert got_client is client
    assert got_row is source_row


# --------------------------------------------------------------------------- #
# End-to-end through the REAL rss + feedparser path (fake client, recorded feed)
# --------------------------------------------------------------------------- #
def test_fetch_items_end_to_end_with_fixture(fake_client, make_result, load_bytes):
    body = load_bytes("arxiv_cs_ai.rss")
    client = fake_client(responses={ARXIV_URL: make_result(content=body, status=200)})

    items = fetch_items(client, {"url": ARXIV_URL})

    # rss fetched exactly the source URL once.
    assert client.requested == [ARXIV_URL]

    # Two of the four fixture entries are UPDATED replacements and get dropped;
    # the two genuine announcements survive (one of which merely *contains* the
    # word UPDATED in its human-readable title).
    assert [i["title"] for i in items] == [
        "A brand new result on transformers. (arXiv:2507.01111v1 [cs.AI])",
        "An UPDATED Benchmark for Reasoning. (arXiv:2507.02222v1 [cs.AI])",
    ]
    for it in items:
        assert it["extra"]["surface"] == "arxiv"
        # bozo (set by rss) coexists with the surface tag.
        assert "bozo" in it["extra"]
        assert it["extra"]["bozo"] is False
        assert it["raw_url"].startswith("http://arxiv.org/abs/")


def test_fetch_items_end_to_end_unchanged_returns_empty(fake_client, make_result):
    # 304 / body-hash short-circuit → rss returns [] → arxiv returns [].
    client = fake_client(
        responses={ARXIV_URL: make_result(status=304, unchanged=True)}
    )
    assert fetch_items(client, {"url": ARXIV_URL}) == []


def test_fetch_items_end_to_end_http_error_raises(fake_client, make_result):
    client = fake_client(
        responses={ARXIV_URL: make_result(content=None, status=503, error=None)}
    )
    with pytest.raises(RuntimeError, match=r"^HTTP 503$"):
        fetch_items(client, {"url": ARXIV_URL})


# --------------------------------------------------------------------------- #
# Live smoke — real arXiv RSS. Deselected by default (-m 'not live') and
# additionally env-guarded so `-m live` on a box without SIGNAL_LIVE skips.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_arxiv_feed_tags_surface(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real arXiv RSS feed")

    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    try:
        items = fetch_items(client, {"url": ARXIV_URL})
    finally:
        client.close()

    assert len(items) > 0
    for item in items:
        assert item["title"]
        assert item["extra"]["surface"] == "arxiv"
        assert _UPDATED_RE.search(item["title"]) is None
