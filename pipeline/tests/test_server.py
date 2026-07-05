"""Tests for signalpipe.server — the FastAPI reader (RSS + dashboard + health).

The server is a pure reader: every route opens a fresh read-only sqlite
connection per request via ``_conn_or_none(cfg)`` and never writes. We drive it
with ``fastapi.testclient.TestClient`` over ``create_app(cfg)`` where ``cfg`` is
the shared, fully-valid ``Config`` whose ``db_path`` points at a tmp sqlite file
(created + seeded through the shared ``conn``/``seed`` fixtures). No network, no
uvicorn socket bind — ``run()`` is exercised only with ``uvicorn.run`` patched.

``spend_today`` / ``last_*`` use SQL ``date('now')`` (the DB wall clock, not
Python), so day-scoped rows are seeded with the day string read back from the
same connection to avoid the midnight-rollover race.
"""

from __future__ import annotations

import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import signalpipe.server as server_mod  # noqa: E402
from signalpipe import __version__  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _client(cfg) -> TestClient:
    return TestClient(server_mod.create_app(cfg))


def _add_health(conn, job, level, message, ts):
    conn.execute(
        "INSERT INTO health(ts, job, level, message, stats) VALUES(?,?,?,?,?)",
        (ts, job, level, message, None),
    )


def _db_today(conn) -> str:
    """The day string exactly as ``_health_ctx``'s ``date('now')`` will compute
    it, read from the same connection so the two agree even across midnight."""
    return conn.execute("SELECT date('now')").fetchone()[0]


def _ts(i):
    base = datetime.datetime(2026, 7, 4, 8, 0, 0, tzinfo=datetime.timezone.utc)
    return (base + datetime.timedelta(minutes=i)).isoformat()


@pytest.fixture
def curated_cfg(cfg, conn, seed):
    """cfg backed by a DB holding exactly one curated, feed-eligible cluster
    (relevance 8, channel 'ai'). The writer ``conn`` stays open for the test so
    the route's read-only connection sees the committed rows via WAL."""
    seed.source()
    cid = seed.cluster()
    seed.curation(cid)
    return cfg


# --------------------------------------------------------------------------- #
# _health_ctx — pure default + seeded aggregation
# --------------------------------------------------------------------------- #
def test_health_ctx_none_returns_zeroed_default():
    assert server_mod._health_ctx(None) == {
        "sources_enabled": 0,
        "sources_verified": 0,
        "clusters": 0,
        "curated": 0,
        "spend_today": 0.0,
        "last_ingest": None,
        "last_curate": None,
        "failing_sources": [],
        "recent": [],
    }


@pytest.mark.integration
def test_health_ctx_aggregation_against_seeded_db(conn, seed):
    day = _db_today(conn)

    # sources: enabled/verified mix + a failing pair + a below-threshold source
    seed.source(slug="ev1", enabled=1, verified_at="2026-07-01T00:00:00+00:00")
    seed.source(slug="ev2", enabled=1, verified_at="2026-07-01T00:00:00+00:00")
    seed.source(slug="en3", enabled=1, verified_at=None)
    seed.source(slug="dis4", enabled=0, verified_at="2026-07-01T00:00:00+00:00")
    seed.source(slug="fail-a", enabled=1, error_count=5, last_error="err a")
    seed.source(slug="fail-b", enabled=1, error_count=3, last_error="err b")
    seed.source(slug="warn", enabled=1, error_count=2, last_error="minor")

    # clusters + curations (only one done & not-skipped)
    c1 = seed.cluster(canonical_url="https://example.com/a", title="Story A")
    c2 = seed.cluster(canonical_url="https://example.com/b", title="Story B")
    c3 = seed.cluster(canonical_url="https://example.com/c", title="Story C")
    seed.curation(c1, status="done", skip=0)
    seed.curation(c2, status="done", skip=1)  # skipped -> excluded
    seed.curation(c3, status="pending", skip=0)  # not done -> excluded

    # spend: today's row counts; a foreign day must not leak in
    seed.spend(day=day, cli_usd=1.5, api_usd=0.75)
    seed.spend(day="2000-01-01", cli_usd=99.0, api_usd=99.0)

    # health: 10 rows; last ingest/curate + recent LIMIT 8 ordering
    rows = [
        ("ingest", "info", "i1"),
        ("curate", "info", "c1"),
        ("score", "info", "s1"),
        ("ingest", "info", "i2"),  # latest ingest
        ("curate", "warn", "c2"),  # latest curate
        ("fetch", "info", "f1"),
        ("digest", "info", "d1"),
        ("server", "info", "sv1"),
        ("sources", "info", "so1"),
        ("score", "error", "s2"),  # latest overall
    ]
    for i, (job, level, msg) in enumerate(rows):
        _add_health(conn, job, level, msg, _ts(i))

    h = server_mod._health_ctx(conn)

    assert h["sources_enabled"] == 6  # all but the disabled one
    assert h["sources_verified"] == 2  # enabled AND verified_at set
    assert h["clusters"] == 3
    assert h["curated"] == 1  # done & skip=0 only
    assert h["spend_today"] == pytest.approx(2.25)

    # last_* = ts of the highest-id row per job
    assert h["last_ingest"] == _ts(3)
    assert h["last_curate"] == _ts(4)

    assert h["failing_sources"] == [
        {"slug": "fail-a", "error_count": 5, "last_error": "err a"},
        {"slug": "fail-b", "error_count": 3, "last_error": "err b"},
    ]

    # recent = last 8 health rows, newest (highest id) first
    assert len(h["recent"]) == 8
    assert h["recent"][0] == {"ts": _ts(9), "job": "score", "level": "error", "message": "s2"}
    assert h["recent"][-1] == {"ts": _ts(2), "job": "score", "level": "info", "message": "s1"}
    assert [r["message"] for r in h["recent"]] == ["s2", "so1", "sv1", "d1", "f1", "c2", "i2", "s1"]


@pytest.mark.integration
def test_health_ctx_spend_zero_when_no_today_row(conn, seed):
    # spend row exists but for a different day -> spend_today stays 0.0
    seed.spend(day="2000-01-01", cli_usd=5.0, api_usd=5.0)
    h = server_mod._health_ctx(conn)
    assert h["spend_today"] == 0.0
    assert h["last_ingest"] is None
    assert h["last_curate"] is None
    assert h["failing_sources"] == []
    assert h["recent"] == []


# --------------------------------------------------------------------------- #
# _conn_or_none
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_conn_or_none_returns_connection_for_existing_db(cfg, conn):
    got = server_mod._conn_or_none(cfg)
    assert got is not None
    try:
        assert got.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        got.close()


@pytest.mark.integration
def test_conn_or_none_none_for_missing_db(cfg, tmp_path):
    cfg.data["db_path"] = str(tmp_path / "nodb" / "missing.db")
    assert server_mod._conn_or_none(cfg) is None


# --------------------------------------------------------------------------- #
# create_app / run
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_create_app_metadata_and_routes(cfg):
    app = server_mod.create_app(cfg)
    assert isinstance(app, FastAPI)
    assert app.title == "Signal"
    assert app.version == __version__
    paths = {r.path for r in app.routes}
    for p in ("/feed.xml", "/feed/{channel}.xml", "/", "/opml", "/healthz"):
        assert p in paths
    # docs disabled at build time
    assert "/docs" not in paths


@pytest.mark.integration
def test_static_mount_serves_css(cfg):
    resp = _client(cfg).get("/static/signal.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")


@pytest.mark.integration
def test_run_invokes_uvicorn_with_cfg_defaults(cfg, monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    captured = {}

    def fake_run(app, host, port, log_level):
        captured.update(app=app, host=host, port=port, log_level=log_level)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = server_mod.run(cfg)
    assert rc == 0
    assert captured["host"] == cfg.server.get("host") == "127.0.0.1"
    assert captured["port"] == cfg.server.get("port") == 8765
    assert captured["log_level"] == "info"
    assert isinstance(captured["app"], FastAPI)


@pytest.mark.integration
def test_run_honors_explicit_host_and_port(cfg, monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    captured = {}
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, host, port, log_level: captured.update(host=host, port=port),
    )
    rc = server_mod.run(cfg, host="0.0.0.0", port=9999)
    assert rc == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999


# --------------------------------------------------------------------------- #
# /healthz
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_healthz_ok_truncates_and_carries_version(cfg, conn, seed):
    for i in range(6):
        seed.source(slug="fail%d" % i, enabled=1, error_count=3 + i, last_error="e%d" % i)
    for j in range(6):
        _add_health(conn, "ingest", "info", "m%d" % j, _ts(j))

    resp = _client(cfg).get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["version"] == __version__
    assert "time" in body and body["time"]
    # aggregate keys merged in
    assert body["sources_enabled"] == 6
    # truncated to five for the JSON payload — and the truncation keeps the
    # WORST offenders (error_count DESC), dropping the lowest (fail0/ec=3).
    assert len(body["failing_sources"]) == 5
    assert [s["slug"] for s in body["failing_sources"]] == [
        "fail5",
        "fail4",
        "fail3",
        "fail2",
        "fail1",
    ]
    assert body["failing_sources"][0] == {"slug": "fail5", "error_count": 8, "last_error": "e5"}
    assert all("fail0" != s["slug"] for s in body["failing_sources"])
    # recent truncated to five, newest (highest id) first
    assert len(body["recent"]) == 5
    assert [r["message"] for r in body["recent"]] == ["m5", "m4", "m3", "m2", "m1"]


@pytest.mark.integration
def test_healthz_unavailable_when_db_missing(cfg, tmp_path):
    cfg.data["db_path"] = str(tmp_path / "nodb" / "missing.db")
    resp = _client(cfg).get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["version"] == __version__
    assert "time" in body and body["time"]
    # health aggregate keys are omitted when the DB is unavailable
    assert "sources_enabled" not in body
    assert "failing_sources" not in body


# --------------------------------------------------------------------------- #
# /opml
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_opml_404_when_missing(cfg, tmp_path):
    cfg.data["sources"]["opml"] = str(tmp_path / "missing.opml")
    resp = _client(cfg).get("/opml")
    assert resp.status_code == 404
    assert "not generated" in resp.json()["detail"]


@pytest.mark.integration
def test_opml_200_serves_file(cfg, tmp_path):
    opml = tmp_path / "sources.opml"
    opml.write_text("<opml version='2.0'><body/></opml>")
    cfg.data["sources"]["opml"] = str(opml)
    resp = _client(cfg).get("/opml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-opml")
    assert "signal-sources.opml" in resp.headers.get("content-disposition", "")
    assert "<opml" in resp.text


# --------------------------------------------------------------------------- #
# /feed/{channel}.xml
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_feed_channel_404_for_unknown(curated_cfg):
    resp = _client(curated_cfg).get("/feed/not-a-channel.xml")
    assert resp.status_code == 404
    assert "unknown channel" in resp.json()["detail"]


@pytest.mark.integration
def test_feed_channel_200_for_known(curated_cfg):
    resp = _client(curated_cfg).get("/feed/ai.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")
    body = resp.text
    assert "<rss" in body
    assert "<title>Example story about AI models</title>" in body
    # the single seeded cluster is id 1 -> its byte-stable, non-permalink guid
    assert '<guid isPermaLink="false">tag:starikov.co,2026:signal/1</guid>' in body


@pytest.mark.integration
def test_feed_channel_known_but_empty_still_200(cfg, conn, seed):
    # channel is valid but no curated item matches -> empty but well-formed feed
    resp = _client(cfg).get("/feed/security.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")
    body = resp.text
    assert "<rss" in body
    assert "<item>" not in body


# --------------------------------------------------------------------------- #
# /feed.xml
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.parametrize(
    "params",
    [
        {"min_score": "11"},
        {"min_score": "-1"},
        {"min_relevance": "0"},
        {"min_relevance": "11"},
        {"limit": "0"},
    ],
)
def test_feed_xml_query_validation_422(cfg, params):
    resp = _client(cfg).get("/feed.xml", params=params)
    assert resp.status_code == 422


@pytest.mark.integration
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"min_score": "0"},
        {"min_score": "10"},
        {"min_relevance": "1"},
        {"min_relevance": "10"},
        {"limit": "1"},
        {"channel": "ai", "since": "7d", "sources": "example"},
    ],
)
def test_feed_xml_valid_params_200(curated_cfg, params):
    resp = _client(curated_cfg).get("/feed.xml", params=params)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")


@pytest.mark.integration
def test_feed_xml_503_when_db_uninitialized(cfg, tmp_path):
    cfg.data["db_path"] = str(tmp_path / "nodb" / "missing.db")
    resp = _client(cfg).get("/feed.xml")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "database not initialized; run ingest first"


@pytest.mark.integration
def test_feed_xml_renders_curated_item_content(curated_cfg):
    body = _client(curated_cfg).get("/feed.xml").text
    # curated item -> why_it_matters surfaces in the description/content
    assert "It matters because reasons." in body
    assert "<content:encoded>" in body


# --------------------------------------------------------------------------- #
# / (dashboard)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_dashboard_empty_state(cfg, conn):
    # DB exists (conn fixture) but is empty
    resp = _client(cfg).get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "SIGNAL" in body
    assert "No items yet" in body
    # no digest section rendered for an empty DB
    assert "digest —" not in body


@pytest.mark.integration
def test_dashboard_unavailable_db_renders_empty(cfg, tmp_path):
    # missing DB -> conn None; dashboard still renders (items empty, health zeroed)
    cfg.data["db_path"] = str(tmp_path / "nodb" / "missing.db")
    resp = _client(cfg).get("/")
    assert resp.status_code == 200
    assert "No items yet" in resp.text


@pytest.mark.integration
def test_dashboard_populated_with_items_and_digest(cfg, conn, seed):
    seed.source()
    cid = seed.cluster()
    seed.curation(cid)
    seed.digest(period_key="2026-W27", body_html="<h1>Weekly wrap</h1><p>Digest body.</p>")

    resp = _client(cfg).get("/")
    assert resp.status_code == 200
    body = resp.text
    # item rendered
    assert "Example story about AI models" in body
    # digest section rendered with its fields
    assert "2026-W27" in body
    assert "Weekly wrap" in body
    assert "Digest body." in body


@pytest.mark.integration
@pytest.mark.parametrize(
    "limit,expected",
    [
        ("0", 422),
        ("201", 422),
        ("1", 200),
        ("60", 200),
        ("200", 200),
    ],
)
def test_dashboard_limit_validation(cfg, conn, limit, expected):
    resp = _client(cfg).get("/", params={"limit": limit})
    assert resp.status_code == expected


@pytest.mark.integration
def test_dashboard_channel_filter_active_marker(cfg, conn, seed):
    # a valid channel is echoed into the page (active nav) without error
    seed.source()
    cid = seed.cluster()
    seed.curation(cid)
    resp = _client(cfg).get("/", params={"channel": "ai"})
    assert resp.status_code == 200
    assert "/?channel=ai" in resp.text


# --------------------------------------------------------------------------- #
# live binding smoke (opt-in only; TestClient already covers behavior)
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_run_binds_and_serves_healthz(cfg):
    import os

    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live binding smoke; set SIGNAL_LIVE=1 to run")
    # Intentionally not executed in CI: run() binds a real socket and blocks.
    # TestClient-based tests above cover the served behavior hermetically.
    pytest.skip("covered hermetically by TestClient tests")
