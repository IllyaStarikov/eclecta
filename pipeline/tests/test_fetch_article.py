"""Tests for signalpipe.fetch_article.

Covers the pure helpers (_is_medium, _looks_paywalled, _SUBSCRIBE_RE, _now_iso),
the trafilatura/readability extraction seam (_extract, both present- and
absent-library branches), _primary_surface, and the full run() resolution
matrix: publication-free / primary / freedium / canonical-fallback (+ the
internal-only archive gate) / failed / skipped, plus cluster_ids re-fetch with
IN() chunking.

Every network boundary is faked: run() constructs PoliteClient(cfg, conn), so we
monkeypatch fetch_article.PoliteClient with a scripted fake, and inject text via
a patched _extract for the resolution-branch tests (the real libs are optional).
No real HTTP, ever.
"""

from __future__ import annotations

import builtins
import datetime

import pytest

from signalpipe import fetch_article as fa


# --------------------------------------------------------------------------- #
# Local fakes / helpers
# --------------------------------------------------------------------------- #
class LocalFakeClient:
    """Drop-in for PoliteClient: scripted FetchResults keyed by exact URL.

    run() calls PoliteClient(cfg, conn) positionally, so we install an instance
    of this via a monkeypatched factory. Records fetched URLs and close().
    """

    def __init__(self, responses=None, default=None):
        self._responses = dict(responses or {})
        self._default = default
        self.requested = []
        self.closed = False

    def fetch(self, url, conditional=True):
        self.requested.append(url)
        if url in self._responses:
            value = self._responses[url]
            return value() if callable(value) else value
        if self._default is not None:
            return self._default() if callable(self._default) else self._default
        raise AssertionError("LocalFakeClient: no canned response for %r" % url)

    def close(self):
        self.closed = True


def _install_client(monkeypatch, client):
    """Make run()'s ``PoliteClient(cfg, conn)`` return our prepared fake."""
    monkeypatch.setattr(fa, "PoliteClient", lambda *a, **k: client)


def _article(conn, cluster_id):
    return conn.execute("SELECT * FROM articles WHERE cluster_id=?", (cluster_id,)).fetchone()


class FakeCfg:
    """Minimal config exposing only ``.paywall`` — all _looks_paywalled reads."""

    def __init__(self, paywall):
        self.paywall = paywall


def _words(n, prefix="word"):
    return " ".join("%s%d" % (prefix, i) for i in range(n))


def _libs_present():
    try:
        import readability  # noqa: F401
        import trafilatura  # noqa: F401

        return True
    except Exception:  # pragma: no cover - env dependent
        return False


def _readability_present():
    try:
        import readability  # noqa: F401

        return True
    except Exception:  # pragma: no cover - env dependent
        return False


# --------------------------------------------------------------------------- #
# _now_iso
# --------------------------------------------------------------------------- #
def test_now_iso_is_tz_aware_utc():
    s = fa._now_iso()
    dt = datetime.datetime.fromisoformat(s)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.timedelta(0)


# --------------------------------------------------------------------------- #
# _is_medium
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://medium.com/@user/a-post-123", True),
        ("https://MEDIUM.com/@user/post", True),  # host lowercased
        ("https://uxdesign.medium.com/story", True),  # subdomain
        ("https://towardsdatascience.medium.com/x", True),
        ("https://example.com/post", False),
        ("https://notmedium.com/post", False),
        ("https://medium.com.evil.com/post", False),  # suffix spoof
        ("not-a-url", False),  # hostname None -> ""
        ("", False),
    ],
)
def test_is_medium(url, expected):
    assert fa._is_medium(url) is expected


# --------------------------------------------------------------------------- #
# _SUBSCRIBE_RE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Please Subscribe to keep reading",
        "You must sign in to continue reading this",
        "Create a free account to read more",
        "Are you already a member? Log in",
        "This article is for subscribers only",
        "This article is for paying members",
    ],
)
def test_subscribe_re_matches_paywall_boilerplate(text):
    assert fa._SUBSCRIBE_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "A calm ordinary sentence about distributed systems and databases.",
        "The quick brown fox jumped over the lazy dog near the river.",
        "",
    ],
)
def test_subscribe_re_ignores_ordinary_prose(text):
    assert fa._SUBSCRIBE_RE.search(text) is None


# --------------------------------------------------------------------------- #
# _looks_paywalled
# --------------------------------------------------------------------------- #
PAYWALL_CFG = {
    "paywall_domains": ["nytimes.com", "wsj.com", "ft.com"],
    "min_words_not_paywalled": 150,
}


@pytest.mark.parametrize(
    "url,text,expected",
    [
        # On-list domain + short body (< max(150*3, 400) = 450) -> paywalled.
        ("https://www.nytimes.com/2026/story", _words(100), True),
        # On-list domain + no extracted text -> paywalled.
        ("https://www.wsj.com/articles/x", None, True),
        # On-list subdomain match.
        ("https://cooking.nytimes.com/recipe", _words(10), True),
        # On-list domain + full text (>= 450 words) -> NOT paywalled.
        ("https://www.ft.com/content/y", _words(500), False),
        # Off-list + short body carrying subscribe boilerplate -> paywalled.
        ("https://blog.example.com/x", "Please subscribe to read on", True),
        # Off-list + short body WITHOUT boilerplate -> NOT paywalled.
        ("https://blog.example.com/x", _words(10), False),
        # Off-list + long body -> NOT paywalled.
        ("https://blog.example.com/x", _words(300), False),
        # Off-list + no text -> NOT paywalled (only on-list+no-text trips).
        ("https://blog.example.com/x", None, False),
    ],
)
def test_looks_paywalled(url, text, expected):
    cfg = FakeCfg(dict(PAYWALL_CFG))
    assert fa._looks_paywalled(cfg, url, text) is expected


def test_looks_paywalled_respects_custom_min_words():
    # Bump the threshold; a 200-word off-list body with boilerplate now trips
    # the short+subscribe rule (200 < 250) where at the default (150) it would not.
    cfg = FakeCfg({"paywall_domains": [], "min_words_not_paywalled": 250})
    text = _words(200) + " subscribe"
    assert fa._looks_paywalled(cfg, "https://blog.example.com/x", text) is True
    # Same body, default threshold (150): 201 words >= 150 so the rule is inert.
    cfg2 = FakeCfg({"paywall_domains": [], "min_words_not_paywalled": 150})
    assert fa._looks_paywalled(cfg2, "https://blog.example.com/x", text) is False


# --------------------------------------------------------------------------- #
# _extract — extraction present vs absent
# --------------------------------------------------------------------------- #
ARTICLE_HTML = (
    b"<html><head><title>Hello World</title></head><body>"
    b"<article><h1>Hello World</h1>"
    b"<p>This is the first paragraph of a genuine article with enough real "
    b"substance for the extractor to keep. It discusses technology, science "
    b"and the state of the art in a couple of complete sentences.</p>"
    b"<p>Here is a <b>second</b> paragraph with <a href='x'>a link</a> and "
    b"still more words so the extraction returns a non-trivial body.</p>"
    b"</article></body></html>"
)


@pytest.mark.skipif(not _libs_present(), reason="trafilatura/readability absent")
def test_extract_present_returns_plaintext_and_meta():
    text, meta = fa._extract(ARTICLE_HTML, "https://example.com/post")
    assert text  # non-empty
    assert "<" not in text  # tags stripped to plaintext
    # BOTH article paragraphs must survive — a first-block-only regression
    # would keep "first paragraph" but drop the tail sentence.
    assert "first paragraph" in text.lower()
    assert "still more words so the extraction returns" in text.lower()
    # trafilatura path populates the meta dict with exactly these keys; on
    # metadata-free HTML the values are all None (no author/date/lang/sitename).
    assert set(meta) == {"author", "date", "lang", "sitename"}


@pytest.mark.skipif(not _readability_present(), reason="readability absent")
def test_extract_readability_fallback_strips_tags(monkeypatch):
    # Force trafilatura absent so the readability fallback path runs.
    real_import = builtins.__import__

    def block_trafilatura(name, *a, **k):
        if name == "trafilatura":
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", block_trafilatura)
    text, meta = fa._extract(ARTICLE_HTML, "https://example.com/post")
    assert text
    assert "<" not in text and ">" not in text  # tags collapsed
    assert "  " not in text  # whitespace collapsed
    assert "first paragraph" in text.lower()
    assert meta == {}  # readability path leaves meta empty


def test_extract_returns_none_when_both_libs_absent(monkeypatch):
    real_import = builtins.__import__

    def block_both(name, *a, **k):
        if name in ("trafilatura", "readability"):
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", block_both)
    text, meta = fa._extract(ARTICLE_HTML, "https://example.com/post")
    assert text is None
    assert meta == {}


def test_extract_empty_html_yields_no_text():
    text, _meta = fa._extract(b"<html><body></body></html>", "https://x.com/e")
    assert text is None


@pytest.mark.skipif(not _readability_present(), reason="readability absent")
def test_extract_trafilatura_empty_text_falls_through_but_keeps_meta(monkeypatch):
    # trafilatura returns a doc with populated meta but no usable text: meta is
    # captured, then the readability fallback supplies the body.
    import sys
    import types

    fake = types.ModuleType("trafilatura")

    def bare_extraction(html, url=None, include_comments=False, favor_precision=True):
        return {
            "text": None,
            "raw_text": None,
            "author": "A. Writer",
            "date": "2026-01-01",
            "language": "en",
            "sitename": "Example",
        }

    fake.bare_extraction = bare_extraction
    monkeypatch.setitem(sys.modules, "trafilatura", fake)

    text, meta = fa._extract(ARTICLE_HTML, "https://example.com/post")
    assert text and "<" not in text  # supplied by readability fallback
    assert meta == {
        "author": "A. Writer",
        "date": "2026-01-01",
        "lang": "en",
        "sitename": "Example",
    }


# --------------------------------------------------------------------------- #
# _primary_surface
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.parametrize(
    "canon,expected_match",
    [
        ("https://arxiv.org/abs/2401.00001", True),
        ("https://github.com/org/repo", True),
        ("https://export.arxiv.org/abs/2401.1", True),  # subdomain -> arxiv.org
        ("https://huggingface.co/models/x", True),
        ("https://example.com/story", False),
        ("https://news.ycombinator.com/item?id=1", False),
    ],
)
def test_primary_surface_selects_known_source(conn, seed, canon, expected_match):
    sid = seed.source()
    cid = seed.cluster(canonical_url="https://cluster.example.com/c-%s" % canon[-6:])
    seed.item(cid, sid, canonical_url=canon, guid="g-primary")
    result = fa._primary_surface(conn, cid)
    if expected_match:
        assert result == canon
    else:
        assert result is None


@pytest.mark.integration
def test_primary_surface_ignores_null_canonical(conn, seed):
    sid = seed.source()
    cid = seed.cluster(canonical_url="https://cluster.example.com/null-canon")
    seed.item(cid, sid, canonical_url=None, guid="g-null")
    seed.item(cid, sid, canonical_url="https://github.com/org/repo", guid="g-gh")
    assert fa._primary_surface(conn, cid) == "https://github.com/org/repo"


@pytest.mark.integration
def test_primary_surface_none_when_no_items(conn, seed):
    cid = seed.cluster(canonical_url="https://cluster.example.com/empty")
    assert fa._primary_surface(conn, cid) is None


# --------------------------------------------------------------------------- #
# run() — no finalists
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_no_finalists_returns_zero_without_client(conn, cfg, monkeypatch, capsys):
    def boom(*a, **k):  # PoliteClient must not be constructed on the empty path.
        raise AssertionError("PoliteClient should not be built when no finalists")

    monkeypatch.setattr(fa, "PoliteClient", boom)
    rc = fa.run(cfg)
    assert rc == 0
    assert "no finalists need fetching" in capsys.readouterr().out


@pytest.mark.integration
def test_run_skips_clusters_below_score_gate(conn, cfg, seed, make_result, monkeypatch):
    # Score below min_score_to_curate (3.5) -> not selected -> no fetch, no row.
    cid = seed.cluster(canonical_url="https://blog.example.com/low", score=1.0)
    client = LocalFakeClient(default=make_result(content=b"<html></html>", status=200))
    _install_client(monkeypatch, client)
    monkeypatch.setattr(fa, "_extract", lambda content, url: (_words(200), {}))
    fa.run(cfg, limit=10)
    assert client.requested == []
    assert _article(conn, cid) is None


# --------------------------------------------------------------------------- #
# run() — publication-free (ok)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_publication_free_ok(conn, cfg, seed, make_result, monkeypatch, capsys, freeze_now_iso):
    url = "https://blog.example.com/post"
    cid = seed.cluster(canonical_url=url, score=5.0)
    client = LocalFakeClient({url: make_result(content=b"<html>body</html>", status=200)})
    _install_client(monkeypatch, client)
    body = _words(200)
    monkeypatch.setattr(fa, "_extract", lambda content, u: (body, {"lang": "en"}))
    frozen = freeze_now_iso(fa)  # pin extracted_at to a known instant

    rc = fa.run(cfg, limit=10)
    assert rc == 0

    row = _article(conn, cid)
    assert row["read_kind"] == "publication-free"
    assert row["fetch_status"] == "ok"
    assert row["paywalled"] == 0
    assert row["archive_url"] is None
    assert row["read_url"] == url
    assert row["source_url"] == url
    assert row["word_count"] == 200
    assert row["lang"] == "en"
    assert row["text"] == body
    # excerpt is the first 70 whitespace-joined tokens of the body.
    assert row["excerpt"] == _words(70)
    assert len(row["excerpt"].split()) == 70
    assert row["extracted_at"] == frozen  # stamped with _now_iso()
    assert client.requested == [url]
    assert client.closed is True
    assert "1 ok, 0 paywalled, 0 failed, 0 skipped" in capsys.readouterr().out


@pytest.mark.integration
def test_run_ok_but_no_text_leaves_read_kind_null(conn, cfg, seed, make_result, monkeypatch):
    # 200 with empty body: extract skipped, text None, off-list -> not paywalled.
    # Actual behavior (res.status==200) => status 'ok', read_kind None, word_count 0.
    url = "https://blog.example.com/empty"
    cid = seed.cluster(canonical_url=url, score=5.0)
    client = LocalFakeClient({url: make_result(content=b"", status=200)})
    _install_client(monkeypatch, client)
    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["fetch_status"] == "ok"
    assert row["read_kind"] is None
    assert row["word_count"] == 0
    assert row["text"] is None
    assert row["read_url"] == url


# --------------------------------------------------------------------------- #
# run() — paywalled -> primary surface
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_paywalled_resolves_to_primary_surface(conn, cfg, seed, make_result, monkeypatch):
    src = "https://www.nytimes.com/2026/07/story"  # on paywall list
    primary = "https://arxiv.org/abs/2401.00001"
    sid = seed.source()
    cid = seed.cluster(canonical_url=src, score=6.0)
    seed.item(cid, sid, canonical_url=primary, guid="g-arxiv")

    client = LocalFakeClient(
        {
            src: make_result(content=b"<html>paywall</html>", status=200),
            primary: make_result(content=b"<html>paper</html>", status=200),
        }
    )
    _install_client(monkeypatch, client)
    short = _words(8)
    long_body = _words(120, prefix="w")

    def fake_extract(content, url):
        return (long_body, {"lang": "en"}) if "arxiv" in url else (short, {})

    monkeypatch.setattr(fa, "_extract", fake_extract)

    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["read_kind"] == "primary"
    assert row["read_url"] == primary
    assert row["source_url"] == src
    assert row["paywalled"] == 1
    assert row["fetch_status"] == "paywalled"
    assert row["word_count"] == 120  # longer primary body replaced the short one
    assert row["text"] == long_body
    assert row["lang"] == "en"
    assert row["archive_url"] is None
    # both the canonical source and the primary surface were fetched, in order
    assert client.requested == [src, primary]


@pytest.mark.integration
def test_run_primary_fetch_non_200_keeps_kind_and_original_text(
    conn, cfg, seed, make_result, monkeypatch
):
    # Primary surface is selected, but fetching it fails (non-200): read_kind
    # stays 'primary', read_url points at the primary, and the original short
    # source text is retained (no replacement).
    src = "https://www.nytimes.com/2026/07/down-primary"
    primary = "https://arxiv.org/abs/2401.99999"
    sid = seed.source()
    cid = seed.cluster(canonical_url=src, score=6.0)
    seed.item(cid, sid, canonical_url=primary, guid="g-arxiv2")
    client = LocalFakeClient(
        {
            src: make_result(content=b"<html>x</html>", status=200),
            primary: make_result(content=b"", status=500),
        }
    )
    _install_client(monkeypatch, client)
    short = _words(9, prefix="src")
    monkeypatch.setattr(fa, "_extract", lambda content, u: (short, {"lang": "en"}))

    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["read_kind"] == "primary"
    assert row["read_url"] == primary
    assert row["text"] == short  # primary fetch failed -> original kept
    assert row["word_count"] == 9
    assert row["fetch_status"] == "paywalled"
    assert client.requested == [src, primary]


@pytest.mark.integration
def test_run_primary_shorter_keeps_original_text(conn, cfg, seed, make_result, monkeypatch):
    # Primary surface is chosen (read_kind primary) but its body is NOT longer,
    # so the original (short) text/meta are retained.
    src = "https://www.wsj.com/articles/z"
    primary = "https://github.com/org/repo"
    sid = seed.source()
    cid = seed.cluster(canonical_url=src, score=6.0)
    seed.item(cid, sid, canonical_url=primary, guid="g-gh")
    client = LocalFakeClient(
        {
            src: make_result(content=b"<html>a</html>", status=200),
            primary: make_result(content=b"<html>b</html>", status=200),
        }
    )
    _install_client(monkeypatch, client)
    original = _words(30, prefix="orig")

    def fake_extract(content, url):
        return (_words(5, prefix="gh"), {}) if "github" in url else (original, {"lang": "en"})

    monkeypatch.setattr(fa, "_extract", fake_extract)
    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["read_kind"] == "primary"
    assert row["read_url"] == primary
    assert row["text"] == original  # shorter primary body rejected
    assert row["word_count"] == 30
    assert row["lang"] == "en"


# --------------------------------------------------------------------------- #
# run() — paywalled medium -> freedium
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_paywalled_medium_routes_to_freedium(conn, cfg, seed, make_result, monkeypatch):
    src = "https://medium.com/@author/great-post-abc123"
    cid = seed.cluster(canonical_url=src, score=5.0)  # no primary items
    client = LocalFakeClient({src: make_result(content=b"<html>m</html>", status=200)})
    _install_client(monkeypatch, client)
    # Short body with subscribe boilerplate -> paywalled (medium is off the domain list).
    monkeypatch.setattr(fa, "_extract", lambda content, u: ("Please subscribe to continue", {}))

    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["read_kind"] == "freedium"
    assert row["read_url"] == "https://freedium.cfd/%s" % src
    assert row["paywalled"] == 1
    assert row["fetch_status"] == "paywalled"
    assert row["archive_url"] is None
    # The teaser text/word_count from the source fetch are retained as-is
    # (freedium is a display redirect, not a re-extraction).
    assert row["text"] == "Please subscribe to continue"
    assert row["word_count"] == 4
    assert client.requested == [src]  # freedium branch does NOT re-fetch


# --------------------------------------------------------------------------- #
# run() — canonical-fallback + internal archive gate
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.parametrize("allow_archive", [False, True])
def test_run_canonical_fallback_archive_gate(
    conn, cfg, seed, make_result, monkeypatch, allow_archive
):
    cfg.data["paywall"]["allow_archive_today"] = allow_archive
    src = "https://www.ft.com/content/paywalled-xyz"  # on list, not medium, no primary
    cid = seed.cluster(canonical_url=src, score=5.0)
    client = LocalFakeClient({src: make_result(content=b"<html>p</html>", status=200)})
    _install_client(monkeypatch, client)
    monkeypatch.setattr(fa, "_extract", lambda content, u: (_words(10), {}))

    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["read_kind"] == "canonical-fallback"
    assert row["read_url"] == src  # read stays canonical; nothing free found
    assert row["paywalled"] == 1
    assert row["fetch_status"] == "paywalled"
    # The short source extraction is kept (no free alternative to swap in).
    assert row["text"] == _words(10)
    assert row["word_count"] == 10
    if allow_archive:
        assert row["archive_url"] == "https://archive.ph/newest/%s" % src
        # Invariant: the internal archive URL is never the rendered read_url.
        assert row["archive_url"] != row["read_url"]
    else:
        assert row["archive_url"] is None
    assert client.requested == [src]


# --------------------------------------------------------------------------- #
# run() — failed + skipped
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_non_200_with_no_text_is_failed(conn, cfg, seed, make_result, monkeypatch):
    url = "https://blog.example.com/down"
    cid = seed.cluster(canonical_url=url, score=5.0)
    client = LocalFakeClient({url: make_result(content=b"", status=503, error="boom")})
    _install_client(monkeypatch, client)
    # _extract is not reached (status != 200), but patch it defensively.
    monkeypatch.setattr(fa, "_extract", lambda content, u: (None, {}))

    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["fetch_status"] == "failed"
    assert row["read_kind"] is None
    assert row["paywalled"] == 0
    assert row["word_count"] == 0
    assert row["read_url"] == url


@pytest.mark.integration
def test_run_null_canonical_is_skipped(conn, cfg, seed, monkeypatch, capsys):
    cid = seed.cluster(canonical_url=None, score=5.0)
    client = LocalFakeClient()  # must never be asked to fetch
    _install_client(monkeypatch, client)

    fa.run(cfg, limit=10)
    row = _article(conn, cid)
    assert row["fetch_status"] == "skipped"
    assert row["source_url"] == ""
    assert row["read_url"] == ""
    assert row["read_kind"] is None
    assert client.requested == []
    assert "0 ok, 0 paywalled, 0 failed, 1 skipped" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# run() — mixed batch: stats aggregation + health/runs/last_run tail
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_mixed_batch_stats_and_tail(conn, cfg, seed, make_result, monkeypatch, capsys):
    a = "https://goodblog.example.com/a"  # publication-free ok
    b = "https://www.nytimes.com/b"  # paywalled -> canonical-fallback
    c = "https://fail.example.com/c"  # failed (503)
    seed.cluster(canonical_url=a, score=9.0)
    seed.cluster(canonical_url=b, score=8.0)
    seed.cluster(canonical_url=c, score=7.0)
    seed.cluster(canonical_url=None, score=6.0)  # skipped

    long_body = _words(200)
    client = LocalFakeClient(
        {
            a: make_result(content=b"<html>a</html>", status=200),
            b: make_result(content=b"<html>b</html>", status=200),
            c: make_result(content=b"", status=503),
        }
    )
    _install_client(monkeypatch, client)

    def fake_extract(content, url):
        if "nytimes" in url:
            return ("short subscribe teaser", {})
        if "goodblog" in url:
            return (long_body, {"lang": "en"})
        return (None, {})

    monkeypatch.setattr(fa, "_extract", fake_extract)

    rc = fa.run(cfg, limit=40)
    assert rc == 0

    out = capsys.readouterr().out
    assert "1 ok, 1 paywalled, 1 failed, 1 skipped" in out
    # score DESC order (9,8,7); the NULL-canonical cluster is skipped, never
    # fetched; the paywalled canonical-fallback (b) does NOT trigger a re-fetch.
    assert client.requested == [a, b, c]
    assert client.closed is True

    # Tail side effects: a health row, a runs row, and cfg.last_run all recorded.
    health = conn.execute(
        "SELECT message, stats FROM health WHERE job='fetch' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert health is not None and "1 ok" in health["message"]
    run_row = conn.execute(
        "SELECT stats FROM runs WHERE job='fetch' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run_row is not None
    import json as _json

    assert _json.loads(run_row["stats"]) == {
        "fetched": 1,
        "paywalled": 1,
        "failed": 1,
        "skipped": 1,
    }
    assert cfg.data["last_run"]["job"] == "fetch"
    assert cfg.data["last_run"]["stats"]["fetched"] == 1


# --------------------------------------------------------------------------- #
# run() — cluster_ids re-fetch: score-gate bypass, no-article filter bypass,
# overwrite of a thin prior row, and IN() chunking past the 400 batch boundary.
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_cluster_ids_refetch_chunking_and_overwrite(
    conn, cfg, seed, make_result, monkeypatch, capsys
):
    n = 401  # > 400 -> exercises the two-chunk IN() loop
    ids = []
    for i in range(n):
        cid = seed.cluster(
            canonical_url="https://ex%d.example.org/p" % i,
            score=None,  # below the (bypassed) score gate
            title="Cluster number %d" % i,
        )
        ids.append(cid)

    # The first cluster already has a thin/empty article row that the normal
    # path would skip (a.cluster_id IS NULL filter) — cluster_ids must overwrite.
    seed.article(
        ids[0],
        read_url="https://ex0.example.org/p",
        source_url="https://ex0.example.org/p",
        read_kind="canonical-fallback",
        word_count=0,
        text=None,
        fetch_status="skipped",
    )

    client = LocalFakeClient(default=make_result(content=b"<html>x</html>", status=200))
    _install_client(monkeypatch, client)
    body = _words(200)
    monkeypatch.setattr(fa, "_extract", lambda content, u: (body, {}))

    rc = fa.run(cfg, cluster_ids=ids)
    assert rc == 0

    # Every requested cluster was fetched and got a fresh ok article row.
    assert len(client.requested) == n
    ok_count = conn.execute(
        "SELECT COUNT(*) AS n FROM articles WHERE fetch_status='ok'"
    ).fetchone()["n"]
    assert ok_count == n

    # The thin prior row was overwritten in place (INSERT OR REPLACE).
    overwritten = _article(conn, ids[0])
    assert overwritten["fetch_status"] == "ok"
    assert overwritten["read_kind"] == "publication-free"
    assert overwritten["word_count"] == 200
    assert overwritten["text"] == body

    assert "%d ok" % n in capsys.readouterr().out
