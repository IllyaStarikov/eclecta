"""Tests for :mod:`signalpipe.publish` — the sole writer of the site repo.

Everything external is faked or tmp: sqlite is the shared tmp-DB ``conn``/``seed``
fixtures, the clock is frozen by swapping ``publish.datetime`` for a shim, the git
boundary runs against a throwaway tmp working repo + a *local* bare "origin" (no
network, ever), and the kb/LLM collaborators are monkeypatched on ``signalpipe.kb``.

Markers:
* pure helpers (no_archive, _fm_quote, digest_display_date/title, _dirty_paths with a
  patched ``_git``) are plain ``unit`` tests.
* anything touching sqlite / the filesystem / a real ``git`` subprocess is
  ``integration`` (still 100% offline).
"""

from __future__ import annotations

import collections
import datetime
import fcntl
import json
import os
import pathlib
import subprocess

import pytest

import signalpipe.kb as kb
import signalpipe.publish as publish

# --------------------------------------------------------------------------- #
# Frozen clock — publish uses ``datetime.datetime.now`` / ``date.today`` which are
# only injectable by swapping the module's ``datetime`` reference.
# --------------------------------------------------------------------------- #
FROZEN_DT = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _iso(hours: float = 0.0) -> str:
    return (FROZEN_DT + datetime.timedelta(hours=hours)).isoformat()


def _install_frozen_clock(monkeypatch, now: datetime.datetime = FROZEN_DT) -> datetime.datetime:
    """Freeze ``publish.datetime`` so ``now()`` / ``date.today()`` are deterministic
    while every other datetime facility keeps working."""
    _dt = datetime  # avoid the class-body name shadowing the module below

    class _FrozenDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # mirrors datetime.datetime.now
            if tz is None:
                return now.replace(tzinfo=None)
            return now.astimezone(tz)

    class _FrozenDate(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(now.year, now.month, now.day)

    class _Shim:
        datetime = _FrozenDatetime
        date = _FrozenDate
        timedelta = _dt.timedelta
        timezone = _dt.timezone

    monkeypatch.setattr(publish, "datetime", _Shim)
    return now


def _human(d: datetime.date) -> str:
    return "%s %d, %d" % (d.strftime("%B"), d.day, d.year)


# --------------------------------------------------------------------------- #
# git helpers (module-level replacement of the product ``_git`` env)
# --------------------------------------------------------------------------- #
Site = collections.namedtuple("Site", "repo bare")


def _git_env():
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, env=_git_env())


def _commit_file(repo, rel, content, message=None, push=False):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    assert _git(repo, "add", "--", rel).returncode == 0
    r = _git(repo, "commit", "-m", message or ("seed %s" % rel))
    assert r.returncode == 0, r.stderr
    if push:
        assert _git(repo, "push", "origin", "main").returncode == 0


@pytest.fixture
def site_repo(tmp_path, cfg, monkeypatch):
    """A tmp working git repo on ``main`` with a local bare ``origin`` remote, wired
    into ``cfg.site``. Global/system git config are isolated so a developer's
    ``~/.gitconfig`` (gpgsign, hooks, default branch) can't perturb the test."""
    gc = tmp_path / "gitconfig"
    gc.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

    bare = tmp_path / "origin.git"
    assert _run(["git", "init", "--bare", "-b", "main", str(bare)]).returncode == 0
    repo = tmp_path / "site"
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    (repo / "README.md").write_text("hello\n")
    assert _git(repo, "add", "--", "README.md").returncode == 0
    assert _git(repo, "commit", "-m", "init").returncode == 0
    assert _git(repo, "remote", "add", "origin", str(bare)).returncode == 0
    assert _git(repo, "push", "-u", "origin", "main").returncode == 0

    cfg.data["site"]["repo"] = str(repo)
    cfg.data["site"]["push"] = True
    return Site(repo=repo, bare=bare)


# --------------------------------------------------------------------------- #
# module constants
# --------------------------------------------------------------------------- #
def test_pipeline_owned_paths():
    assert publish.PIPELINE_OWNED == ("src/data/", "src/content/digests/", "kb/")
    # startswith accepts the tuple directly (relied on by _clean_pipeline_dirt)
    assert "src/data/picks.json".startswith(publish.PIPELINE_OWNED)
    assert not "src/pages/index.astro".startswith(publish.PIPELINE_OWNED)


def test_lock_path_is_redirected_to_tmp():
    # conftest's redirect_state_dirs must have repointed the module lock at tmp,
    # never the real ~/.local/state; otherwise git_publish tests corrupt state.
    assert "Mobile Documents" not in str(publish.LOCK_PATH)
    assert publish.LOCK_PATH.name == "publish.lock"


# --------------------------------------------------------------------------- #
# no_archive / _ARCHIVE_RE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://archive.ph/abc123",
        "https://archive.today/xyz",
        "https://archive.is/deadbeef",
        "https://web.archive.org/web/2026/https://example.com",
        "http://archive.org/details/foo",
        "https://sub.archive.ph/path",  # host-label prefix via '.'
        "https://example.com/read/archive.ph/x",  # path-embedded via '/'
        "ARCHIVE.PH/loud",  # case-insensitive, bare-start
    ],
)
def test_no_archive_scrubs(url):
    assert publish.no_archive(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/archive-foo",  # hyphen, not a dot
        "https://myarchive.photos/x",  # 'archive.pho...' not archive.ph/$
        "https://archived.example.com/post",  # 'archived' — no dot after archive
        "https://example.com/news/2026",
        "https://news.ycombinator.com/item?id=1",
    ],
)
def test_no_archive_keeps_legit(url):
    assert publish.no_archive(url) == url


def test_no_archive_none_and_empty_passthrough():
    assert publish.no_archive(None) is None
    assert publish.no_archive("") == ""  # falsy -> returned as-is, never scrubbed


@pytest.mark.property
def test_no_archive_property_never_leaks_archive_host():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given
    from hypothesis import strategies as st

    hosts = st.sampled_from(["archive.ph", "archive.today", "archive.is", "archive.org"])
    tails = st.sampled_from(["", "/", "/a/b", "/web/x"])
    prefixes = st.sampled_from(["https://", "https://sub.", "http://"])

    @given(prefixes, hosts, tails)
    def _check(pre, host, tail):
        assert publish.no_archive(pre + host + tail) is None

    _check()


# --------------------------------------------------------------------------- #
# _fm_quote
# --------------------------------------------------------------------------- #
def test_fm_quote_plain():
    assert publish._fm_quote("plain") == '"plain"'


def test_fm_quote_none_and_empty():
    assert publish._fm_quote(None) == '""'
    assert publish._fm_quote("") == '""'


def test_fm_quote_escapes_double_quotes():
    assert publish._fm_quote('say "hi"') == '"say \\"hi\\""'


def test_fm_quote_escapes_backslash():
    # 'C:\path' -> the single backslash is doubled
    assert publish._fm_quote("C:\\path") == '"C:\\\\path"'


def test_fm_quote_backslash_before_quote_order():
    # backslash doubled FIRST, then the quote escaped: '\\"' -> \\ \ "  (3 bs + ")
    assert publish._fm_quote('\\"') == '"\\\\\\""'


# --------------------------------------------------------------------------- #
# digest_display_date / digest_title
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kind,key,expected",
    [
        ("daily", "2026-07-04", datetime.date(2026, 7, 4)),
        ("weekly", "2026-W27", datetime.date(2026, 6, 29)),  # Monday of ISO week 27
        ("monthly", "2026-07", datetime.date(2026, 7, 1)),
        ("quarterly", "2026-Q1", datetime.date(2026, 1, 1)),
        ("quarterly", "2026-Q2", datetime.date(2026, 4, 1)),
        ("quarterly", "2026-Q3", datetime.date(2026, 7, 1)),
        ("quarterly", "2026-Q4", datetime.date(2026, 10, 1)),
        ("yearly", "2026", datetime.date(2026, 1, 1)),
    ],
)
def test_digest_display_date(kind, key, expected):
    assert publish.digest_display_date(kind, key) == expected


def test_digest_display_date_weekly_is_monday():
    d = publish.digest_display_date("weekly", "2026-W27")
    assert d.weekday() == 0  # Monday
    assert d.isocalendar()[:2] == (2026, 27)


@pytest.mark.parametrize(
    "kind,key,expected",
    [
        ("daily", "2026-07-04", "Saturday, July 4, 2026"),  # 2026-07-04 is a Saturday
        ("weekly", "2026-W27", "Week of %s" % _human(datetime.date(2026, 6, 29))),
        ("monthly", "2026-07", "July 2026"),
        ("quarterly", "2026-Q1", "Q1 2026"),
        ("quarterly", "2026-Q3", "Q3 2026"),
        ("quarterly", "2026-Q4", "Q4 2026"),
        ("yearly", "2026", "2026"),
    ],
)
def test_digest_title(kind, key, expected):
    assert publish.digest_title(kind, key) == expected


@pytest.mark.parametrize("key", ["2026-01-01", "2026-07-04", "2025-12-31", "2027-02-28"])
def test_digest_daily_roundtrip(key):
    # daily period_key <-> display_date is an identity
    assert publish.digest_display_date("daily", key).isoformat() == key


@pytest.mark.property
def test_digest_weekly_roundtrip_property():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given
    from hypothesis import strategies as st

    @given(st.dates(datetime.date(2000, 1, 1), datetime.date(2099, 12, 31)))
    def _check(d):
        y, w, _ = d.isocalendar()
        key = "%04d-W%02d" % (y, w)
        got = publish.digest_display_date("weekly", key)
        assert got.weekday() == 0
        assert got.isocalendar()[:2] == (y, w)

    _check()


def test_digest_weekly_roundtrip_sample():
    # deterministic fallback so this path is covered without hypothesis
    for d in (
        datetime.date(2026, 1, 1),
        datetime.date(2026, 7, 4),
        datetime.date(2027, 3, 15),
        datetime.date(2024, 12, 30),
    ):
        y, w, _ = d.isocalendar()
        key = "%04d-W%02d" % (y, w)
        got = publish.digest_display_date("weekly", key)
        assert got.weekday() == 0
        assert got.isocalendar()[:2] == (y, w)


# --------------------------------------------------------------------------- #
# site_config
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_site_config_missing_repo_key(cfg):
    cfg.data["site"] = {}
    with pytest.raises(publish.PublishError) as ei:
        publish.site_config(cfg)
    assert "site.repo is not set" in str(ei.value)


@pytest.mark.integration
def test_site_config_repo_absent(cfg, tmp_path):
    cfg.data["site"] = {"repo": str(tmp_path / "nope")}
    with pytest.raises(publish.PublishError) as ei:
        publish.site_config(cfg)
    assert "does not exist" in str(ei.value)


@pytest.mark.integration
def test_site_config_defaults(cfg, tmp_path):
    repo = tmp_path / "existing_repo"
    repo.mkdir()
    cfg.data["site"] = {"repo": str(repo)}
    site = publish.site_config(cfg)
    assert site["repo_path"] == repo
    assert site["branch"] == "main"
    assert site["remote"] == "origin"
    assert site["picks_window_days"] == 7
    assert site["picks_limit"] == 60


@pytest.mark.integration
def test_site_config_expands_user_and_keeps_overrides(cfg, tmp_path, monkeypatch):
    repo = tmp_path / "home_repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg.data["site"] = {
        "repo": "~/home_repo",
        "branch": "trunk",
        "remote": "upstream",
        "picks_window_days": 3,
        "picks_limit": 5,
    }
    site = publish.site_config(cfg)
    assert site["repo_path"] == repo
    assert site["branch"] == "trunk"
    assert site["remote"] == "upstream"
    assert site["picks_window_days"] == 3
    assert site["picks_limit"] == 5


# --------------------------------------------------------------------------- #
# write_digest_md  (needs a real sqlite3.Row for the blurb keys() check)
# --------------------------------------------------------------------------- #
def _digest_row(conn, kind, key):
    return conn.execute(
        "SELECT * FROM digests WHERE kind=? AND period_key=?", (kind, key)
    ).fetchone()


@pytest.mark.integration
def test_write_digest_md_daily_shape(conn, seed, cfg):
    seed.digest(
        kind="daily",
        period_key="2026-07-04",
        title="Some Title",
        blurb="Custom blurb",
        cluster_ids=json.dumps([1, 2, 3]),
        body_md="  # T\n\nbody text  \n",
    )
    row = _digest_row(conn, "daily", "2026-07-04")
    relpath, content = publish.write_digest_md(cfg, row)

    assert relpath == "src/content/digests/daily/2026-07-04.md"
    assert 'title: "Saturday, July 4, 2026"\n' in content  # 2026-07-04 is a Saturday
    assert "kind: daily\n" in content
    assert 'period: "2026-07-04"\n' in content  # period quoted (schema string)
    assert "date: 2026-07-04\n" in content  # date left UNQUOTED
    assert 'blurb: "Custom blurb"\n' in content
    assert "items: 3\n" in content
    # body is stripped, then a single trailing newline appended
    assert content.endswith("---\n\n# T\n\nbody text\n")


@pytest.mark.integration
def test_write_digest_md_blurb_arg_overrides_row(conn, seed, cfg):
    seed.digest(
        kind="daily", period_key="2026-07-04", blurb="Row blurb", cluster_ids=json.dumps([])
    )
    row = _digest_row(conn, "daily", "2026-07-04")
    _, content = publish.write_digest_md(cfg, row, blurb="Arg blurb")
    assert 'blurb: "Arg blurb"\n' in content


@pytest.mark.integration
def test_write_digest_md_blurb_falls_back_to_title(conn, seed, cfg):
    # arg None + row blurb empty -> raw row title (not the composed digest title)
    seed.digest(
        kind="daily",
        period_key="2026-07-04",
        blurb=None,
        title="Fallback Title",
        cluster_ids=json.dumps([]),
    )
    row = _digest_row(conn, "daily", "2026-07-04")
    _, content = publish.write_digest_md(cfg, row)
    assert 'blurb: "Fallback Title"\n' in content


@pytest.mark.integration
def test_write_digest_md_blurb_final_empty_and_null_body(conn, seed, cfg):
    seed.digest(
        kind="daily",
        period_key="2026-07-04",
        blurb=None,
        title=None,
        cluster_ids=None,
        body_md=None,
    )
    row = _digest_row(conn, "daily", "2026-07-04")
    _, content = publish.write_digest_md(cfg, row)
    assert 'blurb: ""\n' in content  # None -> None -> None -> ""
    assert "items: 0\n" in content  # cluster_ids NULL -> []
    assert content.endswith("---\n\n\n")  # NULL body -> empty body block


@pytest.mark.integration
def test_write_digest_md_weekly_lowercases_key_and_quotes_period(conn, seed, cfg):
    seed.digest(kind="weekly", period_key="2026-W27", cluster_ids=json.dumps([1]))
    row = _digest_row(conn, "weekly", "2026-W27")
    relpath, content = publish.write_digest_md(cfg, row)
    assert relpath == "src/content/digests/weekly/2026-w27.md"  # key.lower()
    assert 'period: "2026-W27"\n' in content  # original case kept
    assert "date: 2026-06-29\n" in content  # Monday, unquoted
    assert "items: 1\n" in content


@pytest.mark.integration
def test_write_digest_md_bad_cluster_ids_json(conn, seed, cfg):
    # malformed cluster_ids JSON -> items falls back to 0 (never raises)
    seed.digest(kind="daily", period_key="2026-07-04", cluster_ids="not json at all")
    row = _digest_row(conn, "daily", "2026-07-04")
    _, content = publish.write_digest_md(cfg, row)
    assert "items: 0\n" in content


@pytest.mark.integration
def test_write_digest_md_yearly_period_quoted(conn, seed, cfg):
    seed.digest(kind="yearly", period_key="2026", cluster_ids=json.dumps([]))
    row = _digest_row(conn, "yearly", "2026")
    relpath, content = publish.write_digest_md(cfg, row)
    assert relpath == "src/content/digests/yearly/2026.md"
    assert 'title: "2026"\n' in content
    assert 'period: "2026"\n' in content  # would YAML-parse as a number unquoted
    assert "date: 2026-01-01\n" in content


# --------------------------------------------------------------------------- #
# export_picks
# --------------------------------------------------------------------------- #
def _pick_by_id(picks, cid):
    return next((p for p in picks if p["id"] == cid), None)


@pytest.mark.integration
def test_export_picks_full(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    hn = seed.source(slug="hn", name="Hacker News")
    other = seed.source(slug="rd", name="Reddit")

    # Cluster A: normal — distinct read_url yields a free_link; an archived
    # surface must be scrubbed out while the real one survives.
    ca = seed.cluster(
        canonical_url="https://example.com/a", title="AI model breaks a benchmark record"
    )
    seed.curation(
        ca, category="ai", channels=json.dumps(["ai"]), relevance_score=9, curated_at=_iso(-1)
    )
    seed.article(
        ca,
        source_url="https://example.com/a",
        read_url="https://example.com/read-a",
        read_kind="primary",
        paywalled=1,
    )
    seed.surface(ca, hn, url="https://news.ycombinator.com/item?id=1", points=120)
    seed.surface(ca, other, url="https://archive.ph/scrubbed", points=200)

    # Cluster B: source_url archived -> canonical fallback; read archived -> no free_link.
    cb = seed.cluster(canonical_url="https://example.com/canon-b", title="Story B")
    seed.curation(cb, category="ai", relevance_score=8, curated_at=_iso(-1))
    seed.article(cb, source_url="https://archive.ph/b", read_url="https://archive.ph/b")

    # Cluster C: every candidate archived (source, canonical, only surface) -> skipped.
    cc = seed.cluster(canonical_url="https://archive.today/canon-c", title="Story C")
    seed.curation(cc, category="ai", relevance_score=8, curated_at=_iso(-1))
    seed.article(cc, source_url="https://archive.ph/c", read_url="https://archive.ph/c")
    seed.surface(cc, hn, url="https://archive.is/c")

    # Cluster D: NULL category -> derived deterministically via topics.match_taxonomy.
    cd = seed.cluster(
        canonical_url="https://example.com/d", title="New Rust compiler release ships faster builds"
    )
    seed.curation(
        cd,
        category=None,
        subcategories=None,
        channels=json.dumps(["devtools"]),
        relevance_score=8,
        curated_at=_iso(-1),
    )
    seed.article(
        cd, source_url="https://example.com/d", read_url="https://example.com/d"
    )  # equal -> free_link None

    # Excluded rows.
    ce = seed.cluster(canonical_url="https://example.com/e", title="Below threshold")
    seed.curation(ce, relevance_score=3, curated_at=_iso(-1))  # < min_rel(6)
    cf = seed.cluster(canonical_url="https://example.com/f", title="Too old")
    seed.curation(cf, relevance_score=9, curated_at=_iso(-24 * 8))  # out of window
    cg = seed.cluster(canonical_url="https://example.com/g", title="Skipped row")
    seed.curation(cg, relevance_score=9, skip=1, curated_at=_iso(-1))  # skip=1
    ch = seed.cluster(canonical_url="https://example.com/h", title="Pending row")
    seed.curation(ch, status="triaged", relevance_score=9, curated_at=_iso(-1))

    picks = publish.export_picks(conn, cfg)
    ids = {p["id"] for p in picks}
    assert ids == {ca, cb, cd}
    assert cc not in ids  # all-archived -> skipped, never emits ""
    for excluded in (ce, cf, cg, ch):
        assert excluded not in ids

    a = _pick_by_id(picks, ca)
    assert a["source_url"] == "https://example.com/a"
    assert a["free_link"] == "https://example.com/read-a"  # read != source
    assert a["read_kind"] == "primary"
    assert a["paywalled"] is True
    assert a["state"] == "confident"
    assert a["relevance"] == 9
    assert a["channels"] == ["ai"]
    assert a["notes"] == ["point one", "point two"]
    assert a["category"] == "ai"
    assert a["surfaces"] == [
        {
            "url": "https://news.ycombinator.com/item?id=1",
            "points": 120,
            "comments": 42,
            "name": "Hacker News",
        }
    ]

    b = _pick_by_id(picks, cb)
    assert b["source_url"] == "https://example.com/canon-b"  # canonical fallback
    assert b["free_link"] is None

    d = _pick_by_id(picks, cd)
    # NULL category -> derived deterministically from title + channels. Pin the
    # concrete taxonomy result rather than re-deriving via topics.match_taxonomy
    # (which would be tautological against the SUT's own internal call). The
    # subcategories fall back to the derived list because the row seeded None.
    assert d["category"] == "software"
    assert d["subcategories"] == ["languages"]
    assert d["free_link"] is None  # read_url == source_url


@pytest.mark.integration
def test_export_picks_limit_and_order(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    cfg.data["site"]["picks_limit"] = 1
    low = seed.cluster(canonical_url="https://example.com/low", title="Lower rel")
    seed.curation(low, relevance_score=7, curated_at=_iso(-1))
    high = seed.cluster(canonical_url="https://example.com/high", title="Higher rel")
    seed.curation(high, relevance_score=10, curated_at=_iso(-1))
    seed.article(low, source_url="https://example.com/low", read_url="https://example.com/low")
    seed.article(high, source_url="https://example.com/high", read_url="https://example.com/high")

    picks = publish.export_picks(conn, cfg)
    assert len(picks) == 1
    assert picks[0]["id"] == high  # relevance DESC wins the single slot


@pytest.mark.integration
def test_export_picks_min_relevance_override(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    cfg.data["site"]["picks_min_relevance"] = 9
    c8 = seed.cluster(canonical_url="https://example.com/c8", title="Rel eight")
    seed.curation(c8, relevance_score=8, curated_at=_iso(-1))
    c9 = seed.cluster(canonical_url="https://example.com/c9", title="Rel nine")
    seed.curation(c9, relevance_score=9, curated_at=_iso(-1))
    seed.article(c8, source_url="https://example.com/c8", read_url="https://example.com/c8")
    seed.article(c9, source_url="https://example.com/c9", read_url="https://example.com/c9")

    ids = {p["id"] for p in publish.export_picks(conn, cfg)}
    assert ids == {c9}  # 8 now excluded by the override


@pytest.mark.integration
def test_export_picks_empty(conn, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    assert publish.export_picks(conn, cfg) == []


# --------------------------------------------------------------------------- #
# export_stats
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_export_stats_shape(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    s1 = seed.source(
        slug="s1", name="Source One", category="ai", tier=1, enabled=1, verified_at=_iso(-2)
    )
    seed.source(slug="s2", name="Source Two", category="devtools", tier=2, enabled=1)
    seed.source(slug="s3", name="Source Three", category="science", tier=3, enabled=0)

    c1 = seed.cluster(canonical_url="https://example.com/c1", title="One")
    c2 = seed.cluster(canonical_url="https://example.com/c2", title="Two")
    seed.curation(c1, relevance_score=8, channels=json.dumps(["ai"]), curated_at=_iso(-1))
    seed.curation(c2, relevance_score=7, channels=json.dumps(["devtools"]), curated_at=_iso(-1))
    seed.item(c1, s1, ingested_at=_iso(-1))
    seed.surface(c1, s1, seen_at=_iso(-1))

    seed.digest(kind="weekly", period_key="2026-W27", generated_at=_iso(-2))
    seed.digest(kind="daily", period_key="2026-07-04", generated_at=_iso(-1), title="Daily latest")

    stats = publish.export_stats(conn, cfg)

    assert stats["generated_at"] == FROZEN_DT.isoformat()
    assert stats["site_name"] == "Eclecta"

    assert stats["sources"]["total"] == 3
    assert stats["sources"]["enabled"] == 2
    assert stats["sources"]["verified"] == 1
    assert stats["sources"]["by_category"] == {"ai": 1, "devtools": 1}
    assert stats["sources"]["by_tier"] == {"1": 1, "2": 1}

    p = stats["pipeline"]
    assert p["items_total"] == 1
    assert p["clusters_total"] == 2
    assert p["curations_done"] == 2
    assert p["items_7d"] == 1
    assert p["curated_7d"] == 2
    assert p["avg_relevance_7d"] == 7.5

    assert stats["digests"]["total"] == 2
    assert stats["digests"]["by_kind"] == {"weekly": 1, "daily": 1}
    assert stats["digests"]["latest"] == {
        "kind": "daily",
        "period": "2026-07-04",
        "title": "Daily latest",
        "date": "2026-07-04",
    }

    slugs = [c["slug"] for c in stats["channels"]]
    assert "everything" not in slugs
    assert len(slugs) == len(cfg.channels) - 1
    by_slug = {c["slug"]: c["picks_current"] for c in stats["channels"]}
    assert by_slug["ai"] == 1
    assert by_slug["devtools"] == 1
    assert by_slug["science"] == 0

    assert stats["top_surfaces_7d"] == [{"name": "Source One", "clusters": 1}]
    # Concrete model routing from the config fixture's tiers block (subscription
    # backend). Pinned literally, not re-derived via cfg.model_for.
    assert stats["models"] == {
        "triage": "claude-haiku-4-5",
        "deep": "claude-sonnet-4-6",
        "digest": "claude-opus-4-8",
    }

    # privacy guarantees: no spend dollars, no health/error text anywhere.
    blob = json.dumps(stats).lower()
    assert "usd" not in blob
    assert "$" not in blob
    assert "health" not in blob
    assert "cost" not in blob


@pytest.mark.integration
def test_export_stats_avg_relevance_omitted_when_empty(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    seed.source(slug="only", name="Only")
    seed.digest(kind="weekly", period_key="2026-W27", generated_at=_iso(-1))
    stats = publish.export_stats(conn, cfg)
    # z.number().optional(): the key is OMITTED, never null, when no curations.
    assert "avg_relevance_7d" not in stats["pipeline"]
    assert stats["pipeline"]["curated_7d"] == 0


@pytest.mark.integration
def test_export_stats_latest_none_when_no_digests(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    seed.source(slug="only", name="Only")
    stats = publish.export_stats(conn, cfg)
    assert stats["digests"]["total"] == 0
    assert stats["digests"]["latest"] is None


# --------------------------------------------------------------------------- #
# _dirty_paths parsing (patched _git, no real repo)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_dirty_paths_parsing(monkeypatch):
    porcelain = "\n".join(
        [
            " M src/data/picks.json",
            "?? kb/new note.md",
            "R  old.md -> src/content/digests/weekly/2026-w27.md",
            'A  "quoted with space.md"',
            "x",  # too short -> skipped
            "",  # blank -> skipped
        ]
    )

    def fake_git(repo, *args):
        assert args == ("status", "--porcelain")
        return subprocess.CompletedProcess(list(args), 0, porcelain, "")

    monkeypatch.setattr(publish, "_git", fake_git)
    entries = publish._dirty_paths(pathlib.Path("/whatever"))
    assert entries == [
        (" M", "src/data/picks.json"),
        ("??", "kb/new note.md"),
        ("R ", "src/content/digests/weekly/2026-w27.md"),  # rename -> new path
        ("A ", "quoted with space.md"),  # quotes unwrapped
    ]


@pytest.mark.integration
def test_dirty_paths_clean(monkeypatch):
    monkeypatch.setattr(
        publish,
        "_git",
        lambda repo, *a: subprocess.CompletedProcess(list(a), 0, "", ""),
    )
    assert publish._dirty_paths(pathlib.Path("/x")) == []


# --------------------------------------------------------------------------- #
# _clean_pipeline_dirt (real tmp repo)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_clean_pipeline_dirt_noop_when_clean(site_repo, conn):
    publish._clean_pipeline_dirt(site_repo.repo, conn)  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM health").fetchone()[0] == 0


@pytest.mark.integration
def test_clean_pipeline_dirt_foreign_refuses(site_repo, conn):
    (site_repo.repo / "README.md").write_text("human edit\n")  # tracked, foreign
    with pytest.raises(publish.PublishError) as ei:
        publish._clean_pipeline_dirt(site_repo.repo, conn)
    assert "dirty outside pipeline-owned" in str(ei.value)
    assert "README.md" in str(ei.value)
    row = conn.execute("SELECT level, message FROM health WHERE job='publish'").fetchone()
    assert row["level"] == "error"
    assert "dirty outside pipeline-owned" in row["message"]
    # the foreign file is never touched
    assert (site_repo.repo / "README.md").read_text() == "human edit\n"


@pytest.mark.integration
def test_clean_pipeline_dirt_foreign_no_conn_still_raises(site_repo):
    (site_repo.repo / "README.md").write_text("human edit\n")
    with pytest.raises(publish.PublishError):
        publish._clean_pipeline_dirt(site_repo.repo, None)  # conn=None -> no log


@pytest.mark.integration
def test_clean_pipeline_dirt_removes_untracked_dir(site_repo, capsys):
    # An untracked directory (owned prefix) is rmtree'd; conn=None skips the log.
    _commit_file(site_repo.repo, "src/content/digests/daily/keep.md", "keep\n")
    sub = site_repo.repo / "src/content/digests/daily/sub"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "leftover.md").write_text("junk\n")

    publish._clean_pipeline_dirt(site_repo.repo, None)

    assert not sub.exists()  # whole untracked dir discarded
    assert _git(site_repo.repo, "status", "--porcelain").stdout.strip() == ""
    assert "[publish]" in capsys.readouterr().out


@pytest.mark.integration
def test_clean_pipeline_dirt_discards_owned(site_repo, conn, capsys):
    _commit_file(site_repo.repo, "src/data/picks.json", "orig\n")
    # A tracked keep-file makes the digests dir tracked, so git reports the
    # untracked leftover at its full (owned) path instead of collapsing it.
    _commit_file(site_repo.repo, "src/content/digests/daily/keep.md", "keep\n")
    # modify the tracked pipeline file + drop an untracked one, both owned.
    (site_repo.repo / "src/data/picks.json").write_text("changed\n")
    untracked = site_repo.repo / "src/content/digests/daily/2026-07-04.md"
    untracked.write_text("stale\n")

    publish._clean_pipeline_dirt(site_repo.repo, conn)

    assert (site_repo.repo / "src/data/picks.json").read_text() == "orig\n"  # restored
    assert not untracked.exists()  # unlinked
    assert (site_repo.repo / "src/content/digests/daily/keep.md").exists()  # untouched
    assert _git(site_repo.repo, "status", "--porcelain").stdout.strip() == ""
    assert "[publish]" in capsys.readouterr().out
    row = conn.execute("SELECT level, message FROM health WHERE job='publish'").fetchone()
    assert row["level"] == "warn"
    assert "discarded stale pipeline-owned changes" in row["message"]


# --------------------------------------------------------------------------- #
# git_publish lifecycle (real tmp repo + bare origin)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_git_publish_pushed(site_repo, cfg, conn):
    status = publish.git_publish(
        cfg,
        "signal: picks",
        {"src/data/picks.json": "new content\n"},
        push=True,
        conn=conn,
    )
    assert status == "pushed"
    assert (site_repo.repo / "src/data/picks.json").read_text() == "new content\n"
    head = _git(site_repo.repo, "rev-parse", "HEAD").stdout.strip()
    origin = _git(site_repo.repo, "rev-parse", "origin/main").stdout.strip()
    assert head == origin
    assert _git(site_repo.bare, "log", "-1", "--format=%s").stdout.strip() == "signal: picks"


@pytest.mark.integration
def test_git_publish_committed_local_when_no_push(site_repo, cfg, conn):
    status = publish.git_publish(
        cfg,
        "signal: local only",
        {"src/data/picks.json": "local\n"},
        push=False,
        conn=conn,
    )
    assert status == "committed-local"
    assert _git(site_repo.repo, "log", "-1", "--format=%s").stdout.strip() == "signal: local only"
    # origin never advanced past the initial commit
    assert _git(site_repo.bare, "log", "-1", "--format=%s").stdout.strip() == "init"


@pytest.mark.integration
def test_git_publish_noop_when_no_diff(site_repo, cfg, conn):
    _commit_file(site_repo.repo, "src/data/picks.json", "same\n", push=True)
    before = _git(site_repo.repo, "rev-parse", "HEAD").stdout.strip()
    status = publish.git_publish(
        cfg,
        "signal: noop",
        {"src/data/picks.json": "same\n"},
        push=True,
        conn=conn,
    )
    assert status == "noop"
    after = _git(site_repo.repo, "rev-parse", "HEAD").stdout.strip()
    assert before == after  # no commit created


@pytest.mark.integration
def test_git_publish_skipped_lock(site_repo, cfg, conn):
    publish.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    holder = open(publish.LOCK_PATH, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        status = publish.git_publish(
            cfg,
            "signal: contended",
            {"src/data/picks.json": "x\n"},
            push=True,
            conn=conn,
        )
        assert status == "skipped-lock"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    # nothing written / committed under contention
    assert not (site_repo.repo / "src/data/picks.json").exists()
    row = conn.execute("SELECT level, message FROM health WHERE job='publish'").fetchone()
    assert row["level"] == "info"
    assert "lock held by another run" in row["message"]


@pytest.mark.integration
def test_git_publish_skipped_lock_no_conn(site_repo, cfg):
    # conn=None variant: the log_health call is skipped but the status stands.
    publish.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    holder = open(publish.LOCK_PATH, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        status = publish.git_publish(
            cfg,
            "signal: x",
            {"src/data/picks.json": "x\n"},
            push=True,
            conn=None,
        )
        assert status == "skipped-lock"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


@pytest.mark.integration
def test_git_publish_offline_no_conn(site_repo, cfg, tmp_path):
    # conn=None variant of the fetch-fail + push-fail path (no health logging).
    assert (
        _git(site_repo.repo, "remote", "set-url", "origin", str(tmp_path / "gone.git")).returncode
        == 0
    )
    status = publish.git_publish(
        cfg,
        "signal: offline",
        {"src/data/picks.json": "x\n"},
        push=True,
        conn=None,
    )
    assert status == "push-failed"
    assert _git(site_repo.repo, "log", "-1", "--format=%s").stdout.strip() == "signal: offline"


@pytest.mark.integration
def test_git_publish_rebase_conflict_no_conn(site_repo, cfg, tmp_path):
    clone2 = tmp_path / "clone2"
    assert _run(["git", "clone", str(site_repo.bare), str(clone2)]).returncode == 0
    (clone2 / "conflict.txt").write_text("remote\n")
    assert _git(clone2, "add", "--", "conflict.txt").returncode == 0
    assert _git(clone2, "commit", "-m", "remote").returncode == 0
    assert _git(clone2, "push", "origin", "main").returncode == 0
    _commit_file(site_repo.repo, "conflict.txt", "local\n", message="local")

    with pytest.raises(publish.PublishError):
        publish.git_publish(cfg, "signal: x", {"src/data/picks.json": "x\n"}, push=True, conn=None)
    assert _git(site_repo.repo, "status", "--porcelain").stdout.strip() == ""


@pytest.mark.integration
def test_git_publish_wrong_branch_preflight(site_repo, cfg, conn):
    assert _git(site_repo.repo, "checkout", "-b", "feature").returncode == 0
    with pytest.raises(publish.PublishError) as ei:
        publish.git_publish(cfg, "signal: x", {"src/data/picks.json": "x\n"}, push=True, conn=conn)
    assert "expected 'main'" in str(ei.value)
    assert "feature" in str(ei.value)


@pytest.mark.integration
def test_git_publish_not_a_git_repo(cfg, tmp_path, conn):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg.data["site"]["repo"] = str(plain)
    cfg.data["site"]["push"] = True
    with pytest.raises(publish.PublishError) as ei:
        publish.git_publish(cfg, "signal: x", {"a.txt": "b\n"}, push=True, conn=conn)
    assert "not a git repository" in str(ei.value)


@pytest.mark.integration
def test_git_publish_rebase_conflict_aborts(site_repo, cfg, conn, tmp_path):
    # A second clone advances origin/main with a conflicting file...
    clone2 = tmp_path / "clone2"
    assert _run(["git", "clone", str(site_repo.bare), str(clone2)]).returncode == 0
    (clone2 / "conflict.txt").write_text("remote side\n")
    assert _git(clone2, "add", "--", "conflict.txt").returncode == 0
    assert _git(clone2, "commit", "-m", "remote change").returncode == 0
    assert _git(clone2, "push", "origin", "main").returncode == 0
    # ...while the site repo commits a divergent version of the same file.
    _commit_file(site_repo.repo, "conflict.txt", "local side\n", message="local change")

    with pytest.raises(publish.PublishError) as ei:
        publish.git_publish(cfg, "signal: x", {"src/data/picks.json": "x\n"}, push=True, conn=conn)
    assert "conflicted" in str(ei.value)
    # rebase --abort leaves a clean tree back on main
    assert _git(site_repo.repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(site_repo.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    row = conn.execute(
        "SELECT level, message FROM health WHERE job='publish' AND level='error'"
    ).fetchone()
    assert row is not None
    assert "conflicted" in row["message"]


@pytest.mark.integration
def test_git_publish_offline_fetch_and_push_fail(site_repo, cfg, conn, tmp_path):
    # Point origin at a nonexistent path: fetch fails (offline branch), the local
    # commit is kept, and push ultimately fails -> 'push-failed'.
    assert (
        _git(site_repo.repo, "remote", "set-url", "origin", str(tmp_path / "gone.git")).returncode
        == 0
    )
    status = publish.git_publish(
        cfg,
        "signal: offline",
        {"src/data/picks.json": "x\n"},
        push=True,
        conn=conn,
    )
    assert status == "push-failed"
    assert _git(site_repo.repo, "log", "-1", "--format=%s").stdout.strip() == "signal: offline"
    warns = conn.execute(
        "SELECT message FROM health WHERE job='publish' AND level='warn'"
    ).fetchall()
    messages = " ".join(r["message"] for r in warns)
    assert "git fetch" in messages
    assert "git push failed" in messages


@pytest.mark.integration
def test_git_publish_push_retry_succeeds(site_repo, cfg, conn, tmp_path, monkeypatch):
    # Origin advances (non-conflicting) between our fetch and our push: the first
    # push is rejected, one `pull --rebase` retry resolves it, and the re-push
    # succeeds -> 'pushed'. Injected by advancing origin on the first push call.
    real_git = publish._git
    clone3 = tmp_path / "clone3"
    assert _run(["git", "clone", str(site_repo.bare), str(clone3)]).returncode == 0
    state = {"advanced": False}

    def wrapper(repo, *args):
        if args[:1] == ("push",) and not state["advanced"]:
            state["advanced"] = True
            (clone3 / "other.txt").write_text("competing\n")
            assert _git(clone3, "add", "--", "other.txt").returncode == 0
            assert _git(clone3, "commit", "-m", "competing").returncode == 0
            assert _git(clone3, "push", "origin", "main").returncode == 0
        return real_git(repo, *args)

    monkeypatch.setattr(publish, "_git", wrapper)
    status = publish.git_publish(
        cfg,
        "signal: raced",
        {"src/data/picks.json": "mine\n"},
        push=True,
        conn=conn,
    )
    assert status == "pushed"
    assert state["advanced"]
    # our commit and the competing commit both landed on origin/main
    subjects = _git(site_repo.repo, "log", "--format=%s", "origin/main").stdout.split("\n")
    assert "signal: raced" in subjects
    assert "competing" in subjects


@pytest.mark.integration
def test_git_publish_add_failure_raises(site_repo, cfg, conn, monkeypatch):
    real_git = publish._git

    def wrapper(repo, *args):
        if args[:1] == ("add",):
            return subprocess.CompletedProcess(list(args), 1, "", "add exploded")
        return real_git(repo, *args)

    monkeypatch.setattr(publish, "_git", wrapper)
    with pytest.raises(publish.PublishError) as ei:
        publish.git_publish(cfg, "m", {"src/data/picks.json": "x\n"}, push=True, conn=conn)
    assert "git add failed" in str(ei.value)


@pytest.mark.integration
def test_git_publish_commit_failure_raises(site_repo, cfg, conn, monkeypatch):
    real_git = publish._git

    def wrapper(repo, *args):
        if args[:1] == ("commit",):
            return subprocess.CompletedProcess(list(args), 1, "", "commit exploded")
        return real_git(repo, *args)

    monkeypatch.setattr(publish, "_git", wrapper)
    with pytest.raises(publish.PublishError) as ei:
        publish.git_publish(cfg, "m", {"src/data/picks.json": "x\n"}, push=True, conn=conn)
    assert "git commit failed" in str(ei.value)


@pytest.mark.integration
def test_git_publish_commit_only_dirt_is_cleaned_first(site_repo, cfg, conn):
    # Leftover pipeline-owned dirt (crash between write and commit) must be
    # discarded by git_publish before it writes, not block the publish. The
    # keep-file keeps the digests dir tracked so the leftover reports as an
    # owned path rather than a collapsed foreign one.
    _commit_file(site_repo.repo, "src/content/digests/daily/keep.md", "keep\n", push=True)
    stale = site_repo.repo / "src/content/digests/daily/stale.md"
    stale.write_text("stale\n")
    status = publish.git_publish(
        cfg,
        "signal: picks",
        {"src/data/picks.json": "fresh\n"},
        push=True,
        conn=conn,
    )
    assert status == "pushed"
    assert not stale.exists()  # discarded
    assert (site_repo.repo / "src/data/picks.json").read_text() == "fresh\n"


# --------------------------------------------------------------------------- #
# publish_digest — status -> DB mapping
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.parametrize("status", ["pushed", "committed-local", "noop"])
def test_publish_digest_success_sets_published_at(conn, seed, cfg, monkeypatch, status):
    seed.digest(
        kind="daily",
        period_key="2026-07-04",
        published_at=None,
        publish_error="stale error",
        cluster_ids=json.dumps([]),
    )
    monkeypatch.setattr(publish, "git_publish", lambda *a, **k: status)
    rc = publish.publish_digest(cfg, conn, "daily", "2026-07-04")
    assert rc == 0
    row = _digest_row(conn, "daily", "2026-07-04")
    assert row["published_at"] is not None
    assert row["publish_error"] is None


@pytest.mark.integration
def test_publish_digest_push_failed(conn, seed, cfg, monkeypatch):
    seed.digest(
        kind="daily", period_key="2026-07-04", published_at=None, cluster_ids=json.dumps([])
    )
    monkeypatch.setattr(publish, "git_publish", lambda *a, **k: "push-failed")
    rc = publish.publish_digest(cfg, conn, "daily", "2026-07-04")
    assert rc == 1
    row = _digest_row(conn, "daily", "2026-07-04")
    assert row["published_at"] is None
    assert row["publish_error"] == "push failed; local commit kept"


@pytest.mark.integration
def test_publish_digest_skipped_lock(conn, seed, cfg, monkeypatch):
    seed.digest(
        kind="daily", period_key="2026-07-04", published_at=None, cluster_ids=json.dumps([])
    )
    monkeypatch.setattr(publish, "git_publish", lambda *a, **k: "skipped-lock")
    rc = publish.publish_digest(cfg, conn, "daily", "2026-07-04")
    assert rc == 1
    row = _digest_row(conn, "daily", "2026-07-04")
    assert row["published_at"] is None
    assert row["publish_error"] == "publish lock contention; retry pending"


@pytest.mark.integration
def test_publish_digest_publish_error(conn, seed, cfg, monkeypatch, capsys):
    seed.digest(
        kind="daily", period_key="2026-07-04", published_at=None, cluster_ids=json.dumps([])
    )

    def boom(*a, **k):
        raise publish.PublishError("boom detail")

    monkeypatch.setattr(publish, "git_publish", boom)
    rc = publish.publish_digest(cfg, conn, "daily", "2026-07-04")
    assert rc == 1
    row = _digest_row(conn, "daily", "2026-07-04")
    assert row["published_at"] is None
    assert row["publish_error"] == "boom detail"


@pytest.mark.integration
def test_publish_digest_missing_row(conn, cfg, capsys):
    rc = publish.publish_digest(cfg, conn, "daily", "1999-01-01")
    assert rc == 1
    assert "no daily digest for 1999-01-01" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# run / refresh / publish_kb_daily / publish_trends orchestration
# --------------------------------------------------------------------------- #
class _GitPublishRecorder:
    """Captures git_publish call args and returns a canned status."""

    def __init__(self, status="pushed"):
        self.status = status
        self.calls = []

    def __call__(self, cfg, message, writes, push=True, conn=None):
        self.calls.append({"message": message, "writes": dict(writes), "push": push})
        return self.status


@pytest.mark.integration
def test_run_all_assembles_writes_and_backfills(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    rec = _GitPublishRecorder("pushed")
    monkeypatch.setattr(publish, "git_publish", rec)
    monkeypatch.setattr(kb, "readme", lambda: ("kb/README.md", "readme\n"))
    monkeypatch.setattr(
        kb, "daily_ledger", lambda c, cf, d: ("kb/days/%s.md" % d.isoformat(), "ledger\n")
    )
    did = seed.digest(
        kind="daily", period_key="2026-07-04", published_at=None, cluster_ids=json.dumps([])
    )

    rc = publish.run(cfg, what="all", push=True)
    assert rc == 0
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["message"] == "signal: publish all"
    w = call["writes"]
    assert "src/data/picks.json" in w
    assert "src/data/stats.json" in w
    assert "src/content/digests/daily/2026-07-04.md" in w
    assert "kb/README.md" in w
    assert "kb/days/2026-07-03.md" in w  # yesterday under the frozen clock

    row = conn.execute("SELECT published_at FROM digests WHERE id=?", (did,)).fetchone()
    assert row["published_at"] is not None  # digests/all -> backfilled


@pytest.mark.integration
def test_run_picks_only_no_digest_backfill(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    rec = _GitPublishRecorder("pushed")
    monkeypatch.setattr(publish, "git_publish", rec)
    did = seed.digest(
        kind="daily", period_key="2026-07-04", published_at=None, cluster_ids=json.dumps([])
    )

    rc = publish.run(cfg, what="picks", push=True)
    assert rc == 0
    assert set(rec.calls[0]["writes"]) == {"src/data/picks.json"}
    row = conn.execute("SELECT published_at FROM digests WHERE id=?", (did,)).fetchone()
    assert row["published_at"] is None  # what='picks' never backfills digests


@pytest.mark.integration
def test_run_nothing_to_publish(cfg, monkeypatch, capsys):
    def no_call(*a, **k):
        raise AssertionError("git_publish must not run with empty writes")

    monkeypatch.setattr(publish, "git_publish", no_call)
    rc = publish.run(cfg, what="bogus")
    assert rc == 0
    assert "nothing to publish for --what bogus" in capsys.readouterr().out


@pytest.mark.integration
def test_run_publish_error_returns_1(conn, seed, cfg, monkeypatch, capsys):
    _install_frozen_clock(monkeypatch)

    def boom(*a, **k):
        raise publish.PublishError("preflight boom")

    monkeypatch.setattr(publish, "git_publish", boom)
    rc = publish.run(cfg, what="picks")
    assert rc == 1
    assert "publish failed: preflight boom" in capsys.readouterr().out
    row = conn.execute("SELECT level, message FROM health WHERE job='publish'").fetchone()
    assert row["level"] == "error"
    assert "preflight boom" in row["message"]


@pytest.mark.integration
def test_run_push_failed_returns_1_without_backfill(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    monkeypatch.setattr(publish, "git_publish", _GitPublishRecorder("push-failed"))
    monkeypatch.setattr(kb, "readme", lambda: ("kb/README.md", "r\n"))
    monkeypatch.setattr(kb, "daily_ledger", lambda c, cf, d: ("kb/days/x.md", "l\n"))
    did = seed.digest(
        kind="daily", period_key="2026-07-04", published_at=None, cluster_ids=json.dumps([])
    )
    rc = publish.run(cfg, what="all")
    assert rc == 1
    row = conn.execute("SELECT published_at FROM digests WHERE id=?", (did,)).fetchone()
    assert row["published_at"] is None  # not pushed/committed -> no backfill


@pytest.mark.integration
def test_run_kb_backfill_since(conn, cfg, monkeypatch):
    monkeypatch.setattr(publish, "git_publish", _GitPublishRecorder("pushed"))
    monkeypatch.setattr(kb, "readme", lambda: ("kb/README.md", "r\n"))

    def no_ledger(*a, **k):  # backfill_since path must NOT call daily_ledger
        raise AssertionError("daily_ledger must not run when backfill_since is set")

    monkeypatch.setattr(kb, "daily_ledger", no_ledger)
    monkeypatch.setattr(
        kb,
        "backfill",
        lambda c, cf, since: {
            "kb/days/2026-07-01.md": "a\n",
            "kb/days/2026-07-02.md": "b\n",
        },
    )
    rc = publish.run(cfg, what="kb", backfill_since="2026-07-01")
    assert rc == 0
    call = publish.git_publish.calls[0]
    assert set(call["writes"]) == {
        "kb/README.md",
        "kb/days/2026-07-01.md",
        "kb/days/2026-07-02.md",
    }


@pytest.mark.integration
def test_refresh_publish_error_returns_1(conn, seed, cfg, monkeypatch, capsys):
    _install_frozen_clock(monkeypatch)

    def boom(*a, **k):
        raise publish.PublishError("refresh boom")

    monkeypatch.setattr(publish, "git_publish", boom)
    rc = publish.refresh(cfg)
    assert rc == 1
    assert "publish refresh failed: refresh boom" in capsys.readouterr().out
    row = conn.execute("SELECT level, message FROM health WHERE job='publish'").fetchone()
    assert row["level"] == "error"
    assert "refresh boom" in row["message"]


@pytest.mark.integration
def test_refresh_retries_unpublished_digest(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    rec = _GitPublishRecorder("pushed")
    monkeypatch.setattr(publish, "git_publish", rec)
    did = seed.digest(
        kind="daily", period_key="2026-07-04", published_at=None, cluster_ids=json.dumps([])
    )

    rc = publish.refresh(cfg)
    assert rc == 0
    w = rec.calls[0]["writes"]
    assert "src/data/picks.json" in w
    assert "src/data/stats.json" in w
    assert "src/content/digests/daily/2026-07-04.md" in w  # unpublished retried
    assert rec.calls[0]["message"] == "signal: refresh picks + stats"
    row = conn.execute("SELECT published_at FROM digests WHERE id=?", (did,)).fetchone()
    assert row["published_at"] is not None


@pytest.mark.integration
def test_refresh_no_unpublished_skips_digest_writes(conn, seed, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    rec = _GitPublishRecorder("noop")
    monkeypatch.setattr(publish, "git_publish", rec)
    seed.digest(
        kind="daily", period_key="2026-07-04", published_at=_iso(-1), cluster_ids=json.dumps([])
    )
    rc = publish.refresh(cfg)
    assert rc == 0
    assert set(rec.calls[0]["writes"]) == {"src/data/picks.json", "src/data/stats.json"}


@pytest.mark.integration
def test_publish_kb_daily_default_yesterday(conn, cfg, monkeypatch):
    _install_frozen_clock(monkeypatch)
    rec = _GitPublishRecorder("pushed")
    monkeypatch.setattr(publish, "git_publish", rec)
    monkeypatch.setattr(kb, "readme", lambda: ("kb/README.md", "r\n"))
    monkeypatch.setattr(
        kb, "daily_ledger", lambda c, cf, d: ("kb/days/%s.md" % d.isoformat(), "l\n")
    )

    rc = publish.publish_kb_daily(cfg)
    assert rc == 0
    assert set(rec.calls[0]["writes"]) == {"kb/README.md", "kb/days/2026-07-03.md"}
    assert rec.calls[0]["message"] == "signal: kb ledger 2026-07-03"


@pytest.mark.integration
@pytest.mark.parametrize(
    "dates,expected_days,label",
    [
        ("2026-07-01", ["2026-07-01"], "2026-07-01"),
        (datetime.date(2026, 7, 2), ["2026-07-02"], "2026-07-02"),
        (
            ["2026-07-01", "2026-07-02", "2026-07-03"],
            ["2026-07-01", "2026-07-02", "2026-07-03"],
            "2026-07-01, 2026-07-02, 2026-07-03",
        ),
    ],
)
def test_publish_kb_daily_date_normalization(conn, cfg, monkeypatch, dates, expected_days, label):
    rec = _GitPublishRecorder("pushed")
    monkeypatch.setattr(publish, "git_publish", rec)
    monkeypatch.setattr(kb, "readme", lambda: ("kb/README.md", "r\n"))
    monkeypatch.setattr(
        kb, "daily_ledger", lambda c, cf, d: ("kb/days/%s.md" % d.isoformat(), "l\n")
    )

    rc = publish.publish_kb_daily(cfg, dates=dates)
    assert rc == 0
    w = rec.calls[0]["writes"]
    expected = {"kb/README.md"} | {"kb/days/%s.md" % d for d in expected_days}
    assert set(w) == expected
    assert rec.calls[0]["message"] == "signal: kb ledger %s" % label


@pytest.mark.integration
def test_publish_kb_daily_publish_error(conn, cfg, monkeypatch, capsys):
    monkeypatch.setattr(
        publish,
        "git_publish",
        lambda *a, **k: (_ for _ in ()).throw(publish.PublishError("kbfail")),
    )
    monkeypatch.setattr(kb, "readme", lambda: ("kb/README.md", "r\n"))
    monkeypatch.setattr(kb, "daily_ledger", lambda c, cf, d: ("kb/days/x.md", "l\n"))
    rc = publish.publish_kb_daily(cfg, dates="2026-07-01")
    assert rc == 1
    assert "kb publish failed: kbfail" in capsys.readouterr().out


@pytest.mark.integration
def test_publish_trends_success(conn, cfg, monkeypatch):
    rec = _GitPublishRecorder("pushed")
    monkeypatch.setattr(publish, "git_publish", rec)
    monkeypatch.setattr(kb, "trends", lambda c, cf: ("kb/trends.md", "trend body\n"))
    rc = publish.publish_trends(cfg)
    assert rc == 0
    assert rec.calls[0]["writes"] == {"kb/trends.md": "trend body\n"}
    assert rec.calls[0]["message"] == "signal: kb trends update"


@pytest.mark.integration
def test_publish_trends_none_result_returns_1(conn, cfg, monkeypatch):
    def no_call(*a, **k):
        raise AssertionError("git_publish must not run when trends is None")

    monkeypatch.setattr(publish, "git_publish", no_call)
    monkeypatch.setattr(kb, "trends", lambda c, cf: None)
    assert publish.publish_trends(cfg) == 1


@pytest.mark.integration
def test_publish_trends_exception_logged(conn, cfg, monkeypatch, capsys):
    def no_call(*a, **k):
        raise AssertionError("git_publish must not run when trends raises")

    monkeypatch.setattr(publish, "git_publish", no_call)

    def boom(c, cf):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(kb, "trends", boom)
    rc = publish.publish_trends(cfg)
    assert rc == 1
    assert "kb trends failed: model exploded" in capsys.readouterr().out
    row = conn.execute("SELECT level, message FROM health WHERE job='publish'").fetchone()
    assert row["level"] == "error"
    assert "kb trends failed" in row["message"]


@pytest.mark.integration
def test_publish_trends_publish_error(conn, cfg, monkeypatch, capsys):
    def boom(*a, **k):
        raise publish.PublishError("trend push boom")

    monkeypatch.setattr(publish, "git_publish", boom)
    monkeypatch.setattr(kb, "trends", lambda c, cf: ("kb/trends.md", "body\n"))
    rc = publish.publish_trends(cfg)
    assert rc == 1
    assert "kb trends publish failed: trend push boom" in capsys.readouterr().out
