"""Unit/integration tests for ``signalpipe.ingest.sources_misc`` — the two ad-hoc
fetchers with no dedicated module: Hugging Face daily-papers (JSON API) and GitHub
Trending (HTML scrape).

Everything is hermetic: both functions touch the network ONLY through the injected
``PoliteClient.fetch``, which we replace with the conftest ``FakePoliteClient`` keyed on
the module constants ``HF_DAILY`` / ``GH_TRENDING``. ``source_row`` is accepted but never
dereferenced by either function, so it is passed as a throwaway dict and cannot vary the
requested URL — the fake keys on the constant URL.

The GitHub scraper is regex/split-based and tightly coupled to GitHub's exact markup
(``<article class="Box-row"``, ``href="/owner/repo"``, ``<p>`` description, ``N stars
today``). We drive it from a trimmed-real-markup fixture (``sources_misc_github_trending.html``)
for the happy path and from inline markup for the drift / filter / cap edge cases, asserting
the documented "layout drift degrades to zero items, never bad data" contract.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import pytest

from signalpipe.ingest.sources_misc import (
    GH_TRENDING,
    HF_DAILY,
    _GH_STARS_TODAY_RE,
    fetch_github_trending,
    fetch_hf_daily_papers,
)

_MISSING = object()


# --------------------------------------------------------------------------- #
# Local builders / helpers
# --------------------------------------------------------------------------- #
def _hf_row(
    pid: Any = "2401.00001",
    title: Any = "Some Title",
    upvotes: Any = 10,
    published_at: Any = "2026-07-01T00:00:00Z",
) -> Dict[str, Any]:
    """A single HF daily-papers row. Pass ``_MISSING`` for any field to omit its key."""
    paper: Dict[str, Any] = {}
    if pid is not _MISSING:
        paper["id"] = pid
    if title is not _MISSING:
        paper["title"] = title
    if upvotes is not _MISSING:
        paper["upvotes"] = upvotes
    row: Dict[str, Any] = {"paper": paper}
    if published_at is not _MISSING:
        row["publishedAt"] = published_at
    return row


def _hf_body(rows: List[Any]) -> bytes:
    return json.dumps(rows).encode("utf-8")


def _gh_card(
    owner: str = "o",
    repo: str = "r",
    desc: Optional[str] = "A description",
    stars: str = "5 stars today",
    href: Optional[str] = None,
) -> str:
    """Trimmed single trending card. ``href=None`` -> ``/owner/repo``; pass a full path
    (e.g. ``owner/repo/tree/main``) to model a non ``owner/repo`` link. ``desc=None`` omits
    the ``<p>`` entirely."""
    slug = href if href is not None else "%s/%s" % (owner, repo)
    p = ('<p class="col-9">%s</p>' % desc) if desc is not None else ""
    return (
        '<article class="Box-row">'
        '<h2 class="h3"><a href="/%s"><span>%s /</span> %s</a></h2>'
        "%s"
        '<div class="f6"><span>%s</span></div>'
        "</article>"
    ) % (slug, owner, repo, p, stars)


def _gh_html(cards: List[str], header: str = "<html><body>") -> str:
    return header + "".join(cards) + "</body></html>"


class _RecClient:
    """Minimal client that records the exact ``fetch`` args (url + conditional flag)."""

    def __init__(self, result):
        self._result = result
        self.requested: List[str] = []
        self.conditionals: List[bool] = []

    def fetch(self, url, conditional=True):
        self.requested.append(url)
        self.conditionals.append(conditional)
        return self._result


# =========================================================================== #
# HF daily papers
# =========================================================================== #
def test_hf_field_mapping_from_fixture(fake_client, make_result, load_bytes):
    """[integration] Recorded daily_papers JSON -> exact normalized dict for row 0."""
    client = fake_client(
        responses={HF_DAILY: make_result(content=load_bytes("sources_misc_hf_daily.json"))}
    )
    items = fetch_hf_daily_papers(client, {"slug": "hf"})

    assert len(items) == 3
    assert items[0] == {
        "guid": "hf-2506.12345",
        "raw_url": "https://arxiv.org/abs/2506.12345",
        "title": "Scaling Laws for Small Language Models",  # single \n -> space
        "author": None,
        "published_at": "2026-07-03T01:23:45.000Z",  # TOP-level publishedAt, not paper's
        "points": 128,
        "comments": None,
        "extra": {
            "discussion_url": "https://huggingface.co/papers/2506.12345",
            "surface": "hf-papers",
        },
    }


def test_hf_fixture_all_rows_and_points(fake_client, make_result, load_bytes):
    client = fake_client(
        responses={HF_DAILY: make_result(content=load_bytes("sources_misc_hf_daily.json"))}
    )
    items = fetch_hf_daily_papers(client, {})
    assert [i["guid"] for i in items] == ["hf-2506.12345", "hf-2506.67890", "hf-2506.00007"]
    assert [i["points"] for i in items] == [128, 7, 0]  # zero upvotes preserved
    # Row 1 title had surrounding whitespace that .strip() removes.
    assert items[1]["title"] == "Retrieval-Augmented Generation Revisited"


def test_hf_requests_hf_daily_non_conditional(make_result):
    rec = _RecClient(make_result(content=_hf_body([_hf_row(pid="only", title="Only")]), status=200))
    items = fetch_hf_daily_papers(rec, {})
    assert rec.requested == [HF_DAILY]
    assert rec.conditionals == [False]  # conditional=False is hard-wired
    assert [i["guid"] for i in items] == ["hf-only"]  # the recorded body was actually parsed


def test_hf_source_row_is_ignored(fake_client, make_result):
    client = fake_client(
        responses={HF_DAILY: make_result(content=_hf_body([_hf_row()]))}
    )
    items = fetch_hf_daily_papers(client, {"url": "ignored", "mode": "x", "junk": object()})
    assert len(items) == 1
    assert client.requested == [HF_DAILY]


def test_hf_single_newline_becomes_space_but_runs_not_collapsed(fake_client, make_result):
    # HF only does .replace("\n", " ") — it does NOT collapse runs (unlike GH).
    rows = [_hf_row(pid="x", title="Multi\n\nLine")]
    client = fake_client(responses={HF_DAILY: make_result(content=_hf_body(rows))})
    (item,) = fetch_hf_daily_papers(client, {})
    assert item["title"] == "Multi  Line"  # two spaces, not one


def test_hf_title_stripped_before_replace(fake_client, make_result):
    rows = [_hf_row(pid="x", title="   Padded\nTitle  ")]
    client = fake_client(responses={HF_DAILY: make_result(content=_hf_body(rows))})
    (item,) = fetch_hf_daily_papers(client, {})
    assert item["title"] == "Padded Title"


@pytest.mark.parametrize(
    "row",
    [
        _hf_row(pid=_MISSING),          # no paper.id key
        _hf_row(pid=None),              # id present but null
        _hf_row(pid=""),                # empty id -> falsy
        _hf_row(title=_MISSING),        # no paper.title key
        _hf_row(title=None),            # (None or "") -> ""
        _hf_row(title=""),              # empty title
        _hf_row(title="   \n  "),       # whitespace-only -> "" after strip
        {},                              # no "paper" key at all -> {} -> no id
        {"paper": None},                # paper explicitly null -> `or {}` -> {}
    ],
)
def test_hf_skips_incomplete_rows(fake_client, make_result, row):
    client = fake_client(responses={HF_DAILY: make_result(content=_hf_body([row]))})
    assert fetch_hf_daily_papers(client, {}) == []


def test_hf_valid_row_survives_alongside_skipped(fake_client, make_result):
    rows = [_hf_row(pid=_MISSING), _hf_row(pid="keep", title="Kept"), {}]
    client = fake_client(responses={HF_DAILY: make_result(content=_hf_body(rows))})
    items = fetch_hf_daily_papers(client, {})
    assert [i["guid"] for i in items] == ["hf-keep"]


def test_hf_optional_fields_default_none(fake_client, make_result):
    rows = [_hf_row(pid="p", title="T", upvotes=_MISSING, published_at=_MISSING)]
    client = fake_client(responses={HF_DAILY: make_result(content=_hf_body(rows))})
    (item,) = fetch_hf_daily_papers(client, {})
    assert item["author"] is None
    assert item["comments"] is None
    assert item["points"] is None        # upvotes absent -> None
    assert item["published_at"] is None  # publishedAt absent -> None


def test_hf_caps_at_60_rows(fake_client, make_result):
    rows = [_hf_row(pid="p%d" % i, title="T%d" % i) for i in range(70)]
    client = fake_client(responses={HF_DAILY: make_result(content=_hf_body(rows))})
    items = fetch_hf_daily_papers(client, {})
    assert len(items) == 60
    assert items[0]["guid"] == "hf-p0"
    assert items[-1]["guid"] == "hf-p59"  # rows 60..69 never enter the slice


def test_hf_cap_slices_raw_rows_before_filtering(fake_client, make_result):
    # The [:60] slice is applied to RAW rows, so an invalid row inside the first 60
    # reduces the output below 60 (it is not "backfilled" from row 60+).
    rows = [_hf_row(pid=_MISSING)] + [_hf_row(pid="p%d" % i, title="T%d" % i) for i in range(65)]
    client = fake_client(responses={HF_DAILY: make_result(content=_hf_body(rows))})
    items = fetch_hf_daily_papers(client, {})
    assert len(items) == 59  # 60 sliced rows, first one invalid


def test_hf_empty_list_returns_empty(fake_client, make_result):
    client = fake_client(responses={HF_DAILY: make_result(content=b"[]", status=200)})
    assert fetch_hf_daily_papers(client, {}) == []


@pytest.mark.parametrize(
    "status,content,error,match",
    [
        (503, None, "HTTP 503", r"HTTP 503"),
        (500, None, None, r"^HTTP 500$"),
        (0, None, "ConnectError: boom", r"ConnectError: boom"),
        (500, None, "upstream exploded", r"upstream exploded"),
        (200, b"", None, r"^HTTP 200$"),   # 200 but empty body -> not res.content
        (200, None, None, r"^HTTP 200$"),  # 200 but null body
    ],
)
def test_hf_non_200_or_empty_raises(fake_client, make_result, status, content, error, match):
    client = fake_client(
        responses={HF_DAILY: make_result(content=content, status=status, error=error)}
    )
    with pytest.raises(RuntimeError, match=match):
        fetch_hf_daily_papers(client, {})


# =========================================================================== #
# GitHub trending — happy path from recorded markup
# =========================================================================== #
def test_gh_field_mapping_from_fixture(fake_client, make_result, load_bytes):
    """[integration] Trimmed real trending markup -> exact dict for the first card."""
    html = load_bytes("sources_misc_github_trending.html")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {"slug": "gh"})

    assert len(items) == 3
    assert items[0] == {
        "guid": "ghtrend-torvalds/linux",
        "raw_url": "https://github.com/torvalds/linux",
        "title": "torvalds/linux — Linux kernel source tree",
        "author": "torvalds",
        "published_at": None,
        "points": 1234,  # "1,234 stars today" -> commas stripped
        "comments": None,
        "extra": {"surface": "github-trending"},
    }


def test_gh_repos_authors_and_stars_from_fixture(fake_client, make_result, load_bytes):
    html = load_bytes("sources_misc_github_trending.html")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {})
    assert [i["guid"] for i in items] == [
        "ghtrend-torvalds/linux",
        "ghtrend-rust-lang/rust",
        "ghtrend-python/cpython",
    ]
    assert [i["author"] for i in items] == ["torvalds", "rust-lang", "python"]
    assert [i["points"] for i in items] == [1234, 56, 1]  # last is "1 star today" (singular)


def test_gh_description_tags_stripped_and_whitespace_collapsed(fake_client, make_result, load_bytes):
    html = load_bytes("sources_misc_github_trending.html")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {})
    # Card 1's <p> had an inline <a>…</a> and doubled spaces; both are normalized.
    assert items[1]["title"] == "rust-lang/rust — A safe, concurrent language"


def test_gh_requests_gh_trending_non_conditional(make_result):
    rec = _RecClient(make_result(content=_gh_html([_gh_card(owner="own", repo="rep")]).encode("utf-8")))
    items = fetch_github_trending(rec, {})
    assert rec.requested == [GH_TRENDING]
    assert rec.conditionals == [False]
    assert [i["guid"] for i in items] == ["ghtrend-own/rep"]  # the recorded body was actually parsed


def test_gh_source_row_is_ignored(fake_client, make_result):
    html = _gh_html([_gh_card(owner="a", repo="b")]).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {"url": "ignored", "mode": "x"})
    assert len(items) == 1
    assert client.requested == [GH_TRENDING]


# --------------------------------------------------------------------------- #
# GitHub trending — stars regex (pure)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected,expected_num",
    [
        ("1,234 stars today", "1,234", 1234),
        ("1 star today", "1", 1),           # singular
        ("56 stars today", "56", 56),
        ("12,345,678 stars today", "12,345,678", 12345678),
        ("0 stars today", "0", 0),
    ],
)
def test_gh_stars_regex_matches(text, expected, expected_num):
    m = _GH_STARS_TODAY_RE.search("noise <span> %s </span> noise" % text)
    assert m is not None
    assert m.group(1) == expected
    # Pin the int the fetcher derives against a hardcoded literal (not re-derived from `expected`).
    assert int(m.group(1).replace(",", "")) == expected_num


@pytest.mark.parametrize(
    "text",
    [
        "12 stars this week",   # wrong window
        "trending today",       # no count
        "stars today",          # no leading number
        "1,234 forks today",    # wrong noun
        "",
    ],
)
def test_gh_stars_regex_no_match(text):
    assert _GH_STARS_TODAY_RE.search(text) is None


def test_gh_missing_stars_yields_none_points(fake_client, make_result):
    html = _gh_html([_gh_card(owner="a", repo="b", stars="")]).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    (item,) = fetch_github_trending(client, {})
    assert item["points"] is None
    assert item["guid"] == "ghtrend-a/b"


# --------------------------------------------------------------------------- #
# GitHub trending — description edge cases
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc", [None, "", "   "])
def test_gh_empty_or_missing_desc_title_is_repo_only(fake_client, make_result, desc):
    html = _gh_html([_gh_card(owner="a", repo="b", desc=desc)]).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    (item,) = fetch_github_trending(client, {})
    assert item["title"] == "a/b"  # `... if desc else repo` collapses to bare repo


def test_gh_dangling_p_without_close_bracket_skips_desc(fake_client, make_result):
    # Truncated markup: a "<p" with NO ">" after it (nothing to the end of the chunk).
    # The `if ">" in after_p` guard is False -> desc stays "" -> title is the bare repo.
    html = b'<article class="Box-row"><a href="/a/b">a / b</a><p dangling-no-bracket'
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    (item,) = fetch_github_trending(client, {})
    assert item["title"] == "a/b"
    assert item["guid"] == "ghtrend-a/b"


# --------------------------------------------------------------------------- #
# GitHub trending — href filtering (repo.count("/") != 1)
# --------------------------------------------------------------------------- #
def test_gh_rejects_nested_path_href(fake_client, make_result):
    # First href in the card is a nested tree path -> count("/") == 3 -> skipped.
    bad = _gh_card(owner="o", repo="r", href="owner/repo/tree/main")
    good = _gh_card(owner="keep", repo="me")
    html = _gh_html([bad, good]).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {})
    assert [i["guid"] for i in items] == ["ghtrend-keep/me"]


def test_gh_rejects_single_segment_href(fake_client, make_result):
    # A bare "/owner" (a user/org page) has count("/") == 0 -> skipped.
    bad = _gh_card(href="justowner")
    good = _gh_card(owner="k", repo="v")
    html = _gh_html([bad, good]).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {})
    assert [i["guid"] for i in items] == ["ghtrend-k/v"]


def test_gh_card_without_any_href_is_skipped(fake_client, make_result):
    # A Box-row chunk with no href="/ at all -> repo stays None -> skipped.
    no_href = '<article class="Box-row"><div>nothing linkable</div></article>'
    good = _gh_card(owner="k", repo="v")
    html = _gh_html([no_href, good]).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {})
    assert [i["guid"] for i in items] == ["ghtrend-k/v"]


# --------------------------------------------------------------------------- #
# GitHub trending — caps
# --------------------------------------------------------------------------- #
def test_gh_caps_at_25_cards(fake_client, make_result):
    cards = [_gh_card(owner="o%d" % i, repo="r%d" % i) for i in range(30)]
    html = _gh_html(cards).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {})
    assert len(items) == 25
    assert items[0]["guid"] == "ghtrend-o0/r0"
    assert items[-1]["guid"] == "ghtrend-o24/r24"  # cards 25..29 never enter the slice


# --------------------------------------------------------------------------- #
# GitHub trending — layout-drift degrades to [] (never bad data)
# --------------------------------------------------------------------------- #
def test_gh_no_article_marker_returns_empty(fake_client, make_result):
    # GitHub renames the card wrapper -> zero splits after [1:] -> [].
    html = b"<html><body><div class='repo-row'>torvalds/linux</div></body></html>"
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    assert fetch_github_trending(client, {}) == []


def test_gh_pre_article_content_is_discarded(fake_client, make_result):
    # Everything before the FIRST article marker (chunks[0]) is dropped by [1:],
    # so a stray href/stars in the page chrome never becomes an item.
    header = (
        '<html><body><a href="/some/nav/link">nav</a>'
        "<span>999 stars today</span>"
    )
    html = _gh_html([_gh_card(owner="real", repo="repo")], header=header).encode("utf-8")
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    items = fetch_github_trending(client, {})
    assert [i["guid"] for i in items] == ["ghtrend-real/repo"]
    assert items[0]["points"] == 5  # from the card, not the header's 999


def test_gh_empty_document_returns_empty(fake_client, make_result):
    client = fake_client(responses={GH_TRENDING: make_result(content=b"<html></html>")})
    assert fetch_github_trending(client, {}) == []


def test_gh_decode_utf8_ignores_invalid_bytes(fake_client, make_result):
    # content.decode("utf-8", "ignore") drops undecodable bytes without crashing.
    card = _gh_card(owner="a", repo="b", desc="Fast tool").encode("utf-8")
    html = b"\xff\xfe" + card  # leading garbage bytes (invalid utf-8)
    client = fake_client(responses={GH_TRENDING: make_result(content=html)})
    (item,) = fetch_github_trending(client, {})
    assert item["guid"] == "ghtrend-a/b"
    assert item["title"] == "a/b — Fast tool"


# --------------------------------------------------------------------------- #
# GitHub trending — failure handling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,content,error,match",
    [
        (500, None, None, r"^HTTP 500$"),
        (503, None, "HTTP 503", r"HTTP 503"),
        (0, None, "ConnectError: down", r"ConnectError: down"),
        (429, None, "rate limited", r"rate limited"),
        (200, b"", None, r"^HTTP 200$"),   # 200 but empty body
        (200, None, None, r"^HTTP 200$"),  # 200 but null body
    ],
)
def test_gh_non_200_or_empty_raises(fake_client, make_result, status, content, error, match):
    client = fake_client(
        responses={GH_TRENDING: make_result(content=content, status=status, error=error)}
    )
    with pytest.raises(RuntimeError, match=match):
        fetch_github_trending(client, {})


# =========================================================================== #
# Module constants
# =========================================================================== #
def test_module_constants():
    assert HF_DAILY == "https://huggingface.co/api/daily_papers"
    assert GH_TRENDING == "https://github.com/trending?since=daily"
    assert _GH_STARS_TODAY_RE.pattern == r"([\d,]+)\s+stars?\s+today"


# =========================================================================== #
# Live smoke tests — real hosts. Deselected by default (-m 'not live') and
# additionally env-guarded so `-m live` on a box without SIGNAL_LIVE skips cleanly.
# =========================================================================== #
@pytest.mark.live
def test_live_hf_daily_papers(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to hit the real HF daily-papers API")
    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    client.host_intervals = {}
    client.default_interval = 0.0
    try:
        items = fetch_hf_daily_papers(client, {})
    finally:
        client.close()
    for item in items:
        assert item["guid"].startswith("hf-")
        assert item["raw_url"].startswith("https://arxiv.org/abs/")
        assert item["title"]


@pytest.mark.live
def test_live_github_trending(cfg, conn):  # pragma: no cover - network
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to scrape the real GitHub trending page")
    from signalpipe.ingest.fetch_http import PoliteClient

    client = PoliteClient(cfg, conn)
    client.host_intervals = {}
    client.default_interval = 0.0
    try:
        items = fetch_github_trending(client, {})
    finally:
        client.close()
    for item in items:
        assert item["guid"].startswith("ghtrend-")
        assert item["raw_url"].startswith("https://github.com/")
        assert item["author"] == item["guid"].split("ghtrend-", 1)[1].split("/")[0]
