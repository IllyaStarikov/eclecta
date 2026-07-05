"""Tests for :mod:`signalpipe.backfill` — the one-time historical recovery job.

The module runs ALWAYS against a COPY DB and has three public verbs:

* ``fetch``   — re-fetch missing article text for the top-scored clusters per day
                (delegates to ``fetch_article.run``, the only network leaf).
* ``curate``  — an all-Opus hindsight pass over each day's top clusters that now
                have text, stamping ``curated_at = first_seen`` midnight.
* ``merge``   — fold the copy's new rows back into the LIVE DB via ATTACH, where
                ``INSERT OR IGNORE`` never clobbers a live curation/digest.

Every I/O leaf is faked. The LLM seam is ``adapter.complete`` — backfill does
``from .llm import ... adapter`` so we patch ``backfill.adapter.complete`` (the
name it resolves at call time). ``fetch_article.run`` is patched on its module.
Everything else runs against a real on-disk WAL DB (the ``conn``/``seed``/``cfg``
fixtures). No network, no subprocess, no real LLM.
"""

from __future__ import annotations

import datetime
import itertools
import json
from types import SimpleNamespace

import pytest

import signalpipe.backfill as backfill
import signalpipe.db as db_mod
from signalpipe.llm import LLMError, SpendCapExceeded
from signalpipe.llm.schemas import CURATION_SCHEMA, SYSTEM_CURATE

# Unique canonical_url generator (clusters.canonical_url is UNIQUE).
_CANON = itertools.count(1)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class FakeComplete:
    """Stand-in for ``adapter.complete``.

    Dispatches on the prompt via a supplied ``handler(prompt)`` that returns a
    CURATION_SCHEMA-shaped dict or raises. Every call is recorded on ``.calls``
    so a test can assert the tier / model_override / effort / schema / prompt.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def __call__(
        self,
        tier,
        system,
        prompt,
        schema,
        *,
        cfg=None,
        conn=None,
        effort=None,
        cap_kind="daily",
        model_override=None,
    ):
        self.calls.append(
            SimpleNamespace(
                tier=tier,
                system=system,
                prompt=prompt,
                schema=schema,
                effort=effort,
                cap_kind=cap_kind,
                model_override=model_override,
            )
        )
        return self._handler(prompt)


def _seed_curatable(seed, first_seen, score, title):
    """Seed a cluster with a non-empty English article (a curate candidate)."""
    cid = seed.cluster(
        canonical_url="https://ex.com/%d" % next(_CANON),
        first_seen=first_seen,
        score=score,
        title=title,
    )
    seed.article(cid, text="Article body for %s." % title)
    return cid


def _done_out(**over):
    out = {
        "relevance_score": 8,
        "why_it_matters": "matters",
        "notes": ["x", "y"],
        "summary": "s",
        "channels": ["ai"],
        "novelty": "n",
        "audience": "a",
        "skip": False,
    }
    out.update(over)
    return out


def _insert(conn, table, **vals):
    cols = list(vals)
    conn.execute(
        "INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ",".join("?" for _ in cols)),
        [vals[c] for c in cols],
    )


def _ins_cluster(
    conn,
    cid,
    title="Story about AI",
    canonical=None,
    score=1.0,
    first_seen="2026-05-01T00:00:00+00:00",
):
    from signalpipe.dedup import story_id, title_key

    canonical = canonical if canonical is not None else "https://ex.com/%d" % cid
    tk = title_key(title)
    conn.execute(
        "INSERT INTO clusters(id, canonical_url, title, title_key, first_seen, "
        "last_seen, surface_count, score, story_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (cid, canonical, title, tk, first_seen, first_seen, 1, score, story_id(canonical, tk)),
    )


def _health(conn, level):
    return conn.execute(
        "SELECT message, stats FROM health WHERE job='backfill' AND level=? ORDER BY id",
        (level,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# _opus_model
# --------------------------------------------------------------------------- #
def test_opus_model_resolves_digest_tier(cfg):
    assert backfill._opus_model(cfg) == cfg.model_for("digest")
    assert backfill._opus_model(cfg) == "claude-opus-4-8"
    # OPUS_TIER routes writes to the subscription cloud path.
    assert backfill.OPUS_TIER == "write"


# --------------------------------------------------------------------------- #
# _days — half-open [since, until) date generator
# --------------------------------------------------------------------------- #
def test_days_empty_when_since_equals_until():
    assert list(backfill._days("2026-05-01", "2026-05-01")) == []


def test_days_empty_when_since_after_until():
    assert list(backfill._days("2026-05-05", "2026-05-01")) == []


def test_days_multi_day_is_half_open():
    got = list(backfill._days("2026-05-01", "2026-05-04"))
    assert got == [
        datetime.date(2026, 5, 1),
        datetime.date(2026, 5, 2),
        datetime.date(2026, 5, 3),
    ]  # exclusive of 2026-05-04


def test_days_crosses_month_boundary():
    got = list(backfill._days("2026-04-30", "2026-05-02"))
    assert got == [datetime.date(2026, 4, 30), datetime.date(2026, 5, 1)]


@pytest.mark.property
def test_days_property_half_open():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    @settings(max_examples=80, deadline=None)
    @given(
        st.dates(min_value=datetime.date(2000, 1, 1), max_value=datetime.date(2100, 1, 1)),
        st.integers(min_value=-5, max_value=45),
    )
    def inner(start, delta):
        end = start + datetime.timedelta(days=delta)
        got = list(backfill._days(start.isoformat(), end.isoformat()))
        if delta <= 0:
            assert got == []
        else:
            assert len(got) == delta
            assert got[0] == start
            assert all(start <= d < end for d in got)
            assert got == sorted(got)

    inner()


# --------------------------------------------------------------------------- #
# select_refetch_ids — per-day top_n clusters lacking article text
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_select_refetch_ids_selection_rules(conn, seed):
    fs = "2026-05-01T%02d:00:00+00:00"
    # selected: no article row at all
    a = seed.cluster(canonical_url="https://ex.com/a", first_seen=fs % 10, score=5.0)
    # selected: article present but text IS NULL
    b = seed.cluster(canonical_url="https://ex.com/b", first_seen=fs % 11, score=9.0)
    seed.article(b, text=None)
    # selected: article present but text == ''
    c = seed.cluster(canonical_url="https://ex.com/c", first_seen=fs % 9, score=3.0)
    seed.article(c, text="")
    # excluded: already has text
    d = seed.cluster(canonical_url="https://ex.com/d", first_seen=fs % 8, score=8.0)
    seed.article(d, text="Full body")
    # excluded: canonical_url IS NULL
    seed.cluster(canonical_url=None, first_seen=fs % 7, score=10.0)
    # excluded: outside the day window
    seed.cluster(
        canonical_url="https://ex.com/f", first_seen="2026-05-02T10:00:00+00:00", score=10.0
    )

    ids = backfill.select_refetch_ids(conn, "2026-05-01", "2026-05-02", top_n=40)
    # ordered by score DESC among the selected: b(9) > a(5) > c(3)
    assert ids == [b, a, c]


@pytest.mark.integration
def test_select_refetch_ids_top_n_per_day(conn, seed):
    fs = "2026-05-01T%02d:00:00+00:00"
    b = seed.cluster(canonical_url="https://ex.com/tb", first_seen=fs % 11, score=9.0)
    a = seed.cluster(canonical_url="https://ex.com/ta", first_seen=fs % 10, score=5.0)
    seed.cluster(canonical_url="https://ex.com/tc", first_seen=fs % 9, score=3.0)
    ids = backfill.select_refetch_ids(conn, "2026-05-01", "2026-05-02", top_n=2)
    assert ids == [b, a]  # only the top 2 by score for the day


@pytest.mark.integration
def test_select_refetch_ids_spans_days_in_order(conn, seed):
    d1 = seed.cluster(
        canonical_url="https://ex.com/d1", first_seen="2026-05-01T10:00:00+00:00", score=4.0
    )
    d2 = seed.cluster(
        canonical_url="https://ex.com/d2", first_seen="2026-05-02T10:00:00+00:00", score=9.0
    )
    ids = backfill.select_refetch_ids(conn, "2026-05-01", "2026-05-03", top_n=40)
    # day-01 ids first, then day-02 (extend per day, not globally sorted)
    assert ids == [d1, d2]


@pytest.mark.integration
def test_select_refetch_ids_empty_window(conn, seed):
    seed.cluster(
        canonical_url="https://ex.com/z", first_seen="2026-06-01T10:00:00+00:00", score=9.0
    )
    assert backfill.select_refetch_ids(conn, "2026-05-01", "2026-05-03", top_n=40) == []


# --------------------------------------------------------------------------- #
# _persist — one curation, stamped, taxonomy set; returns the skip flag
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_persist_done(conn, seed):
    title = "OpenAI ships a new GPT frontier model"
    cid = seed.cluster(canonical_url="https://ex.com/p1", title=title)
    c = {"id": cid, "title": title}
    when = "2026-05-01T00:00:00+00:00"

    skipped = backfill._persist(conn, c, _done_out(), "claude-opus-4-8", when)

    assert skipped is False
    row = conn.execute("SELECT * FROM curations WHERE cluster_id=?", (cid,)).fetchone()
    assert row["status"] == "done"
    assert row["tier_used"] == "opus-backfill"
    assert row["backend_used"] == "subscription"
    assert row["model_used"] == "claude-opus-4-8"
    assert row["relevance_score"] == 8
    assert row["why_it_matters"] == "matters"
    assert json.loads(row["notes"]) == ["x", "y"]
    assert row["summary"] == "s"
    assert json.loads(row["channels"]) == ["ai"]
    assert row["novelty"] == "n"
    assert row["audience"] == "a"
    assert row["skip"] == 0
    assert row["curated_at"] == when
    # deterministic taxonomy derived from the title + channels
    assert row["category"] == "ai"
    assert json.loads(row["subcategories"]) == ["models"]


@pytest.mark.integration
def test_persist_skip(conn, seed):
    cid = seed.cluster(canonical_url="https://ex.com/p2", title="Thin marketing rewrite")
    c = {"id": cid, "title": "Thin marketing rewrite"}
    when = "2026-05-02T00:00:00+00:00"
    out = {"skip": True, "skip_reason": "fluff", "channels": []}

    skipped = backfill._persist(conn, c, out, "claude-opus-4-8", when)

    assert skipped is True
    row = conn.execute(
        "SELECT status, skip, skip_reason, relevance_score, curated_at "
        "FROM curations WHERE cluster_id=?",
        (cid,),
    ).fetchone()
    assert row["status"] == "skipped"
    assert row["skip"] == 1
    assert row["skip_reason"] == "fluff"
    assert row["relevance_score"] == 0  # int(None or 0)
    assert row["curated_at"] == when


@pytest.mark.integration
def test_persist_updates_existing_pending_row(conn, seed):
    """INSERT OR IGNORE claims 'pending' then UPDATE finalizes it; a pre-existing
    pending claim is finalized in place (one row, not two)."""
    cid = seed.cluster(canonical_url="https://ex.com/p3", title="Rust release")
    conn.execute("INSERT INTO curations(cluster_id, status) VALUES(?, 'pending')", (cid,))
    backfill._persist(
        conn,
        {"id": cid, "title": "Rust release"},
        _done_out(),
        "claude-opus-4-8",
        "2026-05-03T00:00:00+00:00",
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM curations WHERE cluster_id=?", (cid,)).fetchone()[0] == 1
    )
    assert (
        conn.execute("SELECT status FROM curations WHERE cluster_id=?", (cid,)).fetchone()["status"]
        == "done"
    )


# --------------------------------------------------------------------------- #
# curate — all-Opus hindsight pass over each day's top clusters
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_curate_multiday_done_and_skipped(conn, seed, cfg, monkeypatch):
    a = _seed_curatable(seed, "2026-05-01T10:00:00+00:00", 9.0, "OpenAI ships GPT frontier model")
    b = _seed_curatable(seed, "2026-05-01T09:00:00+00:00", 5.0, "Marketing fluff nobody needs")
    c = _seed_curatable(seed, "2026-05-02T10:00:00+00:00", 7.0, "Rust compiler gets faster")

    def handler(prompt):
        if "fluff" in prompt.lower():
            return _done_out(skip=True, skip_reason="thin", channels=[])
        return _done_out()

    fake = FakeComplete(handler)
    monkeypatch.setattr(backfill.adapter, "complete", fake)

    # 05-03 has no candidates -> exercises the `if not rows: continue` branch.
    rc = backfill.curate(cfg, "2026-05-01", "2026-05-04", top_n=30)
    assert rc == 0

    # one Opus call per candidate, always the write tier + Opus override.
    assert len(fake.calls) == 3
    for call in fake.calls:
        assert call.tier == backfill.OPUS_TIER
        assert call.model_override == "claude-opus-4-8"
        assert call.effort == "low"
        assert call.schema is CURATION_SCHEMA
        assert call.system == SYSTEM_CURATE
        assert call.prompt.startswith(backfill._HINDSIGHT)

    # curated_at is stamped to the cluster's first_seen midnight (its real day).
    ra = conn.execute(
        "SELECT status, curated_at FROM curations WHERE cluster_id=?", (a,)
    ).fetchone()
    assert ra["status"] == "done"
    assert ra["curated_at"] == "2026-05-01T00:00:00+00:00"
    rb = conn.execute(
        "SELECT status, curated_at FROM curations WHERE cluster_id=?", (b,)
    ).fetchone()
    assert rb["status"] == "skipped"
    assert rb["curated_at"] == "2026-05-01T00:00:00+00:00"
    rc2 = conn.execute(
        "SELECT status, curated_at FROM curations WHERE cluster_id=?", (c,)
    ).fetchone()
    assert rc2["status"] == "done"
    assert rc2["curated_at"] == "2026-05-02T00:00:00+00:00"

    stats = json.loads(_health(conn, "info")[-1]["stats"])
    assert stats["done"] == 2
    assert stats["skipped"] == 1
    assert stats["failed"] == 0
    assert stats["days"] == 2
    assert stats["candidates"] == 3


@pytest.mark.integration
def test_curate_spend_cap_stops_batch(conn, seed, cfg, monkeypatch):
    _seed_curatable(seed, "2026-05-01T10:00:00+00:00", 9.0, "First story")
    _seed_curatable(seed, "2026-05-01T09:00:00+00:00", 5.0, "Second story")

    def handler(prompt):
        raise SpendCapExceeded("daily spend cap $10.00 reached")

    monkeypatch.setattr(backfill.adapter, "complete", FakeComplete(handler))

    rc = backfill.curate(cfg, "2026-05-01", "2026-05-02", top_n=30)

    assert rc == 1  # cap hit stops the whole batch
    assert conn.execute("SELECT COUNT(*) FROM curations").fetchone()[0] == 0
    warns = _health(conn, "warn")
    # exactly one warn: the batch stops on the FIRST item's cap, so the second
    # never runs. The logged message is the verbatim SpendCapExceeded text.
    assert len(warns) == 1
    assert warns[0]["message"] == "daily spend cap $10.00 reached"
    # the early return skips the terminal info log.
    assert _health(conn, "info") == []


@pytest.mark.integration
def test_curate_llm_error_marks_failed_and_continues(conn, seed, cfg, monkeypatch):
    h = _seed_curatable(seed, "2026-05-01T10:00:00+00:00", 9.0, "BOOM failing item")
    lo = _seed_curatable(seed, "2026-05-01T09:00:00+00:00", 5.0, "Rust compiler faster")

    def handler(prompt):
        if "BOOM" in prompt:
            raise LLMError("backend exploded")
        return _done_out(channels=["devtools"])

    monkeypatch.setattr(backfill.adapter, "complete", FakeComplete(handler))

    rc = backfill.curate(cfg, "2026-05-01", "2026-05-02", top_n=30)
    assert rc == 0

    # the surviving item is persisted; the failed one wrote no curation.
    assert (
        conn.execute("SELECT status FROM curations WHERE cluster_id=?", (lo,)).fetchone()["status"]
        == "done"
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM curations WHERE cluster_id=?", (h,)).fetchone()[0] == 0
    )

    stats = json.loads(_health(conn, "info")[-1]["stats"])
    assert stats["failed"] == 1
    assert stats["done"] == 1
    warns = _health(conn, "warn")
    # one warn, tagged with the failing item's id and carrying the (truncated)
    # backend error text verbatim.
    assert len(warns) == 1
    assert warns[0]["message"] == "item %d: backend exploded" % h


@pytest.mark.integration
def test_curate_dry_run_writes_nothing(conn, seed, cfg, monkeypatch, capsys):
    _seed_curatable(seed, "2026-05-01T10:00:00+00:00", 9.0, "OpenAI ships GPT frontier model")

    def handler(prompt):  # pragma: no cover - must never run in dry_run
        raise AssertionError("adapter.complete must not run in dry_run")

    monkeypatch.setattr(backfill.adapter, "complete", FakeComplete(handler))

    rc = backfill.curate(cfg, "2026-05-01", "2026-05-02", top_n=30, dry_run=True)
    assert rc == 0
    assert conn.execute("SELECT COUNT(*) FROM curations").fetchone()[0] == 0
    # dry_run touches no health rows either.
    assert conn.execute("SELECT COUNT(*) FROM health").fetchone()[0] == 0
    # Pin the dry-run-only per-day line (count + top score). The terminal
    # summary also contains the word "candidates" regardless of dry_run, so a
    # bare `"candidates" in out` would pass even if the dry-run branch were dead.
    assert "2026-05-01: 1 candidates (top score 9.0)" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# fetch — delegate to fetch_article.run over the selected ids
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_fetch_empty_ids_short_circuits(conn, cfg, monkeypatch):
    from signalpipe import fetch_article

    calls = []
    monkeypatch.setattr(backfill, "select_refetch_ids", lambda *a, **k: [])
    monkeypatch.setattr(
        fetch_article, "run", lambda *a, **k: calls.append((a, k)) or 99
    )  # pragma: no cover

    rc = backfill.fetch(cfg, "2026-05-01", "2026-05-02")
    assert rc == 0
    assert calls == []  # no ids -> fetch_article.run never invoked


@pytest.mark.integration
def test_fetch_delegates_selected_ids(conn, cfg, monkeypatch):
    from signalpipe import fetch_article

    captured = {}
    monkeypatch.setattr(backfill, "select_refetch_ids", lambda *a, **k: [11, 22, 33])

    def fake_run(cfg_arg, cluster_ids=None):
        captured["cfg"] = cfg_arg
        captured["ids"] = cluster_ids
        return len(cluster_ids)

    monkeypatch.setattr(fetch_article, "run", fake_run)

    rc = backfill.fetch(cfg, "2026-05-01", "2026-05-02", top_n=5)
    assert rc == 3
    assert captured["cfg"] is cfg
    assert captured["ids"] == [11, 22, 33]


# --------------------------------------------------------------------------- #
# merge — fold the copy's new rows back into the LIVE DB via ATTACH
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_merge_into_live(cfg, tmp_path):
    live_path = cfg.db_path
    src_path = tmp_path / "bf_src.db"

    # --- LIVE DB: clusters 1..4; a live curation + headline-only article for #1
    live = db_mod.connect_rw(live_path)
    for cid in (1, 2, 3, 4):
        _ins_cluster(live, cid)
    _insert(
        live,
        "curations",
        cluster_id=1,
        status="done",
        tier_used="triage",
        backend_used="subscription",
        model_used="claude-haiku-4-5",
        why_it_matters="LIVE ORIGINAL",
        skip=0,
        curated_at="2026-06-10T00:00:00+00:00",
    )
    _insert(
        live,
        "articles",
        cluster_id=1,
        source_url="https://ex.com/1",
        read_url="https://ex.com/1",
        read_kind="primary",
        paywalled=0,
        text="",
        lang="en",
        fetch_status="ok",
    )
    live.close()

    # --- SRC (backfill copy): same clusters + opus-backfill rows
    src = db_mod.connect_rw(src_path)
    for cid in (1, 2, 3, 4):
        _ins_cluster(src, cid)
    _insert(
        src,
        "curations",
        cluster_id=1,
        status="done",
        tier_used="opus-backfill",
        why_it_matters="BACKFILL 1",
        skip=0,
    )
    _insert(
        src,
        "curations",
        cluster_id=2,
        status="done",
        tier_used="opus-backfill",
        why_it_matters="BACKFILL 2",
        skip=0,
    )
    _insert(
        src,
        "curations",
        cluster_id=3,
        status="done",
        tier_used="triage",
        why_it_matters="not backfill",
        skip=0,
    )
    _insert(
        src,
        "curations",
        cluster_id=4,
        status="done",
        tier_used="opus-backfill",
        why_it_matters="BACKFILL 4",
        skip=0,
    )
    _insert(
        src,
        "articles",
        cluster_id=1,
        source_url="https://ex.com/1",
        read_url="https://ex.com/1",
        text="Recovered 1",
        lang="en",
        fetch_status="ok",
    )
    _insert(
        src,
        "articles",
        cluster_id=2,
        source_url="https://ex.com/2",
        read_url="https://ex.com/2",
        text="Recovered 2",
        lang="en",
        fetch_status="ok",
    )
    _insert(
        src,
        "articles",
        cluster_id=3,
        source_url="https://ex.com/3",
        read_url="https://ex.com/3",
        text="Recovered 3",
        lang="en",
        fetch_status="ok",
    )
    _insert(
        src,
        "articles",
        cluster_id=4,
        source_url="https://ex.com/4",
        read_url="https://ex.com/4",
        text="",
        lang="en",
        fetch_status="ok",
    )
    _insert(
        src,
        "digests",
        kind="daily",
        period_key="2026-05-01",
        window_start="2026-05-01T00:00:00+00:00",
        window_end="2026-05-02T00:00:00+00:00",
        generated_at="2026-05-02T00:00:00+00:00",
        model_used="claude-opus-4-8",
        title="May 1",
        blurb="b",
        body_md="md",
        body_html="<p>html</p>",
        cluster_ids=json.dumps([1, 2]),
        promoted=0,
    )
    _insert(
        src,
        "published_ledger",
        story_id="story-xyz",
        surface="daily",
        edition_key="2026-05-01",
        cluster_id=1,
        first_at="2026-05-02T00:00:00+00:00",
    )
    src.close()

    rc = backfill.merge(cfg, str(src_path))
    assert rc == 0

    out = db_mod.connect_ro(live_path)
    try:
        # INSERT OR IGNORE never overwrites the live curation for #1.
        c1 = out.execute(
            "SELECT why_it_matters, tier_used FROM curations WHERE cluster_id=1"
        ).fetchone()
        assert c1["why_it_matters"] == "LIVE ORIGINAL"
        assert c1["tier_used"] == "triage"
        # #2 gets the backfill curation (no live conflict).
        c2 = out.execute(
            "SELECT why_it_matters, tier_used FROM curations WHERE cluster_id=2"
        ).fetchone()
        assert c2["tier_used"] == "opus-backfill"
        assert c2["why_it_matters"] == "BACKFILL 2"
        # #3's src curation is NOT opus-backfill -> excluded.
        assert out.execute("SELECT COUNT(*) FROM curations WHERE cluster_id=3").fetchone()[0] == 0
        # #4's opus-backfill curation merges even though its text is empty.
        assert (
            out.execute("SELECT tier_used FROM curations WHERE cluster_id=4").fetchone()[
                "tier_used"
            ]
            == "opus-backfill"
        )

        # Articles: bounded to opus-backfill clusters WITH text.
        assert (
            out.execute("SELECT text FROM articles WHERE cluster_id=1").fetchone()["text"]
            == "Recovered 1"
        )  # replaced the empty live row
        assert (
            out.execute("SELECT text FROM articles WHERE cluster_id=2").fetchone()["text"]
            == "Recovered 2"
        )
        # #3 (not backfill) and #4 (empty text) contribute no article.
        assert out.execute("SELECT COUNT(*) FROM articles WHERE cluster_id=3").fetchone()[0] == 0
        assert out.execute("SELECT COUNT(*) FROM articles WHERE cluster_id=4").fetchone()[0] == 0

        assert out.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
        assert out.execute("SELECT COUNT(*) FROM published_ledger").fetchone()[0] == 1

        # counts recorded in the health log.
        msg = _health(out, "info")[-1]["message"]
        payload = json.loads(msg.split("merge -> live: ", 1)[1])
        assert payload == {"articles": 2, "curations": 2, "digests": 1, "ledger": 1}
    finally:
        out.close()


@pytest.mark.integration
def test_merge_detaches_bf_on_error(cfg, tmp_path, monkeypatch):
    """A failure inside the write tx must still DETACH the attached copy."""
    live_path = cfg.db_path
    src_path = tmp_path / "bf_src_err.db"
    db_mod.connect_rw(live_path).close()
    db_mod.connect_rw(src_path).close()

    orig_connect_rw = db_mod.connect_rw

    class RecordingConn:
        def __init__(self, real):
            self._real = real
            self.calls = []

        def execute(self, sql, *args):
            self.calls.append(sql)
            if "INSERT OR IGNORE INTO digests" in sql:
                raise RuntimeError("boom during digests insert")
            return self._real.execute(sql, *args)

        def commit(self):
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

        def close(self):
            return self._real.close()

    holder = {}

    def fake_connect_rw(path):
        conn = RecordingConn(orig_connect_rw(path))
        holder["conn"] = conn
        return conn

    monkeypatch.setattr(db_mod, "connect_rw", fake_connect_rw)

    with pytest.raises(RuntimeError):
        backfill.merge(cfg, str(src_path))

    calls = holder["conn"].calls
    assert any(s.startswith("ATTACH DATABASE") for s in calls)
    assert "DETACH DATABASE bf" in calls
    # DETACH ran AFTER the failing insert (i.e. in the finally, on the error).
    fail_at = max(i for i, s in enumerate(calls) if "INSERT OR IGNORE INTO digests" in s)
    assert calls.index("DETACH DATABASE bf") > fail_at
