"""Tests for ``signalpipe.ingest.pipeline`` — the ingest orchestrator.

Three surfaces are covered hermetically (no network, ever):

* ``_fetch_for_source`` dispatch — every slug/type route is verified by
  monkeypatching each fetcher module's ``fetch_items`` (a module-level attribute
  that pipeline holds a reference to) to a recorder. We assert both the route AND
  the config-derived kwargs (pages/mode/instances/resolve_top/days/queries).
* ``_store_items`` — driven against a real sqlite schema (``conn``) with
  ``pipeline._now_iso`` frozen. Item insert/update, canonicalization, surface
  upsert, the cluster_id-None skip, and the per-item error paths are asserted on
  actual stored rows.
* ``run`` — the full cycle with ``pipeline.PoliteClient`` replaced by a dummy and
  ``pipeline._fetch_for_source`` replaced by canned items, so no fetcher/network
  ever runs. Per-source isolation, auto-disable, store-phase rollback, ``only=``,
  ``limit=`` and the no-due short-circuit are exercised end to end.

The due-source SQL uses SQLite ``datetime('now')``; we make dueness deterministic
by seeding ``last_fetch=NULL`` (always due), a far-past ISO (always due), or
``enabled=0`` (never due) rather than trying to freeze the SQL clock.
"""

from __future__ import annotations

import datetime
import json
import sqlite3

import pytest

import signalpipe.db as db_mod
from signalpipe import dedup
from signalpipe.canonical import canonicalize
from signalpipe.ingest import pipeline

FROZEN = "2026-07-04T12:00:00+00:00"


# --------------------------------------------------------------------------- #
# Local fakes / builders
# --------------------------------------------------------------------------- #
class FakeCfg:
    """Duck-typed stand-in exposing only what pipeline touches: ``.ingest`` (for
    dispatch kwargs) and ``.dedup`` (passed straight to ``assign_cluster``)."""

    def __init__(self, ingest=None, dedup=None):
        self.ingest = {} if ingest is None else dict(ingest)
        self.dedup = {} if dedup is None else dict(dedup)


def _item(**over):
    """A well-formed connector item dict; override any field via kwargs."""
    it = {
        "raw_url": "https://example.com/story",
        "guid": "g1",
        "title": "Fresh headline about widgets and gadgets",
        "author": "Jane Doe",
        "published_at": "2026-06-01T00:00:00+00:00",
        "points": 10,
        "comments": 5,
        "extra": {},
    }
    it.update(over)
    return it


def _n(conn, sql, *params):
    """Scalar COUNT helper."""
    return conn.execute(sql, params).fetchone()[0]


# --------------------------------------------------------------------------- #
# _fetch_for_source dispatch
# --------------------------------------------------------------------------- #
# (pipeline attribute, function name, recorder key)
_FETCHERS = [
    ("hn_mod", "fetch_items", "hn"),
    ("lobsters_mod", "fetch_items", "lobsters"),
    ("reddit_mod", "fetch_items", "reddit"),
    ("arxiv_mod", "fetch_items", "arxiv"),
    ("mastodon_mod", "fetch_items", "mastodon"),
    ("bluesky_mod", "fetch_items", "bsky"),
    ("googlenews_mod", "fetch_items", "gnews"),
    ("wikipedia_events_mod", "fetch_items", "wiki"),
    ("gdelt_mod", "fetch_items", "gdelt"),
    ("devto_mod", "fetch_items", "devto"),
    ("stackexchange_mod", "fetch_items", "stackoverflow"),
    ("rss_mod", "fetch_feed_items", "rss"),
    ("sources_misc", "fetch_hf_daily_papers", "hf"),
    ("sources_misc", "fetch_github_trending", "gh"),
]


def _recorder(records, key):
    def _rec(client, source_row, **kwargs):
        records.append(
            {"key": key, "client": client, "source_row": source_row, "kwargs": kwargs}
        )
        return [{"marker": key}]

    return _rec


@pytest.fixture
def patched_fetchers(monkeypatch):
    """Replace every fetcher entry point with a recorder; return the shared log."""
    records = []
    for attr, func, key in _FETCHERS:
        mod = getattr(pipeline, attr)
        monkeypatch.setattr(mod, func, _recorder(records, key))
    return records


@pytest.mark.parametrize(
    "slug,stype,key,kwargs",
    [
        ("hacker-news", "json", "hn", {"pages": 2}),
        ("lobsters", "json", "lobsters", {"pages": 2}),
        ("reddit-programming", "json", "reddit", {"mode": "public_json"}),
        ("hf-daily-papers", "json", "hf", {}),
        ("github-trending", "scrape", "gh", {}),
        ("arxiv-cs-ai", "atom", "arxiv", {}),
        ("mastodon-social", "json", "mastodon", {"instances": ["mastodon.social"]}),
        ("bsky-feed", "json", "bsky", {}),
        ("gnews-tech", "rss", "gnews", {"resolve_top": 25}),
        ("wiki-events", "api", "wiki", {"days": 2}),
        ("gdelt-ai", "json", "gdelt", {"queries": None}),
        ("devto-top", "json", "devto", {}),
        ("stackoverflow-python", "json", "stackoverflow", {}),
        ("some-blog", "rss", "rss", {}),
        ("some-blog", "atom", "rss", {}),
    ],
)
def test_dispatch_routes_and_default_kwargs(
    patched_fetchers, fake_client, slug, stype, key, kwargs
):
    client = fake_client()
    src = {"slug": slug, "type": stype}
    result = pipeline._fetch_for_source(client, src, FakeCfg(ingest={}))

    # The route's return value flows straight back out.
    assert result == [{"marker": key}]
    # Exactly one fetcher was invoked, with the right client, row and kwargs.
    assert len(patched_fetchers) == 1
    rec = patched_fetchers[0]
    assert rec["key"] == key
    assert rec["client"] is client
    assert rec["source_row"] is src
    assert rec["kwargs"] == kwargs


def test_dispatch_passes_configured_kwargs(patched_fetchers, fake_client):
    cfg = FakeCfg(
        ingest={
            "hn_pages": 5,
            "lobsters_pages": 3,
            "reddit_mode": "api",
            "mastodon_instances": ["a.social", "b.social"],
            "gnews_resolve_top": 7,
            "wiki_events_days": 9,
            "gdelt_queries": ["q1", "q2"],
        }
    )
    client = fake_client()
    cases = [
        ("hacker-news", "json", "hn", {"pages": 5}),
        ("lobsters", "json", "lobsters", {"pages": 3}),
        ("reddit-x", "json", "reddit", {"mode": "api"}),
        ("mastodon-y", "json", "mastodon", {"instances": ["a.social", "b.social"]}),
        ("gnews-z", "json", "gnews", {"resolve_top": 7}),
        ("wiki-z", "json", "wiki", {"days": 9}),
        ("gdelt-z", "json", "gdelt", {"queries": ["q1", "q2"]}),
    ]
    for slug, stype, key, kwargs in cases:
        patched_fetchers.clear()
        res = pipeline._fetch_for_source(client, {"slug": slug, "type": stype}, cfg)
        assert res == [{"marker": key}]
        assert patched_fetchers[0]["kwargs"] == kwargs


def test_dispatch_hn_pages_coerced_from_string(patched_fetchers, fake_client):
    # int(cfg.ingest.get("hn_pages", 2)) coerces a stringy config value.
    cfg = FakeCfg(ingest={"hn_pages": "8"})
    pipeline._fetch_for_source(fake_client(), {"slug": "hacker-news", "type": "json"}, cfg)
    assert patched_fetchers[0]["kwargs"] == {"pages": 8}


def test_dispatch_reddit_prefix_matches_any_subreddit(patched_fetchers, fake_client):
    src = {"slug": "reddit-MachineLearning", "type": "json"}
    pipeline._fetch_for_source(fake_client(), src, FakeCfg())
    assert len(patched_fetchers) == 1
    assert patched_fetchers[0]["key"] == "reddit"
    assert patched_fetchers[0]["source_row"] is src
    # Default reddit_mode flows through even for an arbitrary subreddit slug.
    assert patched_fetchers[0]["kwargs"] == {"mode": "public_json"}


def test_dispatch_unknown_slug_and_type_raises(patched_fetchers, fake_client):
    with pytest.raises(RuntimeError) as exc:
        pipeline._fetch_for_source(
            fake_client(), {"slug": "mystery", "type": "json"}, FakeCfg()
        )
    assert "no fetcher for type=json slug=mystery" in str(exc.value)
    # No fetcher was dispatched.
    assert patched_fetchers == []


# --------------------------------------------------------------------------- #
# _now_iso / constants
# --------------------------------------------------------------------------- #
def test_now_iso_is_tz_aware_utc():
    v = pipeline._now_iso()
    parsed = datetime.datetime.fromisoformat(v)
    assert parsed.tzinfo is not None
    assert v.endswith("+00:00")


def test_auto_disable_threshold_constant():
    assert pipeline.AUTO_DISABLE_ERRORS == 10


# --------------------------------------------------------------------------- #
# _store_items — real schema, frozen clock
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_store_new_item_inserts_item_cluster_and_surface(conn, seed, freeze_now_iso):
    frozen = freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    it = _item(extra={"discussion_url": "https://news.ycombinator.com/item?id=7"})

    with db_mod.write_tx(conn):
        stats = pipeline._store_items(conn, {"id": sid}, [it], FakeCfg())

    assert stats == {"new": 1, "updated": 0}
    row = conn.execute(
        "SELECT * FROM items WHERE source_id=? AND guid='g1'", (sid,)
    ).fetchone()
    assert row["raw_url"] == "https://example.com/story"
    # Pin the literal (not canonicalize(...)) so a canonicalizer regression is caught.
    assert row["canonical_url"] == "https://example.com/story"
    assert row["title"] == "Fresh headline about widgets and gadgets"
    assert row["author"] == "Jane Doe"
    assert row["published_at"] == "2026-06-01T00:00:00+00:00"
    assert row["ingested_at"] == frozen
    assert row["points"] == 10 and row["comments"] == 5
    assert json.loads(row["extra"]) == {
        "discussion_url": "https://news.ycombinator.com/item?id=7"
    }
    assert row["cluster_id"] is not None

    assert _n(conn, "SELECT COUNT(*) FROM clusters") == 1
    surf = conn.execute(
        "SELECT * FROM surfaces WHERE cluster_id=? AND source_id=?",
        (row["cluster_id"], sid),
    ).fetchone()
    assert surf["url"] == "https://news.ycombinator.com/item?id=7"
    assert surf["points"] == 10 and surf["comments"] == 5
    assert surf["seen_at"] == frozen


@pytest.mark.integration
def test_store_surface_url_falls_back_to_raw_url_without_discussion(
    conn, seed, freeze_now_iso
):
    freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    it = _item(raw_url="https://blog.example/post", extra={})

    with db_mod.write_tx(conn):
        pipeline._store_items(conn, {"id": sid}, [it], FakeCfg())

    cid = conn.execute(
        "SELECT cluster_id FROM items WHERE source_id=? AND guid='g1'", (sid,)
    ).fetchone()["cluster_id"]
    surf = conn.execute(
        "SELECT url FROM surfaces WHERE cluster_id=?", (cid,)
    ).fetchone()
    assert surf["url"] == "https://blog.example/post"


@pytest.mark.integration
def test_store_extra_none_uses_raw_url_for_surface(conn, seed, freeze_now_iso):
    # extra=None: (None or {}).get(...) is None -> discussion falls back to raw_url.
    freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    it = _item(raw_url="https://blog.example/x", extra=None)

    with db_mod.write_tx(conn):
        pipeline._store_items(conn, {"id": sid}, [it], FakeCfg())

    cid = conn.execute(
        "SELECT cluster_id FROM items WHERE source_id=? AND guid='g1'", (sid,)
    ).fetchone()["cluster_id"]
    surf = conn.execute("SELECT url FROM surfaces WHERE cluster_id=?", (cid,)).fetchone()
    assert surf["url"] == "https://blog.example/x"
    # extra None is serialized as an empty object on the item row.
    item_extra = conn.execute(
        "SELECT extra FROM items WHERE source_id=? AND guid='g1'", (sid,)
    ).fetchone()["extra"]
    assert json.loads(item_extra) == {}


@pytest.mark.integration
def test_store_aggregator_url_is_still_canonicalized(conn, seed, freeze_now_iso):
    # is_aggregator(raw_url) True takes the elif branch, which STILL canonicalizes
    # (both if/elif branches are behaviorally identical). The surface keeps the raw
    # (un-canonicalized) discussion url.
    freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    agg = "https://news.ycombinator.com/item?id=9&utm_source=x"
    it = _item(raw_url=agg, extra={})

    with db_mod.write_tx(conn):
        pipeline._store_items(conn, {"id": sid}, [it], FakeCfg())

    row = conn.execute(
        "SELECT canonical_url, cluster_id FROM items WHERE source_id=? AND guid='g1'",
        (sid,),
    ).fetchone()
    assert row["canonical_url"] == canonicalize(agg)
    assert row["canonical_url"] == "https://news.ycombinator.com/item?id=9"
    surf = conn.execute(
        "SELECT url FROM surfaces WHERE cluster_id=?", (row["cluster_id"],)
    ).fetchone()
    assert surf["url"] == agg  # raw, not canonical


@pytest.mark.integration
def test_store_empty_raw_url_yields_null_canonical(conn, seed, freeze_now_iso):
    # raw_url="" is falsy: neither the if nor the elif canonicalizes, so
    # canonical_url stays NULL. A discussion_url in extra still drives the surface.
    freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    it = _item(raw_url="", extra={"discussion_url": "https://d/only"})

    with db_mod.write_tx(conn):
        stats = pipeline._store_items(conn, {"id": sid}, [it], FakeCfg())

    assert stats == {"new": 1, "updated": 0}
    row = conn.execute(
        "SELECT raw_url, canonical_url, cluster_id FROM items "
        "WHERE source_id=? AND guid='g1'",
        (sid,),
    ).fetchone()
    assert row["raw_url"] == ""
    assert row["canonical_url"] is None
    assert row["cluster_id"] is not None  # title-key-only cluster
    surf = conn.execute(
        "SELECT url FROM surfaces WHERE cluster_id=?", (row["cluster_id"],)
    ).fetchone()
    assert surf["url"] == "https://d/only"


@pytest.mark.integration
def test_store_existing_item_updates_and_preserves_cluster(conn, seed, freeze_now_iso):
    freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    cfg = FakeCfg()
    it = _item(points=10, comments=5, extra={"discussion_url": "https://d/1"})

    with db_mod.write_tx(conn):
        first = pipeline._store_items(conn, {"id": sid}, [it], cfg)
    assert first == {"new": 1, "updated": 0}
    orig = conn.execute(
        "SELECT id, cluster_id FROM items WHERE source_id=? AND guid='g1'", (sid,)
    ).fetchone()

    it2 = _item(points=99, comments=88, extra={"discussion_url": "https://d/1"})
    with db_mod.write_tx(conn):
        second = pipeline._store_items(conn, {"id": sid}, [it2], cfg)
    assert second == {"new": 0, "updated": 1}

    now = conn.execute(
        "SELECT id, cluster_id, points, comments FROM items "
        "WHERE source_id=? AND guid='g1'",
        (sid,),
    ).fetchone()
    assert now["id"] == orig["id"]
    assert now["cluster_id"] == orig["cluster_id"]
    assert now["points"] == 99 and now["comments"] == 88
    assert _n(conn, "SELECT COUNT(*) FROM items") == 1

    surf = conn.execute(
        "SELECT points, comments FROM surfaces WHERE cluster_id=?", (orig["cluster_id"],)
    ).fetchone()
    assert surf["points"] == 99 and surf["comments"] == 88
    assert _n(conn, "SELECT COUNT(*) FROM surfaces") == 1


@pytest.mark.integration
def test_store_cluster_id_none_skips_surface_but_inserts_item(
    conn, seed, freeze_now_iso, monkeypatch
):
    freeze_now_iso(pipeline)
    monkeypatch.setattr(pipeline, "assign_cluster", lambda *a, **k: None)
    sid = seed.source(slug="src")

    with db_mod.write_tx(conn):
        stats = pipeline._store_items(conn, {"id": sid}, [_item()], FakeCfg())

    assert stats == {"new": 1, "updated": 0}
    row = conn.execute(
        "SELECT cluster_id FROM items WHERE source_id=? AND guid='g1'", (sid,)
    ).fetchone()
    assert row["cluster_id"] is None
    assert _n(conn, "SELECT COUNT(*) FROM surfaces") == 0


@pytest.mark.integration
def test_store_title_none_raises_integrity_error(conn, seed):
    # title=None reaches the clusters INSERT (title NOT NULL) -> IntegrityError.
    sid = seed.source(slug="src")
    with pytest.raises(sqlite3.IntegrityError):
        with db_mod.write_tx(conn):
            pipeline._store_items(conn, {"id": sid}, [_item(title=None)], FakeCfg())


@pytest.mark.integration
@pytest.mark.parametrize("missing", ["raw_url", "guid", "title"])
def test_store_missing_required_key_raises_keyerror(conn, seed, missing):
    sid = seed.source(slug="src")
    it = _item()
    del it[missing]
    with pytest.raises(KeyError):
        with db_mod.write_tx(conn):
            pipeline._store_items(conn, {"id": sid}, [it], FakeCfg())


@pytest.mark.integration
def test_store_multiple_items_returns_aggregate_counts(conn, seed, freeze_now_iso):
    freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    items = [
        _item(guid="a", raw_url="https://a.example/1", title="Alpha about compilers"),
        _item(guid="b", raw_url="https://b.example/2", title="Bravo about kernels"),
        _item(guid="c", raw_url="https://c.example/3", title="Charlie about databases"),
    ]
    with db_mod.write_tx(conn):
        stats = pipeline._store_items(conn, {"id": sid}, items, FakeCfg())
    assert stats == {"new": 3, "updated": 0}
    assert _n(conn, "SELECT COUNT(*) FROM items") == 3
    assert _n(conn, "SELECT COUNT(*) FROM clusters") == 3
    assert _n(conn, "SELECT COUNT(*) FROM surfaces") == 3


# --------------------------------------------------------------------------- #
# run() — full cycle with faked client + fetcher
# --------------------------------------------------------------------------- #
def _install(monkeypatch, items_by_slug, fail=None):
    """Replace PoliteClient (dummy w/ .close()) and _fetch_for_source (canned).

    Returns the list of constructed dummy client instances so a test can assert
    the client was closed. ``fail`` maps a slug to an exception raised at fetch.
    """
    fail = fail or {}
    instances = []

    class DummyClient:
        def __init__(self, cfg, conn):
            self.closed = False
            instances.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(pipeline, "PoliteClient", DummyClient)

    def fake_fetch(client, src, cfg):
        slug = src["slug"]
        if slug in fail:
            raise fail[slug]
        return list(items_by_slug.get(slug, []))

    monkeypatch.setattr(pipeline, "_fetch_for_source", fake_fetch)
    return instances


@pytest.mark.integration
def test_run_happy_path(cfg, conn, seed, monkeypatch, capsys, freeze_now_iso):
    frozen = freeze_now_iso(pipeline)
    sid_a = seed.source(slug="src-a", cadence_min=60)
    sid_b = seed.source(slug="src-b", cadence_min=60)
    items = {
        "src-a": [
            _item(guid="a1", raw_url="https://a.example/1",
                  title="Alpha article about databases")
        ],
        "src-b": [
            _item(guid="b1", raw_url="https://b.example/1",
                  title="Beta article about compilers"),
            _item(guid="b2", raw_url="https://b.example/2",
                  title="Gamma article about kernels"),
        ],
    }
    instances = _install(monkeypatch, items)

    rc = pipeline.run(cfg)
    assert rc == 0

    out = capsys.readouterr().out
    assert "ingest: 2 sources ok, 0 errors, 3 new items, 0 updated" in out

    for sid in (sid_a, sid_b):
        s = conn.execute(
            "SELECT last_fetch, verified_at, error_count, last_error "
            "FROM sources WHERE id=?",
            (sid,),
        ).fetchone()
        assert s["last_fetch"] == frozen
        assert s["verified_at"] == frozen
        assert s["error_count"] == 0
        assert s["last_error"] is None

    assert _n(conn, "SELECT COUNT(*) FROM items") == 3
    assert _n(conn, "SELECT COUNT(*) FROM clusters") == 3
    assert _n(conn, "SELECT COUNT(*) FROM surfaces") == 3

    # refresh_surface_counts populated each cluster.
    counts = sorted(r["surface_count"] for r in conn.execute("SELECT surface_count FROM clusters"))
    assert counts == [1, 1, 1]

    h = conn.execute(
        "SELECT message, stats FROM health WHERE job='ingest' AND level='info'"
    ).fetchone()
    assert h is not None
    assert h["message"] == "ingest: 2 sources ok, 0 errors, 3 new items, 0 updated"
    assert json.loads(h["stats"]) == {"sources": 2, "errors": 0, "new": 3, "updated": 0}

    # record_run appended one ingest run row.
    assert _n(conn, "SELECT COUNT(*) FROM runs WHERE job='ingest'") == 1

    # cfg.write_last_run persisted the totals.
    assert cfg.data["last_run"]["job"] == "ingest"
    assert cfg.data["last_run"]["stats"] == {
        "sources": 2, "errors": 0, "new": 3, "updated": 0,
    }

    # client was constructed once and closed.
    assert len(instances) == 1
    assert instances[0].closed is True


@pytest.mark.integration
def test_run_success_resets_error_count(cfg, conn, seed, monkeypatch, freeze_now_iso):
    frozen = freeze_now_iso(pipeline)
    sid = seed.source(slug="src-a", error_count=5)
    _install(monkeypatch, {"src-a": []})

    rc = pipeline.run(cfg)
    assert rc == 0

    s = conn.execute(
        "SELECT error_count, last_error, last_fetch FROM sources WHERE id=?", (sid,)
    ).fetchone()
    assert s["error_count"] == 0
    assert s["last_error"] is None
    assert s["last_fetch"] == frozen


@pytest.mark.integration
def test_run_only_unknown_slug_returns_1(cfg, capsys):
    rc = pipeline.run(cfg, only="does-not-exist")
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown source slug" in err
    assert "does-not-exist" in err


@pytest.mark.integration
def test_run_only_known_slug_processes_just_that_source(
    cfg, conn, seed, monkeypatch, freeze_now_iso
):
    frozen = freeze_now_iso(pipeline)
    sid_a = seed.source(slug="src-a")
    sid_b = seed.source(slug="src-b")
    _install(
        monkeypatch,
        {"src-a": [_item(guid="a1", raw_url="https://a.example/1",
                         title="Alpha about databases")]},
    )

    rc = pipeline.run(cfg, only="src-a")
    assert rc == 0

    a = conn.execute("SELECT last_fetch FROM sources WHERE id=?", (sid_a,)).fetchone()
    b = conn.execute("SELECT last_fetch FROM sources WHERE id=?", (sid_b,)).fetchone()
    assert a["last_fetch"] == frozen
    assert b["last_fetch"] is None  # never selected
    assert _n(conn, "SELECT COUNT(*) FROM items") == 1


@pytest.mark.integration
def test_run_only_selects_disabled_source(cfg, conn, seed, monkeypatch, freeze_now_iso):
    # only= bypasses the enabled/due filter (SELECT ... WHERE slug=?).
    frozen = freeze_now_iso(pipeline)
    sid = seed.source(slug="src-a", enabled=0)
    _install(monkeypatch, {"src-a": []})

    rc = pipeline.run(cfg, only="src-a")
    assert rc == 0
    s = conn.execute("SELECT last_fetch FROM sources WHERE id=?", (sid,)).fetchone()
    assert s["last_fetch"] == frozen


@pytest.mark.integration
def test_run_no_sources_due(cfg, conn, seed, monkeypatch, capsys):
    seed.source(slug="src-a", enabled=0)
    seed.source(slug="src-b", enabled=0)
    _install(monkeypatch, {})

    rc = pipeline.run(cfg)
    assert rc == 0
    assert "no sources due" in capsys.readouterr().out
    assert _n(conn, "SELECT COUNT(*) FROM items") == 0
    # No cycle ran, so no ingest health row was logged.
    assert _n(conn, "SELECT COUNT(*) FROM health WHERE job='ingest'") == 0


@pytest.mark.integration
def test_run_per_source_isolation_and_error_truncation(
    cfg, conn, seed, monkeypatch, capsys, freeze_now_iso
):
    frozen = freeze_now_iso(pipeline)
    sid_a = seed.source(slug="src-a")
    sid_b = seed.source(slug="src-b")
    long_msg = "x" * 400
    _install(
        monkeypatch,
        {"src-b": [_item(guid="b1", raw_url="https://b.example/1",
                         title="Beta about compilers")]},
        fail={"src-a": RuntimeError(long_msg)},
    )

    rc = pipeline.run(cfg)
    assert rc == 0

    out = capsys.readouterr().out
    assert "ingest: 1 sources ok, 1 errors, 1 new items, 0 updated" in out

    a = conn.execute(
        "SELECT error_count, last_error, enabled, last_fetch FROM sources WHERE id=?",
        (sid_a,),
    ).fetchone()
    assert a["error_count"] == 1
    assert a["enabled"] == 1  # below the auto-disable threshold
    assert a["last_error"] == long_msg[:300]
    assert len(a["last_error"]) == 300
    assert a["last_fetch"] is None  # never succeeded

    b = conn.execute(
        "SELECT last_fetch, error_count FROM sources WHERE id=?", (sid_b,)
    ).fetchone()
    assert b["last_fetch"] == frozen
    assert b["error_count"] == 0
    assert _n(conn, "SELECT COUNT(*) FROM items") == 1


@pytest.mark.integration
def test_run_auto_disable_at_threshold(cfg, conn, seed, monkeypatch):
    sid = seed.source(slug="src-a", error_count=9)
    _install(monkeypatch, {}, fail={"src-a": RuntimeError("boom")})

    rc = pipeline.run(cfg)
    assert rc == 0

    s = conn.execute(
        "SELECT error_count, enabled FROM sources WHERE id=?", (sid,)
    ).fetchone()
    assert s["error_count"] == 10
    assert s["enabled"] == 0

    h = conn.execute(
        "SELECT message FROM health WHERE job='ingest' AND level='warn'"
    ).fetchone()
    assert h is not None
    assert "auto-disabled" in h["message"]
    assert "src-a" in h["message"]
    assert "after 10 consecutive errors" in h["message"]


@pytest.mark.integration
def test_run_store_phase_failure_rolls_back_and_isolates(
    cfg, conn, seed, monkeypatch, capsys, freeze_now_iso
):
    freeze_now_iso(pipeline)
    sid_a = seed.source(slug="src-a")
    sid_b = seed.source(slug="src-b")
    malformed = _item(guid="a1", raw_url="https://a.example/x", title=None)
    good = _item(guid="b1", raw_url="https://b.example/1", title="Beta about compilers")
    _install(monkeypatch, {"src-a": [malformed], "src-b": [good]})

    rc = pipeline.run(cfg)
    assert rc == 0

    a = conn.execute(
        "SELECT error_count, last_error, enabled FROM sources WHERE id=?", (sid_a,)
    ).fetchone()
    assert a["error_count"] == 1
    assert a["enabled"] == 1
    # The rolled-back store phase records the exact IntegrityError from the
    # NOT NULL clusters.title constraint (title=None), truncated to <=300 chars.
    assert a["last_error"] == "NOT NULL constraint failed: clusters.title"

    # src-a's partial write rolled back: no items and no cluster for it.
    assert _n(conn, "SELECT COUNT(*) FROM items WHERE source_id=?", sid_a) == 0
    assert _n(conn, "SELECT COUNT(*) FROM items WHERE source_id=?", sid_b) == 1
    assert _n(conn, "SELECT COUNT(*) FROM clusters") == 1

    assert "ingest: 1 sources ok, 1 errors, 1 new items, 0 updated" in capsys.readouterr().out


@pytest.mark.integration
def test_run_limit_processes_only_first_n(
    cfg, conn, seed, monkeypatch, capsys, freeze_now_iso
):
    frozen = freeze_now_iso(pipeline)
    # Distinct past last_fetch values make the ORDER BY last_fetch ASC deterministic.
    sid_old = seed.source(slug="src-old", last_fetch="2020-01-01T00:00:00+00:00", cadence_min=60)
    sid_mid = seed.source(slug="src-mid", last_fetch="2021-01-01T00:00:00+00:00", cadence_min=60)
    sid_new = seed.source(slug="src-new", last_fetch="2022-01-01T00:00:00+00:00", cadence_min=60)
    _install(monkeypatch, {})  # every source returns no items

    rc = pipeline.run(cfg, limit=2)
    assert rc == 0
    assert "ingest: 2 sources ok, 0 errors, 0 new items, 0 updated" in capsys.readouterr().out

    # The two oldest were processed; the newest was left untouched.
    assert conn.execute(
        "SELECT last_fetch FROM sources WHERE id=?", (sid_old,)
    ).fetchone()["last_fetch"] == frozen
    assert conn.execute(
        "SELECT last_fetch FROM sources WHERE id=?", (sid_mid,)
    ).fetchone()["last_fetch"] == frozen
    assert conn.execute(
        "SELECT last_fetch FROM sources WHERE id=?", (sid_new,)
    ).fetchone()["last_fetch"] == "2022-01-01T00:00:00+00:00"


@pytest.mark.integration
def test_run_far_past_last_fetch_is_due(cfg, conn, seed, monkeypatch, freeze_now_iso):
    # A source last fetched long ago (well beyond its cadence) is due.
    frozen = freeze_now_iso(pipeline)
    sid = seed.source(slug="src-a", last_fetch="2020-01-01T00:00:00+00:00", cadence_min=60)
    _install(monkeypatch, {"src-a": []})

    rc = pipeline.run(cfg)
    assert rc == 0
    assert conn.execute(
        "SELECT last_fetch FROM sources WHERE id=?", (sid,)
    ).fetchone()["last_fetch"] == frozen


# --------------------------------------------------------------------------- #
# Property: store counts + surface-count consistency
# --------------------------------------------------------------------------- #
@pytest.mark.property
def test_property_store_counts_and_surface_counts(conn, seed, freeze_now_iso):
    """For any well-formed batch: new+updated == len(items), and after
    refresh_surface_counts every cluster's surface_count equals its real surface
    row count. Distinct multi-token titles keep clusters from over-merging so the
    two invariants are exercised across both the new and update paths."""
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    freeze_now_iso(pipeline)
    sid = seed.source(slug="src")
    cfg = FakeCfg()
    vocab = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]

    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(st.lists(st.integers(min_value=0, max_value=6), max_size=15))
    def inner(keys):
        conn.execute("DELETE FROM surfaces")
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM clusters")
        items = [
            {
                "raw_url": "https://ex%d.example/p" % k,
                "guid": "g%d" % k,
                "title": "Report about %s topic" % vocab[k],
                "author": None,
                "published_at": None,
                "points": k,
                "comments": k,
                "extra": {},
            }
            for k in keys
        ]
        with db_mod.write_tx(conn):
            stats = pipeline._store_items(conn, {"id": sid}, items, cfg)
        assert stats["new"] + stats["updated"] == len(items)

        with db_mod.write_tx(conn):
            dedup.refresh_surface_counts(conn)

        for row in conn.execute("SELECT id, surface_count FROM clusters").fetchall():
            actual = conn.execute(
                "SELECT COUNT(*) FROM surfaces WHERE cluster_id=?", (row["id"],)
            ).fetchone()[0]
            assert row["surface_count"] == actual

    inner()


# --------------------------------------------------------------------------- #
# Live smoke — the real cycle against real sources/network. Deselected by
# default (-m 'not live') and env-guarded so `-m live` on a bare box skips.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_run_cycle(cfg, conn, seed):  # pragma: no cover - network
    import os

    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 to run a real ingest cycle")
    seed.source(
        slug="hacker-news", name="Hacker News", type="json",
        url="https://hn.algolia.com/api/v1/search", cadence_min=60,
    )
    assert pipeline.run(cfg, only="hacker-news") == 0
    assert _n(conn, "SELECT COUNT(*) FROM items") > 0
