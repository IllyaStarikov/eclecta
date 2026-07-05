"""Tests for :mod:`signalpipe.digest` — multi-cadence Opus digest generation.

Every external boundary is faked or tmp:

* sqlite      — the shared ``conn``/``seed``/``cfg`` fixtures (a real tmp DB that
                ``cfg.db_path`` also points at, so ``run()``'s own ``connect_rw``
                connection reads the seeded rows).
* llm         — ``digest.adapter.complete_with_cost`` is monkeypatched on the
                module (the adapter holds its own reference); no CLI/API ever runs.
* filesystem  — ``cfg.staging_dir`` resolves under the autouse-redirected tmp state
                dir, so staged markdown never lands in the user's live state dir.
* clock       — ``run()`` is always driven with an explicit ``period`` so its window
                is derived from ``period.parse_period`` instead of the wall clock.

No network, ever.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

import signalpipe.digest as digest
import signalpipe.period as period_mod
import signalpipe.publish as publish_mod
from signalpipe.llm import LLMError, SpendCapExceeded, UsageLimitExhausted
from signalpipe.llm.schemas import STYLE_FALLBACK


# --------------------------------------------------------------------------- #
# Local helpers / doubles
# --------------------------------------------------------------------------- #
SINCE = "2026-06-26T00:00:00+00:00"
UNTIL = "2026-07-03T00:00:00+00:00"

DIGEST_OUT = {
    "title": "This week in tech",
    "body_md": "## Models\n\nBig models shipped. See [source](https://example.com/x).",
    "blurb": "The week in AI, distilled.",
}
# body_md citing an archive mirror — must be refused at generation time.
ARCHIVE_OUT = {
    "title": "Refused",
    "body_md": "Read more at https://archive.ph/abc123 for context.",
    "blurb": "b",
}


def _item_row(**over):
    """A row-like mapping as ``_gather`` would yield (dict supports ``r['k']``)."""
    row = dict(
        id=1,
        story_id="story-1",
        title="A title",
        relevance_score=9,
        why_it_matters="why it matters",
        notes='["note one", "note two"]',
        summary="a summary",
        channels='["ai"]',
        novelty="genuinely new",
        read_url="https://example.com/read",
        source_url="https://example.com/src",
    )
    row.update(over)
    return row


def _sub_row(**over):
    row = dict(
        kind="daily",
        period_key="2026-07-01",
        title="Daily one",
        body_md="Daily body text.",
    )
    row.update(over)
    return row


def _fake_adapter(out=None, cost=0.12, raises=None):
    """Build a stand-in for ``adapter.complete_with_cost`` recording its calls."""
    calls = []

    def fake(tier, system, prompt, schema, *, cfg=None, conn=None,
             effort=None, cap_kind="daily", model_override=None):
        calls.append(types.SimpleNamespace(
            tier=tier, system=system, prompt=prompt, schema=schema,
            effort=effort, cap_kind=cap_kind))
        if raises is not None:
            raise raises
        return (dict(out or DIGEST_OUT), cost)

    fake.calls = calls
    return fake


def _boom_adapter():
    def fake(*a, **k):  # pragma: no cover - only hit on a bug
        raise AssertionError("adapter.complete_with_cost must not be called")
    return fake


class _FakeDocCfg:
    """Minimal cfg stand-in for ``_load_style`` / ``_load_editorial`` (they use
    only ``cfg.repo_path(rel)`` and ``cfg.blog_repo``)."""

    def __init__(self, repo_dir, blog_dir):
        self._repo = repo_dir
        self.blog_repo = blog_dir

    def repo_path(self, rel):
        return self._repo / rel


def _write_doc(base, rel, text):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _digest_count(conn):
    return conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]


def _digest_row(conn, kind="weekly", key="2026-W27"):
    return conn.execute(
        "SELECT * FROM digests WHERE kind=? AND period_key=?", (kind, key)
    ).fetchone()


def _seed_window_curation(seed, conn, **cur_over):
    """Seed a cluster + in-window done curation, return (cluster_id, story_id)."""
    cid = seed.cluster(canonical_url="https://example.com/story-%d"
                       % cur_over.pop("_n", 1),
                       title=cur_over.pop("_title", "A curated story"))
    seed.article(cluster_id=cid,
                 read_url="https://example.com/read",
                 source_url="https://example.com/src")
    over = dict(curated_at=SINCE, relevance_score=8, status="done", skip=0)
    over.update(cur_over)
    seed.curation(cluster_id=cid, **over)
    story = conn.execute("SELECT story_id FROM clusters WHERE id=?",
                         (cid,)).fetchone()["story_id"]
    return cid, story


# --------------------------------------------------------------------------- #
# LOWER_TIERS constant
# --------------------------------------------------------------------------- #
def test_lower_tiers_mapping():
    assert digest.LOWER_TIERS == {
        "monthly": ("weekly", "daily"),
        "quarterly": ("monthly",),
        "yearly": ("quarterly",),
    }
    # daily/weekly are leaf cadences and must NOT be hierarchical.
    assert "daily" not in digest.LOWER_TIERS
    assert "weekly" not in digest.LOWER_TIERS


# --------------------------------------------------------------------------- #
# _items_payload
# --------------------------------------------------------------------------- #
def test_items_payload_parses_json_and_maps_fields():
    row = _item_row()
    (out,) = digest._items_payload([row])
    assert out == {
        "title": "A title",
        "relevance": 9,
        "why": "why it matters",
        "notes": ["note one", "note two"],
        "summary": "a summary",
        "channels": ["ai"],
        "novelty": "genuinely new",
        "url": "https://example.com/read",
    }


def test_items_payload_null_notes_and_channels_default_to_empty_lists():
    row = _item_row(notes=None, channels=None)
    (out,) = digest._items_payload([row])
    assert out["notes"] == []
    assert out["channels"] == []


def test_items_payload_url_prefers_read_then_source_then_empty():
    # read_url normal -> read_url wins
    (a,) = digest._items_payload([_item_row(
        read_url="https://a.example/x", source_url="https://b.example/y")])
    assert a["url"] == "https://a.example/x"
    # read_url is an archive mirror -> scrubbed, falls back to source_url
    (b,) = digest._items_payload([_item_row(
        read_url="https://archive.ph/zzz", source_url="https://b.example/y")])
    assert b["url"] == "https://b.example/y"
    # both archive.* -> ""
    (c,) = digest._items_payload([_item_row(
        read_url="https://archive.ph/zzz", source_url="https://archive.today/q")])
    assert c["url"] == ""
    # both NULL -> ""
    (d,) = digest._items_payload([_item_row(read_url=None, source_url=None)])
    assert d["url"] == ""
    # empty read_url (falsy) falls through to source_url
    (e,) = digest._items_payload([_item_row(
        read_url="", source_url="https://b.example/y")])
    assert e["url"] == "https://b.example/y"


# --------------------------------------------------------------------------- #
# _build_prompt
# --------------------------------------------------------------------------- #
def test_build_prompt_items_only_label_and_header():
    p = digest._build_prompt("weekly", "2026-W27", SINCE, UNTIL,
                             [_item_row()], [])
    assert p.startswith("PERIOD: weekly 2026-W27 (2026-06-26 .. 2026-07-03)")
    assert "CURATED ITEMS (JSON, best first):" in p
    assert "TOP CURATIONS" not in p
    assert "LOWER-TIER DIGESTS" not in p
    # payload is embedded as JSON
    assert '"title": "A title"' in p
    assert '"why": "why it matters"' in p
    assert "note one" in p


def test_build_prompt_header_slices_since_and_until_to_ten_chars():
    # Full ISO timestamps in, only the YYYY-MM-DD date out.
    p = digest._build_prompt("daily", "2026-07-02",
                             "2026-07-01T00:00:00+00:00",
                             "2026-07-02T00:00:00+00:00",
                             [_item_row()], [])
    assert p.startswith("PERIOD: daily 2026-07-02 (2026-07-01 .. 2026-07-02)")


def test_build_prompt_items_plus_subdigests_switches_label_and_adds_section():
    subs = [_sub_row(kind="daily", period_key="2026-07-01",
                     title="D1", body_md="body1")]
    p = digest._build_prompt("weekly", "2026-W27", SINCE, UNTIL,
                             [_item_row()], subs)
    assert "TOP CURATIONS IN THE PERIOD (JSON, best first):" in p
    assert "CURATED ITEMS" not in p
    assert "LOWER-TIER DIGESTS IN THE PERIOD (oldest first):" in p
    assert "### [daily 2026-07-01] D1" in p
    assert "body1" in p


def test_build_prompt_subdigests_only_no_curation_label():
    subs = [_sub_row(title="D1", body_md="body1")]
    p = digest._build_prompt("monthly", "2026-06", SINCE, UNTIL, [], subs)
    assert "CURATED ITEMS" not in p
    assert "TOP CURATIONS" not in p
    assert "### [daily 2026-07-01] D1" in p


def test_build_prompt_subdigest_title_and_body_fallbacks():
    subs = [_sub_row(title=None, body_md=None)]
    p = digest._build_prompt("monthly", "2026-06", SINCE, UNTIL, [], subs)
    assert "### [daily 2026-07-01] (untitled)" in p


def test_build_prompt_empty_is_header_only():
    p = digest._build_prompt("weekly", "2026-W27", SINCE, UNTIL, [], [])
    assert p == "PERIOD: weekly 2026-W27 (2026-06-26 .. 2026-07-03)"


# --------------------------------------------------------------------------- #
# _staged_markdown  (patch digest.datetime so date.today() is frozen)
# --------------------------------------------------------------------------- #
def test_staged_markdown_frontmatter_layout(monkeypatch):
    import datetime as _dt

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(2026, 7, 4)

    monkeypatch.setattr(digest, "datetime",
                        types.SimpleNamespace(date=_FixedDate))

    md = digest._staged_markdown(
        "This week in tech", "A standfirst.", "## Body\n\nText.", "2026-W27")

    assert "NAME         Signal Digest 2026-W27" in md
    assert "PROJECT      Signal" in md
    assert "D.CREATED    2026-07-04" in md
    assert "D.MODIFIED   2026-07-04" in md
    assert "VERSION      1.0.0" in md
    assert "TAGS         #Signal" in md
    # the comment block is a promote-compatible frontmatter header
    assert md.startswith("<!--\n")
    assert "-->\n\n# This week in tech\n\n> *A standfirst.*\n\n## Body\n\nText.\n" in md


# --------------------------------------------------------------------------- #
# _load_style / _load_editorial
# --------------------------------------------------------------------------- #
def test_load_style_prefers_repo_doc(tmp_path):
    repo = tmp_path / "repo"
    blog = tmp_path / "blog"
    _write_doc(repo, digest.STYLE_DOC, "REPO STYLE GUIDE")
    _write_doc(blog, digest.STYLE_DOC, "BLOG STYLE GUIDE")
    cfg = _FakeDocCfg(repo, blog)
    assert digest._load_style(cfg) == "REPO STYLE GUIDE"


def test_load_style_falls_back_to_blog_repo(tmp_path):
    repo = tmp_path / "repo"
    blog = tmp_path / "blog"
    repo.mkdir()
    _write_doc(blog, digest.STYLE_DOC, "BLOG STYLE GUIDE")
    cfg = _FakeDocCfg(repo, blog)
    assert digest._load_style(cfg) == "BLOG STYLE GUIDE"


def test_load_style_empty_doc_is_skipped(tmp_path):
    repo = tmp_path / "repo"
    blog = tmp_path / "blog"
    _write_doc(repo, digest.STYLE_DOC, "   \n\t")  # whitespace-only -> skipped
    _write_doc(blog, digest.STYLE_DOC, "BLOG STYLE GUIDE")
    cfg = _FakeDocCfg(repo, blog)
    assert digest._load_style(cfg) == "BLOG STYLE GUIDE"


def test_load_style_none_present_returns_fallback(tmp_path):
    repo = tmp_path / "repo"
    blog = tmp_path / "blog"
    repo.mkdir()
    blog.mkdir()
    cfg = _FakeDocCfg(repo, blog)
    assert digest._load_style(cfg) == STYLE_FALLBACK


def test_load_editorial_prefers_repo_then_blog_then_empty(tmp_path):
    repo = tmp_path / "repo"
    blog = tmp_path / "blog"
    # repo present -> wins
    _write_doc(repo, digest.EDITORIAL_DOC, "REPO POLICY")
    _write_doc(blog, digest.EDITORIAL_DOC, "BLOG POLICY")
    assert digest._load_editorial(_FakeDocCfg(repo, blog)) == "REPO POLICY"

    # blog only
    repo2 = tmp_path / "repo2"
    blog2 = tmp_path / "blog2"
    repo2.mkdir()
    _write_doc(blog2, digest.EDITORIAL_DOC, "BLOG POLICY")
    assert digest._load_editorial(_FakeDocCfg(repo2, blog2)) == "BLOG POLICY"

    # neither -> "" (NOT STYLE_FALLBACK)
    repo3 = tmp_path / "repo3"
    blog3 = tmp_path / "blog3"
    repo3.mkdir()
    blog3.mkdir()
    assert digest._load_editorial(_FakeDocCfg(repo3, blog3)) == ""


def test_load_editorial_empty_repo_doc_is_skipped(tmp_path):
    repo = tmp_path / "repo"
    blog = tmp_path / "blog"
    _write_doc(repo, digest.EDITORIAL_DOC, "  \n\t")  # whitespace-only -> skipped
    _write_doc(blog, digest.EDITORIAL_DOC, "BLOG POLICY")
    assert digest._load_editorial(_FakeDocCfg(repo, blog)) == "BLOG POLICY"


# --------------------------------------------------------------------------- #
# _gather  (real sqlite)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_gather_returns_in_window_done_curation(conn, seed):
    cid, story = _seed_window_curation(seed, conn)
    rows = digest._gather(conn, SINCE, UNTIL, 7, 40, "weekly", "2026-W27")
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == cid
    assert r["story_id"] == story
    assert r["relevance_score"] == 8
    assert r["read_url"] == "https://example.com/read"


@pytest.mark.integration
def test_gather_excludes_below_relevance_and_out_of_window(conn, seed):
    # in window, high relevance -> kept
    _seed_window_curation(seed, conn, _n=1, _title="keep me", relevance_score=9,
                          curated_at=SINCE)
    # in window but below min_relevance -> dropped
    _seed_window_curation(seed, conn, _n=2, _title="too low", relevance_score=6,
                          curated_at=SINCE)
    # high relevance but curated BEFORE the window -> dropped
    _seed_window_curation(seed, conn, _n=3, _title="too early", relevance_score=9,
                          curated_at="2026-06-01T00:00:00+00:00")
    # high relevance but curated AT `until` (half-open, excluded) -> dropped
    _seed_window_curation(seed, conn, _n=4, _title="too late", relevance_score=9,
                          curated_at=UNTIL)
    rows = digest._gather(conn, SINCE, UNTIL, 7, 40, "weekly", "2026-W27")
    assert [r["title"] for r in rows] == ["keep me"]


@pytest.mark.integration
def test_gather_excludes_skipped_and_non_done(conn, seed):
    _seed_window_curation(seed, conn, _n=1, _title="skipped", skip=1)
    _seed_window_curation(seed, conn, _n=2, _title="pending", status="pending")
    rows = digest._gather(conn, SINCE, UNTIL, 7, 40, "weekly", "2026-W27")
    assert rows == []


@pytest.mark.integration
def test_gather_ordered_by_relevance_then_curated_at_desc(conn, seed):
    _seed_window_curation(seed, conn, _n=1, _title="mid", relevance_score=8,
                          curated_at=SINCE)
    _seed_window_curation(seed, conn, _n=2, _title="top", relevance_score=10,
                          curated_at=SINCE)
    _seed_window_curation(seed, conn, _n=3, _title="late-high", relevance_score=10,
                          curated_at="2026-07-02T00:00:00+00:00")
    rows = digest._gather(conn, SINCE, UNTIL, 7, 40, "weekly", "2026-W27")
    # relevance DESC, then curated_at DESC within the same relevance
    assert [r["title"] for r in rows] == ["late-high", "top", "mid"]


@pytest.mark.integration
def test_gather_limit_honored(conn, seed):
    for n in range(5):
        _seed_window_curation(seed, conn, _n=n, _title="story %d" % n,
                              relevance_score=9, curated_at=SINCE)
    rows = digest._gather(conn, SINCE, UNTIL, 7, 2, "weekly", "2026-W27")
    assert len(rows) == 2


@pytest.mark.integration
def test_gather_excludes_story_from_prior_edition(conn, seed):
    cid, story = _seed_window_curation(seed, conn)
    # same cadence, DIFFERENT edition already ran this story -> excluded
    seed.ledger(story, "weekly", edition_key="2026-W26", cluster_id=cid)
    rows = digest._gather(conn, SINCE, UNTIL, 7, 40, "weekly", "2026-W27")
    assert rows == []


@pytest.mark.integration
def test_gather_keeps_story_from_same_edition(conn, seed):
    cid, story = _seed_window_curation(seed, conn)
    # SAME edition key -> --force regeneration keeps its own stories
    seed.ledger(story, "weekly", edition_key="2026-W27", cluster_id=cid)
    rows = digest._gather(conn, SINCE, UNTIL, 7, 40, "weekly", "2026-W27")
    assert [r["id"] for r in rows] == [cid]


@pytest.mark.integration
def test_gather_prior_edition_filter_is_per_cadence(conn, seed):
    # A prior DAILY edition ran the story; a WEEKLY gather must still keep it
    # (the de-dup filter is scoped to surface=kind).
    cid, story = _seed_window_curation(seed, conn)
    seed.ledger(story, "daily", edition_key="2026-07-01", cluster_id=cid)
    rows = digest._gather(conn, SINCE, UNTIL, 7, 40, "weekly", "2026-W27")
    assert [r["id"] for r in rows] == [cid]


# --------------------------------------------------------------------------- #
# _gather_subdigests  (real sqlite)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_gather_subdigests_window_filter_and_order(conn, seed):
    since = "2026-06-01T00:00:00+00:00"
    until = "2026-07-01T00:00:00+00:00"
    # in-window weekly (later) and daily (earlier)
    seed.digest(kind="weekly", period_key="2026-W24",
                window_start="2026-06-10T00:00:00+00:00")
    seed.digest(kind="daily", period_key="2026-06-05",
                window_start="2026-06-05T00:00:00+00:00")
    # out of window: before `since`
    seed.digest(kind="daily", period_key="2026-05-20",
                window_start="2026-05-20T00:00:00+00:00")
    # out of window: at/after `until` (half-open)
    seed.digest(kind="daily", period_key="2026-07-01",
                window_start=until)
    # wrong tier for a monthly consumer (monthly not in LOWER_TIERS['monthly'])
    seed.digest(kind="monthly", period_key="2026-05",
                window_start="2026-06-15T00:00:00+00:00")

    rows = digest._gather_subdigests(conn, "monthly", since, until)
    # ordered by window_start, then kind — only the two in-window lower tiers
    assert [(r["kind"], r["period_key"]) for r in rows] == [
        ("daily", "2026-06-05"),
        ("weekly", "2026-W24"),
    ]


@pytest.mark.integration
def test_gather_subdigests_quarterly_consumes_only_monthly(conn, seed):
    since = "2026-04-01T00:00:00+00:00"
    until = "2026-07-01T00:00:00+00:00"
    seed.digest(kind="monthly", period_key="2026-04",
                window_start="2026-04-01T00:00:00+00:00")
    # weekly is NOT a lower tier of quarterly -> excluded
    seed.digest(kind="weekly", period_key="2026-W18",
                window_start="2026-04-27T00:00:00+00:00")
    rows = digest._gather_subdigests(conn, "quarterly", since, until)
    assert [r["kind"] for r in rows] == ["monthly"]


# --------------------------------------------------------------------------- #
# run() — control-flow return codes
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_unknown_kind_returns_2(cfg, capsys):
    rc = digest.run(cfg, kind="hourly", period=None)
    assert rc == 2
    assert "unknown digest kind" in capsys.readouterr().out


@pytest.mark.integration
def test_run_no_items_and_no_subdigests_returns_1(cfg, conn, monkeypatch, capsys):
    monkeypatch.setattr(digest.adapter, "complete_with_cost", _boom_adapter())
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 1
    out = capsys.readouterr().out
    assert "no curated items" in out and "nothing to digest" in out
    assert _digest_count(conn) == 0


@pytest.mark.integration
def test_run_existing_full_window_skips(cfg, conn, seed, monkeypatch, capsys):
    _, until = period_mod.parse_period("weekly", "2026-W27")
    seed.digest(kind="weekly", period_key="2026-W27", window_end=until,
                title="OLD TITLE")
    monkeypatch.setattr(digest.adapter, "complete_with_cost", _boom_adapter())
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 0
    assert "exists (use --force to regenerate)" in capsys.readouterr().out
    # unchanged: still the seeded row, adapter never fired
    assert _digest_count(conn) == 1
    assert _digest_row(conn)["title"] == "OLD TITLE"


@pytest.mark.integration
def test_run_existing_shorter_window_regenerates(cfg, conn, seed, monkeypatch,
                                                 capsys):
    since, until = period_mod.parse_period("weekly", "2026-W27")
    # existing row covers a SHORTER window (ends at `since`, < due `until`)
    seed.digest(kind="weekly", period_key="2026-W27", window_end=since,
                title="OLD TITLE")
    _seed_window_curation(seed, conn, curated_at=since)
    fake = _fake_adapter(DIGEST_OUT, 0.20)
    monkeypatch.setattr(digest.adapter, "complete_with_cost", fake)

    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 0
    assert "regenerating with the full window" in capsys.readouterr().out
    assert len(fake.calls) == 1
    row = _digest_row(conn)
    assert row["title"] == "This week in tech"     # upserted
    assert row["window_end"] == until              # now the full window


@pytest.mark.integration
def test_run_without_period_derives_key_and_window_from_today(cfg, conn, seed,
                                                              monkeypatch):
    import datetime as _dt

    run_date = _dt.date(2026, 7, 3)

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls):
            return run_date

    # Freeze only digest's clock; period_mod uses its own datetime for the math.
    monkeypatch.setattr(digest, "datetime", types.SimpleNamespace(
        date=_FixedDate, datetime=_dt.datetime, timezone=_dt.timezone))

    key = period_mod.period_key("weekly", run_date)
    since, until = period_mod.window("weekly", run_date)
    _seed_window_curation(seed, conn, curated_at=since)
    fake = _fake_adapter(DIGEST_OUT, 0.12)
    monkeypatch.setattr(digest.adapter, "complete_with_cost", fake)

    rc = digest.run(cfg, kind="weekly")  # no explicit period -> uses today
    assert rc == 0
    row = _digest_row(conn, key=key)
    assert row is not None
    assert row["window_start"] == since
    assert row["window_end"] == until


@pytest.mark.integration
def test_run_force_regenerates_full_window(cfg, conn, seed, monkeypatch):
    since, until = period_mod.parse_period("weekly", "2026-W27")
    seed.digest(kind="weekly", period_key="2026-W27", window_end=until,
                title="OLD TITLE")
    _seed_window_curation(seed, conn, curated_at=since)
    fake = _fake_adapter(DIGEST_OUT, 0.11)
    monkeypatch.setattr(digest.adapter, "complete_with_cost", fake)

    rc = digest.run(cfg, kind="weekly", period="2026-W27", force=True)
    assert rc == 0
    assert len(fake.calls) == 1
    assert _digest_row(conn)["title"] == "This week in tech"


# --------------------------------------------------------------------------- #
# run() — weekly happy path
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_weekly_happy_path(cfg, conn, seed, monkeypatch):
    since, until = period_mod.parse_period("weekly", "2026-W27")
    cid, story = _seed_window_curation(seed, conn, curated_at=since,
                                       relevance_score=9)
    fake = _fake_adapter(DIGEST_OUT, 0.12)
    monkeypatch.setattr(digest.adapter, "complete_with_cost", fake)

    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 0

    # ----- adapter was driven with the composed system + period prompt -----
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.tier == "digest"
    assert call.cap_kind == "digest"
    assert call.effort == "max"
    assert call.prompt.startswith("PERIOD: weekly 2026-W27")
    assert "You are writing a 'Signal' digest" in call.system

    # ----- digests row upserted -----
    row = _digest_row(conn)
    assert row is not None
    assert row["title"] == "This week in tech"
    assert row["blurb"] == "The week in AI, distilled."
    assert row["body_md"] == DIGEST_OUT["body_md"]
    assert row["window_start"] == since
    assert row["window_end"] == until
    assert row["model_used"] == "claude-opus-4-8"
    assert abs(row["cost_usd"] - 0.12) < 1e-9
    assert json.loads(row["cluster_ids"]) == [cid]
    # markdown IS installed here, so the real renderer runs (## -> <h2>)
    assert "<h2>Models</h2>" in row["body_html"]

    # ----- published_ledger records this edition's story -----
    ledger = conn.execute(
        "SELECT story_id, surface, edition_key, cluster_id FROM published_ledger"
    ).fetchall()
    assert len(ledger) == 1
    assert ledger[0]["story_id"] == story
    assert ledger[0]["surface"] == "weekly"
    assert ledger[0]["edition_key"] == "2026-W27"
    assert ledger[0]["cluster_id"] == cid

    # ----- staged markdown written under the tmp staging dir -----
    staged = cfg.staging_dir / "signal_digest_2026_w27.md"
    assert row["staged_path"] == str(staged)
    assert staged.exists()
    body = staged.read_text()
    assert "# This week in tech" in body
    assert "NAME         Signal Digest 2026-W27" in body
    assert DIGEST_OUT["body_md"] in body

    # ----- run bookkeeping: health info + run attribution + last_run -----
    assert conn.execute(
        "SELECT COUNT(*) FROM health WHERE job='digest' AND level='info'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM runs WHERE job='digest'"
    ).fetchone()[0] == 1
    assert cfg.data["last_run"]["job"] == "digest"
    assert cfg.data["last_run"]["stats"]["items"] == 1


@pytest.mark.integration
def test_run_weekly_excerpt_falls_back_when_no_blurb(cfg, conn, seed, monkeypatch):
    since, _ = period_mod.parse_period("weekly", "2026-W27")
    _seed_window_curation(seed, conn, curated_at=since)
    out = dict(DIGEST_OUT)
    out.pop("blurb")  # no standfirst -> excerpt uses the "best of..." fallback
    monkeypatch.setattr(digest.adapter, "complete_with_cost",
                        _fake_adapter(out, 0.05))
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 0
    staged = cfg.staging_dir / "signal_digest_2026_w27.md"
    assert "The best of technology and AI, weekly 2026-W27." in staged.read_text()
    # blurb column stored empty
    assert (_digest_row(conn)["blurb"] or "") == ""


# --------------------------------------------------------------------------- #
# run() — hierarchical (monthly) path over sub-digests, no direct items
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_monthly_over_subdigests_only(cfg, conn, seed, monkeypatch):
    since, until = period_mod.parse_period("monthly", "2026-06")
    # a lower-tier weekly whose window starts inside June, but NO curations
    seed.digest(kind="weekly", period_key="2026-W24",
                window_start="2026-06-10T00:00:00+00:00",
                window_end="2026-06-17T00:00:00+00:00",
                title="Week 24", body_md="## Wk24\n\nStuff happened.")
    fake = _fake_adapter(DIGEST_OUT, 0.30)
    monkeypatch.setattr(digest.adapter, "complete_with_cost", fake)

    rc = digest.run(cfg, kind="monthly", period="2026-06")
    assert rc == 0
    # the prompt carried the sub-digest, not curated items
    prompt = fake.calls[0].prompt
    assert "LOWER-TIER DIGESTS IN THE PERIOD" in prompt
    assert "### [weekly 2026-W24] Week 24" in prompt
    assert "CURATED ITEMS" not in prompt and "TOP CURATIONS" not in prompt

    row = _digest_row(conn, kind="monthly", key="2026-06")
    assert row is not None
    assert row["window_start"] == since
    assert row["window_end"] == until
    assert json.loads(row["cluster_ids"]) == []      # no direct items
    # no items -> no new ledger rows written
    assert conn.execute(
        "SELECT COUNT(*) FROM published_ledger").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# run() — archive refusal gate
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_refuses_archive_body(cfg, conn, seed, monkeypatch, capsys):
    since, _ = period_mod.parse_period("weekly", "2026-W27")
    _seed_window_curation(seed, conn, curated_at=since)
    monkeypatch.setattr(digest.adapter, "complete_with_cost",
                        _fake_adapter(ARCHIVE_OUT, 0.09))
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 1
    assert "REFUSED" in capsys.readouterr().out
    # nothing stored, nothing staged
    assert _digest_count(conn) == 0
    assert not (cfg.staging_dir / "signal_digest_2026_w27.md").exists()
    health = conn.execute(
        "SELECT level, message FROM health WHERE job='digest'").fetchone()
    assert health["level"] == "error"
    assert "archive link" in health["message"]


# --------------------------------------------------------------------------- #
# run() — adapter exception branches
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_usage_limit_defers_with_warn(cfg, conn, seed, monkeypatch, capsys):
    since, _ = period_mod.parse_period("weekly", "2026-W27")
    _seed_window_curation(seed, conn, curated_at=since)
    monkeypatch.setattr(
        digest.adapter, "complete_with_cost",
        _fake_adapter(raises=UsageLimitExhausted("quota exhausted")))
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 1
    assert "digest deferred" in capsys.readouterr().out
    assert _digest_count(conn) == 0
    health = conn.execute(
        "SELECT level, message FROM health WHERE job='digest'").fetchone()
    assert health["level"] == "warn"
    assert "quota exhausted" in health["message"]


@pytest.mark.integration
@pytest.mark.parametrize("exc", [
    LLMError("backend blew up"),
    SpendCapExceeded("digest cap reached"),
])
def test_run_llm_errors_return_1_and_log_error(cfg, conn, seed, monkeypatch,
                                               capsys, exc):
    since, _ = period_mod.parse_period("weekly", "2026-W27")
    _seed_window_curation(seed, conn, curated_at=since)
    monkeypatch.setattr(digest.adapter, "complete_with_cost",
                        _fake_adapter(raises=exc))
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 1
    assert "digest failed" in capsys.readouterr().out
    assert _digest_count(conn) == 0
    health = conn.execute(
        "SELECT level, message FROM health WHERE job='digest'").fetchone()
    assert health["level"] == "error"
    assert str(exc) in health["message"]


# --------------------------------------------------------------------------- #
# run() — markdown-missing fallback (<pre> body)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_markdown_missing_falls_back_to_pre(cfg, conn, seed, monkeypatch):
    since, _ = period_mod.parse_period("weekly", "2026-W27")
    _seed_window_curation(seed, conn, curated_at=since)
    # Force `import markdown` to raise ImportError inside run().
    monkeypatch.setitem(sys.modules, "markdown", None)
    monkeypatch.setattr(digest.adapter, "complete_with_cost",
                        _fake_adapter(DIGEST_OUT, 0.07))
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 0
    row = _digest_row(conn)
    assert row["body_html"] == "<pre>%s</pre>" % row["body_md"]
    assert "## Models" in row["body_html"]  # raw markdown preserved verbatim


# --------------------------------------------------------------------------- #
# run() — site publish hook (cfg.site.push)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_run_publishes_when_site_push_enabled(cfg, conn, seed, monkeypatch):
    since, _ = period_mod.parse_period("weekly", "2026-W27")
    _seed_window_curation(seed, conn, curated_at=since)
    cfg.data["site"]["push"] = True
    calls = []
    monkeypatch.setattr(publish_mod, "publish_digest",
                        lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(digest.adapter, "complete_with_cost",
                        _fake_adapter(DIGEST_OUT, 0.10))
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    assert rc == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    # publish_digest(cfg, conn, kind, key, blurb=...)
    assert args[0] is cfg
    assert args[2] == "weekly"
    assert args[3] == "2026-W27"
    assert kwargs["blurb"] == "The week in AI, distilled."


@pytest.mark.integration
def test_run_publish_failure_does_not_kill_digest(cfg, conn, seed, monkeypatch,
                                                  capsys):
    since, _ = period_mod.parse_period("weekly", "2026-W27")
    _seed_window_curation(seed, conn, curated_at=since)
    cfg.data["site"]["push"] = True

    def boom(*a, **k):
        raise RuntimeError("git push rejected")

    monkeypatch.setattr(publish_mod, "publish_digest", boom)
    monkeypatch.setattr(digest.adapter, "complete_with_cost",
                        _fake_adapter(DIGEST_OUT, 0.10))
    rc = digest.run(cfg, kind="weekly", period="2026-W27")
    # publish failure is swallowed: the digest itself still succeeds + persists
    assert rc == 0
    assert _digest_row(conn) is not None
    assert "site publish failed" in capsys.readouterr().out
    warn = conn.execute(
        "SELECT message FROM health WHERE job='digest' AND level='warn'"
    ).fetchone()
    assert "site publish failed" in warn["message"]
