"""Tests for :mod:`signalpipe.retag` — deterministic v2-taxonomy backfill.

``retag.run`` opens its OWN writer connection via ``connect_rw(cfg.db_path)``.
The shared ``cfg`` fixture repoints ``db_path`` at ``tmp_path/signal.db`` and the
``conn``/``seed`` fixtures use that exact same tmp file, so we seed rows through
``seed`` (committed autocommit writes), invoke ``retag.run(cfg, ...)`` (which reads
+ writes over a second connection to the same WAL DB), then assert the resulting
rows back through ``conn``. Real sqlite + tmp filesystem, zero network — the whole
module is integration-marked.

Expected taxonomy values are derived from the real ``topics.match_taxonomy`` (the
same function retag delegates to) so the assertions track the actual code path,
plus a couple of hard-coded category checks to pin down concrete behavior.
"""

from __future__ import annotations

import datetime
import json
import types

import pytest

import signalpipe.retag as retag
import signalpipe.topics as topics

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _curation(conn, cluster_id):
    return conn.execute(
        "SELECT category, subcategories FROM curations WHERE cluster_id=?",
        (cluster_id,),
    ).fetchone()


def _ledger_rows(conn):
    return conn.execute(
        "SELECT story_id, surface, edition_key, cluster_id, first_at "
        "FROM published_ledger ORDER BY story_id"
    ).fetchall()


def _ledger_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM published_ledger").fetchone()["n"]


def _freeze_retag_clock(monkeypatch, iso="2026-07-04T12:00:00+00:00"):
    """Freeze ``retag``'s ``datetime.datetime.now(...)`` used for ledger first_at.

    retag references ``datetime.datetime.now`` and ``datetime.timezone.utc`` off
    its module-level ``datetime`` import; datetime.datetime is a C type we can't
    setattr on, so we swap the whole module reference for a shim that returns a
    fixed instant while delegating ``timezone`` to the real module.
    """
    frozen = datetime.datetime.fromisoformat(iso)
    shim = types.SimpleNamespace(
        datetime=types.SimpleNamespace(now=lambda tz=None: frozen),
        timezone=datetime.timezone,
    )
    monkeypatch.setattr(retag, "datetime", shim)
    return iso


# Titles chosen so match_taxonomy lands on a distinct, non-trivial category each.
_ROW_SPECS = [
    ("https://ex.com/c1", "New frontier model benchmark from OpenAI", []),  # ai
    ("https://ex.com/c2", "Rust compiler gets much faster", ["devtools"]),  # software
    (
        "https://ex.com/c3",
        "CVE zero-day exploit in popular npm package",  # security
        ["security"],
    ),
]


def _seed_rows(seed, specs=_ROW_SPECS, category="SENTINEL", subcategories='["SENTINEL_SUB"]'):
    """Seed one cluster + one curation per spec; return list of (cid, title, chans)."""
    out = []
    for url, title, chans in specs:
        cid = seed.cluster(canonical_url=url, title=title)
        seed.curation(
            cluster_id=cid,
            channels=json.dumps(chans),
            category=category,
            subcategories=subcategories,
        )
        out.append((cid, title, chans))
    return out


# --------------------------------------------------------------------------- #
# Write path
# --------------------------------------------------------------------------- #
def test_run_write_updates_every_curation(cfg, conn, seed, capsys):
    rows = _seed_rows(seed)

    rc = retag.run(cfg)
    assert rc == 0

    for cid, title, chans in rows:
        expected = topics.match_taxonomy(title, chans)
        got = _curation(conn, cid)
        assert got["category"] == expected["category"]
        assert json.loads(got["subcategories"]) == expected["subcategories"]

    # Concrete pins so a silent taxonomy regression is caught, not just "agrees
    # with itself".
    assert _curation(conn, rows[0][0])["category"] == "ai"
    assert _curation(conn, rows[1][0])["category"] == "software"
    assert _curation(conn, rows[2][0])["category"] == "security"
    assert json.loads(_curation(conn, rows[2][0])["subcategories"]) == [
        "vulns",
        "research",
        "supplychain",
    ]

    out = capsys.readouterr().out
    assert "retag: 3 curations" in out
    assert "retag: wrote 3 rows" in out
    assert "by category:" in out


def test_run_returns_zero_and_seeds_empty_ledger_when_no_digests(cfg, conn, seed):
    rows = _seed_rows(seed, specs=[("https://ex.com/c1", "AI models from OpenAI", [])])
    rc = retag.run(cfg)
    assert rc == 0
    # The write path actually ran: the SENTINEL category was overwritten with the
    # concrete taxonomy ("AI models from OpenAI" -> ai, no subcategory terms hit).
    got = _curation(conn, rows[0][0])
    assert got["category"] == "ai"
    assert json.loads(got["subcategories"]) == []
    # No digests seeded -> ledger stays empty even on the write path.
    assert _ledger_count(conn) == 0


def test_run_malformed_channels_degrades_to_empty(cfg, conn, seed):
    # A row whose channels column is not valid JSON must NOT crash — it degrades
    # to [] and is categorized purely from its title.
    cid = seed.cluster(canonical_url="https://ex.com/bad", title="AI models from OpenAI")
    seed.curation(cluster_id=cid, channels="{not valid json", category="SENTINEL")

    rc = retag.run(cfg)
    assert rc == 0

    expected = topics.match_taxonomy("AI models from OpenAI", [])
    got = _curation(conn, cid)
    assert got["category"] == expected["category"]
    assert json.loads(got["subcategories"]) == expected["subcategories"]
    # Pin the concrete degraded result so a taxonomy regression can't move both
    # sides together: bad JSON -> [] channels -> title-only categorization -> ai.
    assert got["category"] == "ai"
    assert json.loads(got["subcategories"]) == []


def test_run_null_channels_treated_as_empty(cfg, conn, seed):
    # channels IS NULL -> `None or "[]"` -> [] (the falsy-coalesce branch).
    cid = seed.cluster(canonical_url="https://ex.com/nul", title="Quantum physics breakthrough")
    seed.curation(cluster_id=cid, channels=None, category="SENTINEL")

    rc = retag.run(cfg)
    assert rc == 0

    expected = topics.match_taxonomy("Quantum physics breakthrough", [])
    got = _curation(conn, cid)
    assert got["category"] == expected["category"]
    assert json.loads(got["subcategories"]) == expected["subcategories"]
    # Concrete pin: "quantum"/"physics" are research/science terms -> the research
    # category with a single "science" subcategory.
    assert got["category"] == "research"
    assert json.loads(got["subcategories"]) == ["science"]


def test_run_is_idempotent(cfg, conn, seed):
    rows = _seed_rows(seed)

    assert retag.run(cfg) == 0
    first = {
        cid: (r["category"], r["subcategories"]) for cid, *_ in rows for r in [_curation(conn, cid)]
    }
    # Guard the idempotency claim against a no-op regression: a run that never
    # wrote would leave every row at SENTINEL and STILL satisfy first == second.
    # Anchor the first run to the concrete taxonomy it must have produced.
    assert first[rows[0][0]][0] == "ai"
    assert first[rows[1][0]][0] == "software"
    assert first[rows[2][0]][0] == "security"
    assert "SENTINEL" not in {cat for cat, _ in first.values()}

    assert retag.run(cfg) == 0
    second = {
        cid: (r["category"], r["subcategories"]) for cid, *_ in rows for r in [_curation(conn, cid)]
    }

    assert first == second


def test_run_null_title_falls_back_without_crashing(cfg, conn, seed):
    # clusters.title is NOT NULL, but curations may still hand match_taxonomy an
    # empty title via `title or ""`; a whitespace-only title exercises the
    # no-signal fallback -> default "industry".
    cid = seed.cluster(canonical_url="https://ex.com/blank", title=" ")
    seed.curation(cluster_id=cid, channels=json.dumps([]), category="SENTINEL")

    assert retag.run(cfg) == 0
    got = _curation(conn, cid)
    assert got["category"] == topics.match_taxonomy(" ", [])["category"]
    # No-signal fallback pinned concretely: no title terms + no channels -> the
    # hard-coded default "industry", with no subcategories.
    assert got["category"] == "industry"
    assert json.loads(got["subcategories"]) == []


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #
def test_run_dry_run_writes_nothing(cfg, conn, seed, capsys):
    rows = _seed_rows(seed)
    # A digest that WOULD seed the ledger on the write path — dry-run must skip it.
    a = rows[0][0]
    conn.execute("UPDATE clusters SET story_id='s_dry' WHERE id=?", (a,))
    seed.digest(period_key="2026-W27", cluster_ids=json.dumps([a]))

    rc = retag.run(cfg, dry_run=True)
    assert rc == 0

    # Every curation column is untouched.
    for cid, *_ in rows:
        got = _curation(conn, cid)
        assert got["category"] == "SENTINEL"
        assert got["subcategories"] == '["SENTINEL_SUB"]'
    # And the ledger was never touched.
    assert _ledger_count(conn) == 0

    out = capsys.readouterr().out
    assert "top subcategories:" in out
    assert "examples:" in out
    assert "(dry-run: nothing written)" in out
    # The write-path lines must NOT appear.
    assert "retag: wrote" not in out
    assert "seeded ledger" not in out


def test_run_dry_run_distribution_output(cfg, seed, capsys):
    _seed_rows(
        seed,
        specs=[
            ("https://ex.com/a", "AI models from OpenAI", []),
            ("https://ex.com/b", "Anthropic ships a new Claude model", []),
        ],
    )
    rc = retag.run(cfg, dry_run=True)
    assert rc == 0

    out = capsys.readouterr().out
    assert "retag: 2 curations" in out
    assert "by category:" in out
    # Both rows land in ai, so ai is the ONLY category line: pin it as an actual
    # indented distribution row and assert every other category is absent, rather
    # than a bare "ai" substring that any incidental text could satisfy.
    assert "\n  ai" in out
    for other in ("software", "security", "research", "hardware", "industry"):
        assert other not in out


# --------------------------------------------------------------------------- #
# limit (post-fetch slice, NOT a SQL LIMIT)
# --------------------------------------------------------------------------- #
def test_run_limit_processes_only_first_k(cfg, conn, seed, capsys):
    specs = [
        ("https://ex.com/l1", "AI models from OpenAI", []),
        ("https://ex.com/l2", "AI models from OpenAI", []),
        ("https://ex.com/l3", "AI models from OpenAI", []),
    ]
    rows = _seed_rows(seed, specs=specs)  # all sentinel category

    rc = retag.run(cfg, limit=2)
    assert rc == 0

    updated = sum(1 for cid, *_ in rows if _curation(conn, cid)["category"] != "SENTINEL")
    assert updated == 2  # exactly `limit` rows re-tagged (order-agnostic)

    out = capsys.readouterr().out
    assert "retag: 3 curations" not in out
    assert "retag: 2 curations" in out
    assert "retag: wrote 2 rows" in out


def test_run_limit_zero_is_falsy_and_processes_all(cfg, conn, seed, capsys):
    specs = [
        ("https://ex.com/z1", "AI models from OpenAI", []),
        ("https://ex.com/z2", "AI models from OpenAI", []),
    ]
    rows = _seed_rows(seed, specs=specs)

    rc = retag.run(cfg, limit=0)  # `if limit:` is False -> no slice -> all rows
    assert rc == 0

    updated = sum(1 for cid, *_ in rows if _curation(conn, cid)["category"] != "SENTINEL")
    assert updated == 2
    assert "retag: 2 curations" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Ledger seeding
# --------------------------------------------------------------------------- #
def test_run_seeds_ledger_from_digests(cfg, conn, seed, capsys, monkeypatch):
    iso = _freeze_retag_clock(monkeypatch)
    a = seed.cluster(canonical_url="https://ex.com/a", title="Alpha story", story_id="s_alpha")
    b = seed.cluster(canonical_url="https://ex.com/b", title="Beta story", story_id="s_beta")
    c = seed.cluster(
        canonical_url="https://ex.com/c", title="Gamma story", story_id=None
    )  # no story_id -> skipped
    seed.digest(kind="weekly", period_key="2026-W27", cluster_ids=json.dumps([a, b, c]))

    rc = retag.run(cfg)
    assert rc == 0

    rows = _ledger_rows(conn)
    assert len(rows) == 2  # a + b; c skipped for lacking story_id
    by_story = {r["story_id"]: r for r in rows}
    assert set(by_story) == {"s_alpha", "s_beta"}

    ra = by_story["s_alpha"]
    assert ra["surface"] == "weekly"  # kind -> surface
    assert ra["edition_key"] == "2026-W27"  # period_key -> edition_key
    assert ra["cluster_id"] == a  # cluster id -> cluster_id
    assert ra["first_at"] == iso  # frozen clock

    assert "retag: seeded ledger with 2 edition-story rows" in capsys.readouterr().out


def test_run_ledger_skips_null_empty_malformed_and_missing(cfg, conn, seed, capsys):
    # digest with NULL cluster_ids: excluded by the `WHERE cluster_ids IS NOT NULL`.
    seed.digest(kind="daily", period_key="2026-07-01", cluster_ids=None)
    # empty JSON array -> no cids.
    seed.digest(kind="daily", period_key="2026-07-02", cluster_ids="[]")
    # malformed JSON -> guarded (ValueError) -> [].
    seed.digest(kind="daily", period_key="2026-07-03", cluster_ids="{oops")
    # valid ids but no such clusters -> fetchone() None -> skipped.
    seed.digest(kind="daily", period_key="2026-07-04", cluster_ids=json.dumps([9991, 9992]))

    rc = retag.run(cfg)
    assert rc == 0
    assert _ledger_count(conn) == 0
    assert "retag: seeded ledger with 0 edition-story rows" in capsys.readouterr().out


def test_run_ledger_insert_or_ignore_is_idempotent(cfg, conn, seed):
    a = seed.cluster(canonical_url="https://ex.com/a", title="Alpha story", story_id="s_alpha")
    seed.digest(kind="weekly", period_key="2026-W27", cluster_ids=json.dumps([a]))

    assert retag.run(cfg) == 0
    assert _ledger_count(conn) == 1
    first_at = _ledger_rows(conn)[0]["first_at"]

    # Second run rebuilds the same (story_id, surface, edition_key) PK -> the
    # INSERT OR IGNORE keeps exactly one row and does not churn first_at.
    assert retag.run(cfg) == 0
    rows = _ledger_rows(conn)
    assert len(rows) == 1
    assert rows[0]["first_at"] == first_at


def test_run_ledger_respects_preexisting_row(cfg, conn, seed):
    a = seed.cluster(canonical_url="https://ex.com/a", title="Alpha story", story_id="s_alpha")
    # A ledger row already published for this (story, surface, edition).
    seed.ledger(
        "s_alpha",
        "weekly",
        edition_key="2026-W27",
        cluster_id=a,
        first_at="2020-01-01T00:00:00+00:00",
    )
    seed.digest(kind="weekly", period_key="2026-W27", cluster_ids=json.dumps([a]))

    assert retag.run(cfg) == 0
    rows = _ledger_rows(conn)
    assert len(rows) == 1
    # INSERT OR IGNORE left the original first_at untouched.
    assert rows[0]["first_at"] == "2020-01-01T00:00:00+00:00"


def test_run_ledger_same_story_across_editions_is_two_rows(cfg, conn, seed):
    a = seed.cluster(canonical_url="https://ex.com/a", title="Alpha story", story_id="s_alpha")
    seed.digest(kind="weekly", period_key="2026-W26", cluster_ids=json.dumps([a]))
    seed.digest(kind="weekly", period_key="2026-W27", cluster_ids=json.dumps([a]))

    assert retag.run(cfg) == 0
    rows = _ledger_rows(conn)
    assert len(rows) == 2  # same story, distinct edition_key -> distinct PKs
    assert {r["edition_key"] for r in rows} == {"2026-W26", "2026-W27"}


# --------------------------------------------------------------------------- #
# Empty DB
# --------------------------------------------------------------------------- #
def test_run_empty_db(cfg, conn, capsys):
    rc = retag.run(cfg)
    assert rc == 0
    assert _ledger_count(conn) == 0
    out = capsys.readouterr().out
    assert "retag: 0 curations" in out
    assert "retag: wrote 0 rows" in out
    assert "retag: seeded ledger with 0 edition-story rows" in out


def test_run_empty_db_dry_run(cfg, capsys):
    rc = retag.run(cfg, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "retag: 0 curations" in out
    assert "(dry-run: nothing written)" in out
    assert "retag: wrote" not in out
