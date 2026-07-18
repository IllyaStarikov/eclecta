"""Tests for signalpipe.ingest.fetch_http.PoliteClient.

Every HTTP boundary is faked with ``httpx.MockTransport`` (respx is not
installed). The shared ``polite_client_factory`` fixture builds a REAL
PoliteClient whose ``.client`` is swapped for a MockTransport-backed client and
whose rate limiter is neutralized, so tests never sleep or hit the network.

Rate-limiter and deadline tests replace the module's ``time`` reference with a
deterministic fake so waits/deadlines are asserted without real sleeping.
"""

from __future__ import annotations

import hashlib
import os
from typing import List

import httpx
import pytest

from signalpipe.ingest import fetch_http
from signalpipe.ingest.fetch_http import (
    FETCH_DEADLINE_SEC,
    HOST_MIN_INTERVAL,
    MAX_BODY_BYTES,
    FetchResult,
    PoliteClient,
)


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
class _FakeCfg:
    """Duck-typed cfg: __init__ only touches ``.ingest`` and ``.user_agent``."""

    def __init__(self, ingest=None, user_agent="signalpipe-test/ua"):
        self.ingest = dict(ingest or {})
        self.user_agent = user_agent


class _FakeTime:
    """Deterministic stand-in for the ``time`` module used by fetch_http.

    ``monotonic`` walks a fixed sequence (repeating the last value once
    exhausted); ``sleep`` records its arguments instead of blocking.
    """

    def __init__(self, monotonic_values):
        self._vals = list(monotonic_values)
        self._i = 0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        if self._i < len(self._vals):
            v = self._vals[self._i]
        else:
            v = self._vals[-1]
        self._i += 1
        return v

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _seed_cache_row(
    conn,
    url,
    etag=None,
    last_modified=None,
    body_sha256=None,
    fetched_at="2020-01-01T00:00:00+00:00",
    status=200,
):
    conn.execute(
        "INSERT INTO fetch_cache(url, etag, last_modified, body_sha256, "
        "fetched_at, status) VALUES(?,?,?,?,?,?)",
        (url, etag, last_modified, body_sha256, fetched_at, status),
    )


def _cache_row(conn, url):
    return conn.execute("SELECT * FROM fetch_cache WHERE url=?", (url,)).fetchone()


# --------------------------------------------------------------------------- #
# FetchResult dataclass + module constants
# --------------------------------------------------------------------------- #
def test_fetchresult_defaults():
    r = FetchResult(status=200)
    assert r.status == 200
    assert r.content is None
    assert r.unchanged is False
    assert r.error is None
    assert r.final_url is None


def test_module_constants():
    assert MAX_BODY_BYTES == 5 * 1024 * 1024
    assert FETCH_DEADLINE_SEC == 60.0
    # A few representative per-host overrides are present.
    assert HOST_MIN_INTERVAL["arxiv.org"] == 3.5
    assert HOST_MIN_INTERVAL["www.reddit.com"] == 7.0
    assert HOST_MIN_INTERVAL["hn.algolia.com"] == 0.5


# --------------------------------------------------------------------------- #
# __init__ knob parsing
# --------------------------------------------------------------------------- #
def test_init_defaults_when_ingest_empty():
    pc = PoliteClient(_FakeCfg({}))
    try:
        assert pc.default_interval == 2.0
        assert pc.max_body_bytes == MAX_BODY_BYTES
        assert pc.fetch_deadline == FETCH_DEADLINE_SEC
        assert pc.conn is None
        # Base host table copied in verbatim.
        assert pc.host_intervals["arxiv.org"] == 3.5
        # It's a copy, not the module constant.
        assert pc.host_intervals is not HOST_MIN_INTERVAL
        # httpx client built with the documented defaults.
        assert pc.client.timeout.read == 20.0
        assert pc.client.max_redirects == 5
        assert pc.client.follow_redirects is True
    finally:
        pc.close()


def test_init_reads_cfg_knobs_and_merges_host_intervals():
    pc = PoliteClient(
        _FakeCfg(
            {
                "per_host_min_interval_sec": 1.5,
                "max_body_bytes": 123,
                "fetch_deadline_sec": 9.0,
                "http_timeout_sec": 7,
                "max_redirects": 3,
                "host_min_interval": {"foo.example": 42.0, "arxiv.org": 99.0},
            }
        )
    )
    try:
        assert pc.default_interval == 1.5
        assert pc.max_body_bytes == 123
        assert pc.fetch_deadline == 9.0
        # cfg host overrides win over the base table.
        assert pc.host_intervals["foo.example"] == 42.0
        assert pc.host_intervals["arxiv.org"] == 99.0
        # An un-overridden base entry survives the merge.
        assert pc.host_intervals["lobste.rs"] == 2.0
        # Transport knobs actually reach the httpx client (not just parsed).
        assert pc.client.timeout.read == 7.0
        assert pc.client.max_redirects == 3
    finally:
        pc.close()


# --------------------------------------------------------------------------- #
# fetch() — happy path + cache upsert
# --------------------------------------------------------------------------- #
def test_fetch_200_happy_path_and_cache_upsert(polite_client_factory, conn):
    url = "https://example.com/x"
    body = b"hello world"

    def handler(request):
        return httpx.Response(
            200,
            content=body,
            headers={
                "ETag": '"abc"',
                "Last-Modified": "Wed, 01 Jan 2020 00:00:00 GMT",
            },
        )

    pc = polite_client_factory(handler)
    res = pc.fetch(url)

    assert res.status == 200
    assert res.content == body
    assert res.unchanged is False
    assert res.error is None
    assert res.final_url == url

    row = _cache_row(conn, url)
    assert row is not None
    assert row["etag"] == '"abc"'
    assert row["last_modified"] == "Wed, 01 Jan 2020 00:00:00 GMT"
    # Concrete literal (sha256 of b"hello world"), not a mirror of the SUT's
    # own hashing expression — pins the exact stored digest.
    assert row["body_sha256"] == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert row["status"] == 200


def test_fetch_writes_fetched_at_from_now_iso(polite_client_factory, conn, freeze_now_iso):
    frozen = freeze_now_iso(fetch_http)
    url = "https://example.com/tstamp"

    def handler(request):
        return httpx.Response(200, content=b"body")

    pc = polite_client_factory(handler)
    pc.fetch(url)

    row = _cache_row(conn, url)
    assert row["fetched_at"] == frozen


# --------------------------------------------------------------------------- #
# fetch() — conditional GET (304)
# --------------------------------------------------------------------------- #
def test_fetch_304_conditional_get(polite_client_factory, conn, freeze_now_iso):
    frozen = freeze_now_iso(fetch_http)
    url = "https://example.com/cond"
    _seed_cache_row(
        conn,
        url,
        etag='"etag1"',
        last_modified="Wed, 01 Jan 2020 00:00:00 GMT",
        body_sha256="oldsha",
        status=200,
    )
    seen = {}

    def handler(request):
        seen["inm"] = request.headers.get("If-None-Match")
        seen["ims"] = request.headers.get("If-Modified-Since")
        return httpx.Response(304)

    pc = polite_client_factory(handler)
    res = pc.fetch(url)

    # Conditional headers were assembled from the cached row.
    assert seen["inm"] == '"etag1"'
    assert seen["ims"] == "Wed, 01 Jan 2020 00:00:00 GMT"

    assert res.status == 304
    assert res.unchanged is True
    assert res.content is None

    row = _cache_row(conn, url)
    assert row["status"] == 304
    # Validators preserved from the cached row on a 304.
    assert row["etag"] == '"etag1"'
    assert row["last_modified"] == "Wed, 01 Jan 2020 00:00:00 GMT"
    assert row["body_sha256"] == "oldsha"
    # fetched_at refreshed (differs from the stale seeded value).
    assert row["fetched_at"] == frozen


def test_fetch_conditional_false_skips_cache_headers(polite_client_factory, conn):
    url = "https://example.com/nocond"
    _seed_cache_row(conn, url, etag='"etag1"', last_modified="Wed, 01 Jan 2020 00:00:00 GMT")
    seen = {}

    def handler(request):
        seen["inm"] = request.headers.get("If-None-Match")
        seen["ims"] = request.headers.get("If-Modified-Since")
        return httpx.Response(200, content=b"fresh")

    pc = polite_client_factory(handler)
    res = pc.fetch(url, conditional=False)

    # No conditional headers even though a cache row exists.
    assert seen["inm"] is None
    assert seen["ims"] is None
    assert res.status == 200
    assert res.content == b"fresh"
    # A non-conditional 200 is never "unchanged" (cached was ignored).
    assert res.unchanged is False


# --------------------------------------------------------------------------- #
# fetch() — body-hash short-circuit
# --------------------------------------------------------------------------- #
def test_fetch_identical_body_hash_short_circuits(polite_client_factory, conn):
    url = "https://example.com/hashsc"
    body = b"stable feed content"
    sha = hashlib.sha256(body).hexdigest()
    # No etag/last_modified -> no conditional headers -> server returns 200,
    # but the body hash matches the cached one.
    _seed_cache_row(conn, url, body_sha256=sha)

    def handler(request):
        return httpx.Response(200, content=body)

    pc = polite_client_factory(handler)
    res = pc.fetch(url)

    assert res.status == 200
    assert res.unchanged is True
    assert res.content == body


def test_fetch_changed_body_is_not_unchanged(polite_client_factory, conn):
    url = "https://example.com/changed"
    body = b"brand new content"
    _seed_cache_row(conn, url, body_sha256="a-different-old-hash")

    def handler(request):
        return httpx.Response(200, content=body)

    pc = polite_client_factory(handler)
    res = pc.fetch(url)

    assert res.status == 200
    assert res.unchanged is False
    assert res.content == body
    # The stale cached hash was overwritten with the new body's hash
    # (concrete literal: sha256 of b"brand new content").
    row = _cache_row(conn, url)
    assert row["body_sha256"] == "5513aea5c15197e2a26ffb463e7859200ada061938f7c64acce7c6efac146d3b"


def test_fetch_empty_body_hash_is_none(polite_client_factory, conn):
    url = "https://example.com/empty"

    def handler(request):
        return httpx.Response(200, content=b"")

    pc = polite_client_factory(handler)
    res = pc.fetch(url)

    assert res.status == 200
    assert res.content == b""
    assert res.unchanged is False
    # Empty body -> sha is None in the cache.
    row = _cache_row(conn, url)
    assert row["body_sha256"] is None


# --------------------------------------------------------------------------- #
# fetch() — HTTP >= 400
# --------------------------------------------------------------------------- #
def test_fetch_http_error_status(polite_client_factory, conn):
    url = "https://example.com/missing"

    def handler(request):
        # Body present but must never be buffered on an error status.
        return httpx.Response(404, content=b"not found body", headers={"ETag": '"e404"'})

    pc = polite_client_factory(handler)
    res = pc.fetch(url)

    assert res.status == 404
    assert res.error == "HTTP 404"
    assert res.content is None
    assert res.final_url == url

    row = _cache_row(conn, url)
    assert row["status"] == 404
    assert row["body_sha256"] is None
    assert row["etag"] == '"e404"'


# --------------------------------------------------------------------------- #
# fetch() — size caps
# --------------------------------------------------------------------------- #
def test_fetch_content_length_over_cap_rejected(polite_client_factory, conn):
    url = "https://example.com/big"

    def handler(request):
        return httpx.Response(200, content=b"0123456789")  # CL=10

    pc = polite_client_factory(handler)
    pc.max_body_bytes = 5
    res = pc.fetch(url)

    assert res.status == 200
    assert res.error is not None
    assert res.error.startswith("body too large")
    assert "Content-Length 10 > 5" in res.error
    assert res.content is None
    # Rejected before body read AND before any cache write.
    assert _cache_row(conn, url) is None


def test_fetch_streamed_body_exceeds_cap(polite_client_factory, conn):
    url = "https://example.com/drip"

    def handler(request):
        def gen():
            yield b"aaaa"
            yield b"bbbb"
            yield b"cccc"

        return httpx.Response(200, content=gen())  # no Content-Length

    pc = polite_client_factory(handler)
    pc.max_body_bytes = 5
    res = pc.fetch(url)

    assert res.status == 200
    # Full deterministic message, including the "— aborted" suffix.
    assert res.error == "body exceeded 5 bytes — aborted"
    assert res.content is None
    # Aborted mid-stream: no successful cache write.
    assert _cache_row(conn, url) is None


# --------------------------------------------------------------------------- #
# fetch() — deadline
# --------------------------------------------------------------------------- #
def test_fetch_deadline_exceeded(polite_client_factory, conn, monkeypatch):
    url = "https://example.com/slow"

    def handler(request):
        return httpx.Response(200, content=b"data")

    pc = polite_client_factory(handler)
    pc.fetch_deadline = 10.0
    # monotonic calls: rate-limit(x2), deadline-base, then loop-check jumps past.
    fake = _FakeTime([0.0, 0.0, 0.0, 100.0])
    monkeypatch.setattr(fetch_http, "time", fake)

    res = pc.fetch(url)

    assert res.status == 200
    # Deadline value is interpolated into the message (10s), plus the suffix.
    assert res.error == "fetch deadline exceeded (10s) — aborted"
    assert res.content is None


# --------------------------------------------------------------------------- #
# fetch() — transport error
# --------------------------------------------------------------------------- #
def test_fetch_transport_error_status_zero(polite_client_factory):
    url = "https://example.com/boom"

    def handler(request):
        raise httpx.ConnectError("connection refused")

    pc = polite_client_factory(handler)
    res = pc.fetch(url)

    assert res.status == 0
    assert res.content is None
    assert res.error is not None
    assert res.error.startswith("ConnectError:")
    assert "connection refused" in res.error


# --------------------------------------------------------------------------- #
# fetch() — network-only (conn=None)
# --------------------------------------------------------------------------- #
def test_fetch_conn_none_network_only(polite_client_factory):
    url = "https://example.com/nocache"
    body = b"payload"

    def handler(request):
        return httpx.Response(200, content=body)

    pc = polite_client_factory(handler, cache=False)
    assert pc.conn is None
    res = pc.fetch(url)

    assert res.status == 200
    assert res.content == body
    # No conditional dedup possible without a cache.
    assert res.unchanged is False


def test_cache_helpers_noop_without_conn(polite_client_factory):
    def handler(request):
        return httpx.Response(200, content=b"x")

    pc = polite_client_factory(handler, cache=False)
    # _cache_row returns None and _cache_put short-circuits (returns None,
    # never touches conn) — removing the `conn is None` guard would raise
    # AttributeError on conn.execute and fail here.
    assert pc._cache_row("https://example.com/whatever") is None
    assert pc._cache_put("https://example.com/whatever", 200, "e", "lm", "sha") is None
    # A network-only fetch still works and never grows a cache row.
    res = pc.fetch("https://example.com/whatever")
    assert res.status == 200
    assert res.content == b"x"
    assert pc._cache_row("https://example.com/whatever") is None


# --------------------------------------------------------------------------- #
# resolve()
# --------------------------------------------------------------------------- #
def test_resolve_head_200_returns_final_url(polite_client_factory):
    url = "https://example.com/redir"
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200)

    pc = polite_client_factory(handler)
    out = pc.resolve(url)

    assert out == url
    assert methods == ["HEAD"]  # no GET fallback


def test_resolve_405_falls_back_to_get(polite_client_factory):
    url = "https://example.com/nohead"
    methods = []
    body_consumed = {"n": 0}

    def handler(request):
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)

        def gen():
            body_consumed["n"] += 1
            yield b"should-not-be-read"

        return httpx.Response(200, content=gen())

    pc = polite_client_factory(handler)
    out = pc.resolve(url)

    assert out == url
    assert methods == ["HEAD", "GET"]
    # Body was streamed but never iterated.
    assert body_consumed["n"] == 0


def test_resolve_http_error_returns_none(polite_client_factory):
    url = "https://example.com/dead"

    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    pc = polite_client_factory(handler)
    assert pc.resolve(url) is None


# --------------------------------------------------------------------------- #
# _respect_rate_limit
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "host, host_intervals, default_interval, last_hit, monotonic_seq,"
    " expected_slept, expected_last_hit",
    [
        # Per-host override drives a positive wait.
        ("a.example", {"a.example": 5.0}, 2.0, 100.0, [102.0, 105.0], [3.0], 105.0),
        # Negative wait -> never sleeps.
        ("n.example", {}, 2.0, 10.0, [100.0, 100.0], [], 100.0),
        # Default interval drives a positive wait when no host override.
        ("d.example", {}, 2.0, 50.0, [51.0, 52.0], [1.0], 52.0),
    ],
)
def test_respect_rate_limit_wait_computation(
    cfg,
    monkeypatch,
    host,
    host_intervals,
    default_interval,
    last_hit,
    monotonic_seq,
    expected_slept,
    expected_last_hit,
):
    pc = PoliteClient(cfg)
    try:
        pc.host_intervals = dict(host_intervals)
        pc.default_interval = default_interval
        pc._last_hit = {host: last_hit}
        fake = _FakeTime(monotonic_seq)
        monkeypatch.setattr(fetch_http, "time", fake)

        pc._respect_rate_limit("https://%s/path" % host)

        assert fake.slept == expected_slept
        assert pc._last_hit[host] == expected_last_hit
    finally:
        pc.close()


def test_respect_rate_limit_second_same_host_call_waits(cfg, monkeypatch):
    pc = PoliteClient(cfg)
    try:
        pc.host_intervals = {"s.example": 3.0}
        pc.default_interval = 0.0
        pc._last_hit = {}
        # call1: monotonic 1000.0 (wait<0, no sleep), store 1000.0
        # call2: monotonic 1000.5 (wait=1000+3-1000.5=2.5), store 1003.0
        fake = _FakeTime([1000.0, 1000.0, 1000.5, 1003.0])
        monkeypatch.setattr(fetch_http, "time", fake)

        pc._respect_rate_limit("https://s.example/a")
        assert fake.slept == []  # first hit never waits
        assert pc._last_hit["s.example"] == 1000.0

        pc._respect_rate_limit("https://s.example/b")
        assert fake.slept == [2.5]  # second same-host call waits ~interval
        assert pc._last_hit["s.example"] == 1003.0
    finally:
        pc.close()


def test_respect_rate_limit_host_extraction_lowercased(cfg, monkeypatch):
    pc = PoliteClient(cfg)
    try:
        pc.host_intervals = {"mixed.example": 4.0}
        pc.default_interval = 0.0
        pc._last_hit = {"mixed.example": 200.0}
        fake = _FakeTime([201.0, 205.0])
        monkeypatch.setattr(fetch_http, "time", fake)

        # Uppercase host resolves to the same lowercase interval bucket.
        pc._respect_rate_limit("https://MIXED.EXAMPLE/x")

        assert fake.slept == [3.0]  # 200 + 4 - 201
        assert pc._last_hit["mixed.example"] == 205.0
    finally:
        pc.close()


def test_respect_rate_limit_missing_host_uses_default(cfg, monkeypatch):
    pc = PoliteClient(cfg)
    try:
        pc.host_intervals = {"real.example": 9.0}
        pc.default_interval = 2.0
        pc._last_hit = {"": 300.0}  # empty-host bucket
        fake = _FakeTime([301.0, 304.0])
        monkeypatch.setattr(fetch_http, "time", fake)

        # A url with no hostname -> host "" -> default interval used.
        pc._respect_rate_limit("mailto:someone")

        assert fake.slept == [1.0]  # 300 + 2 - 301
        assert pc._last_hit[""] == 304.0
    finally:
        pc.close()


# --------------------------------------------------------------------------- #
# context manager
# --------------------------------------------------------------------------- #
def test_context_manager_closes_client(cfg):
    pc = PoliteClient(cfg)
    with pc as entered:
        assert entered is pc
        assert pc.client.is_closed is False
    assert pc.client.is_closed is True


def test_exit_returns_false_does_not_suppress(cfg):
    pc = PoliteClient(cfg)
    assert pc.__exit__(None, None, None) is False


# --------------------------------------------------------------------------- #
# fetch_cache upsert round-trip via real db.connect_rw
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_fetch_cache_upsert_roundtrip(polite_client_factory, conn, monkeypatch):
    url = "https://example.com/roundtrip"
    bodies = [b"first-version", b"second-version"]
    calls = {"n": 0}

    def handler(request):
        i = calls["n"]
        calls["n"] += 1
        return httpx.Response(200, content=bodies[i], headers={"ETag": '"v%d"' % i})

    times = iter(["2026-07-04T00:00:01+00:00", "2026-07-04T00:00:02+00:00"])
    monkeypatch.setattr(fetch_http, "_now_iso", lambda: next(times))

    pc = polite_client_factory(handler)
    pc.fetch(url)
    pc.fetch(url)

    count = conn.execute("SELECT COUNT(*) AS c FROM fetch_cache WHERE url=?", (url,)).fetchone()[
        "c"
    ]
    assert count == 1  # ON CONFLICT(url) DO UPDATE, not a second row

    row = _cache_row(conn, url)
    assert row["etag"] == '"v1"'  # latest fetch won
    # Concrete literal: sha256 of b"second-version" (the winning body).
    assert row["body_sha256"] == "0bf4f0c2fe256440721e138b839d11642d6529b99be0b9d7c1752cc954cb0b40"
    assert row["fetched_at"] == "2026-07-04T00:00:02+00:00"


# --------------------------------------------------------------------------- #
# live smoke (skipped unless SIGNAL_LIVE=1)
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_fetch_example_com(cfg):
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live network test; set SIGNAL_LIVE=1 to run")
    cfg.ingest["per_host_min_interval_sec"] = 0
    pc = PoliteClient(cfg, None)
    try:
        res = pc.fetch("https://example.com")
        assert res.status == 200
        assert res.content
    finally:
        pc.close()


# --------------------------------------------------------------------------- #
# robots.txt compliance (RFC 9309)
# --------------------------------------------------------------------------- #
def _robots_handler(robots_body: str, robots_status: int = 200):
    """A MockTransport handler that serves a canned robots.txt for /robots.txt
    and a plain 200 "article body" for every other path. Returns (handler, seen)
    where ``seen`` records every requested path."""
    seen: List[str] = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            if robots_status >= 400:
                return httpx.Response(robots_status)
            return httpx.Response(200, content=robots_body.encode("utf-8"))
        return httpx.Response(200, content=b"article body")

    return handler, seen


def test_fetchresult_blocked_by_robots_default():
    assert FetchResult(status=200).blocked_by_robots is False


def test_init_robots_knobs_off_by_default():
    pc = PoliteClient(_FakeCfg({}))
    try:
        # Off in code so the suite is undisturbed; TTL defaults to 24h.
        assert pc.respect_robots is False
        assert pc.robots_ttl == 86400.0
        assert pc._robots == {}
    finally:
        pc.close()


def test_init_robots_knobs_from_cfg():
    pc = PoliteClient(_FakeCfg({"respect_robots": True, "robots_cache_ttl_sec": 10}))
    try:
        assert pc.respect_robots is True
        assert pc.robots_ttl == 10.0
        assert pc.user_agent == "signalpipe-test/ua"
    finally:
        pc.close()


def test_robots_disallow_blocks_matching_path(polite_client_factory):
    handler, seen = _robots_handler("User-agent: *\nDisallow: /private/\n")
    pc = polite_client_factory(handler)
    pc.respect_robots = True

    blocked = pc.fetch("https://ex.com/private/secret")
    assert blocked.status == 0
    assert blocked.blocked_by_robots is True
    assert blocked.error == "blocked by robots.txt"
    assert blocked.content is None
    assert blocked.final_url == "https://ex.com/private/secret"
    # Blocked BEFORE the article GET: only robots.txt was hit for this path.
    assert seen == ["/robots.txt"]

    # A path the same policy allows proceeds (robots now cached, not refetched).
    allowed = pc.fetch("https://ex.com/public/post")
    assert allowed.status == 200
    assert allowed.content == b"article body"
    assert allowed.blocked_by_robots is False
    assert seen == ["/robots.txt", "/public/post"]


def test_robots_allows_when_robots_missing(polite_client_factory):
    # 404 robots.txt => no applicable policy => fail-open allow.
    handler, _seen = _robots_handler("", robots_status=404)
    pc = polite_client_factory(handler)
    pc.respect_robots = True

    res = pc.fetch("https://ex.com/anything")
    assert res.status == 200
    assert res.content == b"article body"
    assert res.blocked_by_robots is False


def test_robots_ignored_when_disabled(polite_client_factory):
    # A blanket Disallow that WOULD block, but respect_robots is off by default.
    handler, seen = _robots_handler("User-agent: *\nDisallow: /\n")
    pc = polite_client_factory(handler)

    res = pc.fetch("https://ex.com/blocked-if-checked")
    assert res.status == 200
    assert res.content == b"article body"
    # robots.txt is never even fetched when the feature is disabled.
    assert "/robots.txt" not in seen


def test_robots_fetched_once_per_netloc(polite_client_factory):
    handler, seen = _robots_handler("User-agent: *\nDisallow: /x/\n")
    pc = polite_client_factory(handler)
    pc.respect_robots = True

    pc.fetch("https://ex.com/a")
    pc.fetch("https://ex.com/b")
    # Cached per netloc for robots_ttl: only one robots.txt fetch for two GETs.
    assert seen.count("/robots.txt") == 1


def test_robots_fail_open_on_transport_error(polite_client_factory):
    def handler(request):
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=b"article body")

    pc = polite_client_factory(handler)
    pc.respect_robots = True

    # A broken robots.txt must not halt ingestion.
    res = pc.fetch("https://ex.com/x")
    assert res.status == 200
    assert res.content == b"article body"


def test_robots_ua_specific_disallow(polite_client_factory):
    # RobotFileParser matches the token before the first "/" of our UA
    # ("signalpipe-test/ua" -> "signalpipe-test").
    robots = "User-agent: signalpipe-test\nDisallow: /nope/\n\nUser-agent: *\nDisallow:\n"
    handler, _seen = _robots_handler(robots)
    pc = polite_client_factory(handler)
    pc.respect_robots = True

    blocked = pc.fetch("https://ex.com/nope/here")
    assert blocked.blocked_by_robots is True
    allowed = pc.fetch("https://ex.com/ok/here")
    assert allowed.status == 200
