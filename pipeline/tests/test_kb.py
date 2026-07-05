"""Tests for :mod:`signalpipe.kb` — the deterministic kb/ working-note builders.

Every function here RETURNS (relpath, content); publish.py does the writing, so
these tests never touch a real repo or the network.

Boundaries faked:
  * sqlite       — the shared ``conn``/``seed`` fixtures (tmp DB, real schema).
  * llm          — ``signalpipe.llm.adapter.complete`` is monkeypatched (kb pulls
                   the module in via a local import, so patch the module attr).
  * filesystem   — ``cfg.site['repo']`` points at ``tmp_path``; no repo write ever
                   happens here (kb only reads trends.md).
  * clock        — ``kb.datetime`` is swapped for a frozen SimpleNamespace where a
                   date/now bound is load-bearing (backfill, trends).
"""

from __future__ import annotations

import datetime
import json
import types

import pytest

import signalpipe.kb as kb
import signalpipe.llm.adapter as adapter_mod


# --------------------------------------------------------------------------- #
# Clock helpers — freeze both now() and today() at 2026-07-04.
# --------------------------------------------------------------------------- #
class _FrozenDate(datetime.date):
    @classmethod
    def today(cls):
        return datetime.date(2026, 7, 4)


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=tz)


def _freeze_kb_datetime(monkeypatch):
    """Freeze kb.datetime.{date.today, datetime.now} at 2026-07-04T12:00Z.

    kb references only ``datetime.datetime``, ``datetime.date``,
    ``datetime.timedelta`` and ``datetime.timezone``, so a SimpleNamespace with
    those four names fully substitutes for the module.
    """
    monkeypatch.setattr(
        kb,
        "datetime",
        types.SimpleNamespace(
            datetime=_FrozenDateTime,
            date=_FrozenDate,
            timedelta=datetime.timedelta,
            timezone=datetime.timezone,
        ),
    )


def _make_fake_complete(result, calls):
    """A stand-in for ``adapter.complete`` recording its call kwargs.

    ``result`` is returned as the schema dict, unless it is an Exception, in
    which case it is raised (to exercise the error branches).
    """

    def _fake(tier, system, prompt, schema, *, cfg=None, conn=None,
              cap_kind="daily", **_kw):
        calls.append(
            {
                "tier": tier,
                "system": system,
                "prompt": prompt,
                "schema": schema,
                "cap_kind": cap_kind,
            }
        )
        if isinstance(result, Exception):
            raise result
        return result

    return _fake


def _health_rows(conn):
    return conn.execute(
        "SELECT job, level, message FROM health ORDER BY id"
    ).fetchall()


# =========================================================================== #
# Pure logic
# =========================================================================== #
def test_readme_returns_constant_tuple():
    relpath, content = kb.readme()
    assert relpath == "kb/README.md"
    assert content.startswith("# Signal knowledge base")
    assert "days/" in content
    assert "trends.md" in content
    # README promises no archive URLs anywhere in kb/.
    assert "archive.*" in content


def test_day_bounds_utc_midnight_window():
    since, until = kb._day_bounds(datetime.date(2026, 7, 4))
    assert since == "2026-07-04T00:00:00+00:00"
    assert until == "2026-07-05T00:00:00+00:00"


def test_day_bounds_crosses_month_boundary():
    since, until = kb._day_bounds(datetime.date(2026, 1, 31))
    assert since == "2026-01-31T00:00:00+00:00"
    assert until == "2026-02-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime.date(2026, 7, 4), datetime.date(2026, 7, 4)),
        ("2026-07-04", datetime.date(2026, 7, 4)),
        ("2025-12-31", datetime.date(2025, 12, 31)),
    ],
)
def test_coerce_date(value, expected):
    assert kb._coerce_date(value) == expected


def test_coerce_date_passthrough_is_same_object():
    d = datetime.date(2026, 7, 4)
    assert kb._coerce_date(d) is d


def test_split_changelog_marker_and_changelog():
    text = (
        "# Trends\n\n## AI\nOld trend body.\n\n"
        + kb._CHANGELOG_MARKER
        + "\n## Changelog\n- 2026-06-27: previous update\n- 2026-06-20: older\n"
    )
    body, log = kb._split_changelog(text)
    assert body == "# Trends\n\n## AI\nOld trend body."
    assert log == "- 2026-06-27: previous update\n- 2026-06-20: older"


def test_split_changelog_marker_without_changelog_header():
    text = "Body text\n" + kb._CHANGELOG_MARKER + "\nsome trailing junk"
    body, log = kb._split_changelog(text)
    assert body == "Body text"
    assert log == ""


def test_split_changelog_no_marker_rstrips():
    body, log = kb._split_changelog("Just the body\n\n   \n")
    assert body == "Just the body"
    assert log == ""


def test_module_constants():
    assert kb.TRENDS_PATH == "kb/trends.md"
    assert kb._CHANGELOG_MARKER == "<!-- signal:changelog -->"
    assert kb.TRENDS_SCHEMA["required"] == ["trends_md", "changes"]
    assert kb.TRENDS_SCHEMA["additionalProperties"] is False
    assert set(kb.TRENDS_SCHEMA["properties"]) == {"trends_md", "changes"}
    # The system prompt forbids archive.* links and asks for JSON only.
    assert "archive.*" in kb.SYSTEM_TRENDS
    assert "JSON" in kb.SYSTEM_TRENDS


# =========================================================================== #
# daily_ledger
# =========================================================================== #
@pytest.mark.integration
def test_daily_ledger_populated(conn, cfg, seed):
    src = seed.source(name="Hacker News", slug="hn")

    c1 = seed.cluster(
        title="Big AI model released",
        canonical_url="https://example.com/story1",
        score=9.5,
        surface_count=3,
    )
    seed.article(
        c1,
        source_url="https://example.com/story1",
        read_url="https://example.com/free-read1",
    )
    seed.curation(
        c1,
        status="done",
        skip=0,
        relevance_score=9,
        why_it_matters="This is why it matters.",
        notes=json.dumps(["first note", "second note"]),
    )
    seed.surface(c1, src, url="https://news.ycombinator.com/item?id=1", points=250)

    # A skipped curation (status='skipped') — counted, not listed.
    c2 = seed.cluster(
        title="Skipped thing",
        canonical_url="https://example.com/story2",
        score=None,
    )
    seed.curation(c2, status="skipped", skip=0, relevance_score=3)

    # An uncurated, scored cluster — shows in the "Top uncurated" section.
    seed.cluster(
        title="Uncurated gem",
        canonical_url="https://example.com/story3",
        score=7.0,
        surface_count=2,
    )

    # Ingest health: one info line (rendered) + one error line (excluded) +
    # one non-ingest job (excluded).
    kb_db_log(conn, "ingest", "info", "Fetched 12 new items from 5 sources",
              ts="2026-07-04T09:30:00+00:00")
    kb_db_log(conn, "ingest", "error", "boom", ts="2026-07-04T09:31:00+00:00")
    kb_db_log(conn, "score", "info", "scored 3", ts="2026-07-04T09:32:00+00:00")

    relpath, content = kb.daily_ledger(conn, cfg, datetime.date(2026, 7, 4))

    assert relpath == "kb/days/2026-07-04.md"
    assert content.startswith("# Signal ledger — 2026-07-04\n")
    assert "## Curated (1 done, 1 skipped)" in content
    assert (
        "### [Big AI model released](https://example.com/story1) — 9/10"
        in content
    )
    assert "This is why it matters." in content
    assert "Notes: first note · second note" in content
    assert (
        "Links: [source](https://example.com/story1) · "
        "[free read](https://example.com/free-read1) · "
        "[Hacker News (250)](https://news.ycombinator.com/item?id=1)"
    ) in content
    assert "## Top uncurated (1)" in content
    assert (
        "- [Uncurated gem](https://example.com/story3) — score 7.0, 2 surface(s)"
        in content
    )
    assert "Skipped thing" not in content
    assert "## Ingest (1 run(s))" in content
    assert "- 09:30 — Fetched 12 new items from 5 sources" in content
    assert "boom" not in content  # error-level ingest line excluded
    assert "scored 3" not in content  # non-ingest job excluded


@pytest.mark.integration
def test_daily_ledger_empty_day(conn, cfg):
    relpath, content = kb.daily_ledger(conn, cfg, datetime.date(2026, 7, 4))
    assert relpath == "kb/days/2026-07-04.md"
    assert "## Curated (0 done, 0 skipped)" in content
    assert "No curations completed." in content
    assert "## Top uncurated (0)" in content
    assert "None scored." in content
    assert "## Ingest (0 run(s))" in content
    assert "No ingest activity recorded." in content


@pytest.mark.integration
def test_daily_ledger_coerces_string_date(conn, cfg):
    relpath, content = kb.daily_ledger(conn, cfg, "2026-07-04")
    assert relpath == "kb/days/2026-07-04.md"
    assert content.startswith("# Signal ledger — 2026-07-04\n")


@pytest.mark.integration
def test_daily_ledger_done_but_skipped_counts_as_skipped(conn, cfg, seed):
    """status='done' AND skip=1 is a skip: counted, not listed under Curated."""
    c = seed.cluster(title="Muted item", canonical_url="https://example.com/m")
    seed.curation(c, status="done", skip=1, relevance_score=2)
    _, content = kb.daily_ledger(conn, cfg, datetime.date(2026, 7, 4))
    assert "## Curated (0 done, 1 skipped)" in content
    assert "No curations completed." in content
    assert "Muted item" not in content


@pytest.mark.integration
def test_daily_ledger_archive_scrub(conn, cfg, seed):
    """archive.* never leaks: source scrubbed, archive surface skipped."""
    src_arch = seed.source(name="Archive Src", slug="arch")
    src_real = seed.source(name="Lobsters", slug="lob")

    c = seed.cluster(
        title="Archive scrub story",
        canonical_url="https://example.com/arch-story",
        score=None,
    )
    seed.article(
        c,
        source_url="https://archive.ph/AbC12",   # scrubbed -> None
        read_url="https://example.com/free",
    )
    seed.curation(
        c,
        status="done",
        skip=0,
        relevance_score=5,
        why_it_matters=None,
        notes=json.dumps([]),
    )
    # Archive surface (skipped) + a real surface with NULL points.
    seed.surface(c, src_arch, url="https://archive.today/xyz9", points=100)
    seed.surface(c, src_real, url="https://lobste.rs/s/abc", points=None)

    _, content = kb.daily_ledger(conn, cfg, datetime.date(2026, 7, 4))

    assert "archive.ph" not in content
    assert "archive.today" not in content
    assert "[source]" not in content  # source_url was archive -> dropped
    # Title falls back to the free read url.
    assert (
        "### [Archive scrub story](https://example.com/free) — 5/10" in content
    )
    # No notes / no why line (empty notes, why None).
    assert "Notes:" not in content
    # Real surface with NULL points renders name-only (no "(n)" suffix).
    assert (
        "Links: [free read](https://example.com/free) · "
        "[Lobsters](https://lobste.rs/s/abc)"
    ) in content
    assert "[Lobsters (" not in content


@pytest.mark.integration
def test_daily_ledger_excludes_out_of_window_rows(conn, cfg, seed):
    """Rows on a neighbouring day must not bleed into this day's ledger."""
    c = seed.cluster(title="Yesterday news", canonical_url="https://example.com/y")
    seed.curation(
        c, status="done", skip=0, relevance_score=6,
        curated_at="2026-07-03T23:59:59+00:00",  # day before
    )
    _, content = kb.daily_ledger(conn, cfg, datetime.date(2026, 7, 4))
    assert "## Curated (0 done, 0 skipped)" in content
    assert "Yesterday news" not in content


# =========================================================================== #
# backfill
# =========================================================================== #
@pytest.mark.integration
def test_backfill_range(conn, cfg, seed, monkeypatch):
    _freeze_kb_datetime(monkeypatch)  # today = 2026-07-04 -> yesterday 07-03

    # One curation landing on 2026-07-02 to prove routing to the right file.
    c = seed.cluster(
        title="Midweek release",
        canonical_url="https://example.com/mid",
        first_seen="2026-07-02T09:00:00+00:00",
    )
    seed.curation(
        c, status="done", skip=0, relevance_score=7,
        curated_at="2026-07-02T10:00:00+00:00",
    )

    writes = kb.backfill(conn, cfg, "2026-07-01")

    assert set(writes) == {
        "kb/days/2026-07-01.md",
        "kb/days/2026-07-02.md",
        "kb/days/2026-07-03.md",
    }
    for date_str, content in (
        ("2026-07-01", writes["kb/days/2026-07-01.md"]),
        ("2026-07-02", writes["kb/days/2026-07-02.md"]),
        ("2026-07-03", writes["kb/days/2026-07-03.md"]),
    ):
        assert content.startswith("# Signal ledger — %s\n" % date_str)
    assert "Midweek release" in writes["kb/days/2026-07-02.md"]
    assert "No curations completed." in writes["kb/days/2026-07-01.md"]
    assert "No curations completed." in writes["kb/days/2026-07-03.md"]


@pytest.mark.integration
def test_backfill_empty_when_since_after_yesterday(conn, cfg, monkeypatch):
    _freeze_kb_datetime(monkeypatch)  # yesterday = 2026-07-03
    writes = kb.backfill(conn, cfg, "2026-07-05")
    assert writes == {}


@pytest.mark.integration
def test_backfill_single_day(conn, cfg, monkeypatch):
    _freeze_kb_datetime(monkeypatch)  # yesterday = 2026-07-03
    writes = kb.backfill(conn, cfg, "2026-07-03")
    assert set(writes) == {"kb/days/2026-07-03.md"}


# =========================================================================== #
# trends
# =========================================================================== #
def _set_repo(cfg, tmp_path, name="repo", trends_text=None):
    """Point cfg.site['repo'] at a tmp dir; optionally write kb/trends.md."""
    repo = tmp_path / name
    (repo / "kb").mkdir(parents=True, exist_ok=True)
    if trends_text is not None:
        (repo / "kb" / "trends.md").write_text(trends_text)
    cfg.data["site"]["repo"] = str(repo)
    return repo


@pytest.mark.integration
def test_trends_happy_path(conn, cfg, seed, tmp_path, monkeypatch):
    _freeze_kb_datetime(monkeypatch)
    existing = (
        "# Trends\n\n## AI\nOld trend body.\n\n"
        + kb._CHANGELOG_MARKER
        + "\n## Changelog\n- 2026-06-27: previous update\n"
    )
    _set_repo(cfg, tmp_path, trends_text=existing)

    seed.digest()  # weekly, period_key 2026-W27, title "This week in tech"
    c = seed.cluster(title="Fresh curation title",
                     canonical_url="https://example.com/fresh")
    seed.curation(c, status="done", skip=0, relevance_score=8)

    calls = []
    result = {
        "trends_md": "## AI\n\nUpdated trend body with [x](https://example.com/x).",
        "changes": "Refreshed the AI section.",
    }
    monkeypatch.setattr(adapter_mod, "complete",
                        _make_fake_complete(result, calls))

    out = kb.trends(conn, cfg)
    assert out is not None
    relpath, content = out
    assert relpath == "kb/trends.md"

    # New body first, then the marker + managed changelog.
    assert content.startswith(
        "## AI\n\nUpdated trend body with [x](https://example.com/x)."
    )
    assert kb._CHANGELOG_MARKER in content
    assert "## Changelog" in content
    # Today's entry is prepended above the preserved old log.
    new_entry = "- 2026-07-04: Refreshed the AI section."
    old_entry = "- 2026-06-27: previous update"
    assert new_entry in content
    assert old_entry in content
    assert content.index(new_entry) < content.index(old_entry)

    # Prompt assembly: adapter got the right tier/system/schema and a prompt
    # carrying the current body, the weekly digest, and the recent picks.
    assert len(calls) == 1
    call = calls[0]
    assert call["tier"] == "deep"
    assert call["system"] == kb.SYSTEM_TRENDS
    assert call["schema"] == kb.TRENDS_SCHEMA
    assert call["cap_kind"] == "daily"
    prompt = call["prompt"]
    assert "CURRENT TRENDS DOCUMENT:" in prompt
    assert "Old trend body." in prompt
    assert "LATEST WEEKLY DIGEST (2026-W27 — This week in tech):" in prompt
    assert "TOP RECENT CURATIONS (JSON):" in prompt
    assert "Fresh curation title" in prompt


@pytest.mark.integration
def test_trends_first_run_empty_repo(conn, cfg, seed, tmp_path, monkeypatch):
    """repo unset -> current='' -> body '(empty — first run)', log has only today."""
    _freeze_kb_datetime(monkeypatch)
    cfg.data["site"]["repo"] = ""  # falsy -> current stays ""

    seed.digest()

    calls = []
    result = {"trends_md": "## New\n\nBody.", "changes": "Initial doc."}
    monkeypatch.setattr(adapter_mod, "complete",
                        _make_fake_complete(result, calls))

    relpath, content = kb.trends(conn, cfg)
    assert relpath == "kb/trends.md"
    assert "(empty — first run)" in calls[0]["prompt"]
    # Log has exactly today's entry, nothing preserved.
    expected_log = "## Changelog\n- 2026-07-04: Initial doc.\n"
    assert content.endswith(expected_log)


@pytest.mark.integration
def test_trends_repo_read_oserror_falls_back(conn, cfg, seed, tmp_path,
                                             monkeypatch):
    """repo dir exists but kb/trends.md missing -> OSError -> current=''."""
    _freeze_kb_datetime(monkeypatch)
    _set_repo(cfg, tmp_path)  # no trends.md written

    seed.digest()
    calls = []
    monkeypatch.setattr(
        adapter_mod, "complete",
        _make_fake_complete({"trends_md": "## X\n\ny", "changes": "c"}, calls),
    )
    out = kb.trends(conn, cfg)
    assert out is not None
    assert "(empty — first run)" in calls[0]["prompt"]


@pytest.mark.integration
def test_trends_picks_only_no_weekly(conn, cfg, seed, tmp_path, monkeypatch):
    """No weekly digest, but recent picks -> proceeds; prompt omits digest."""
    _freeze_kb_datetime(monkeypatch)
    _set_repo(cfg, tmp_path)

    c = seed.cluster(title="Only a pick", canonical_url="https://example.com/p")
    seed.curation(c, status="done", skip=0, relevance_score=8)

    calls = []
    monkeypatch.setattr(
        adapter_mod, "complete",
        _make_fake_complete({"trends_md": "## T\n\nbody", "changes": "c"}, calls),
    )
    out = kb.trends(conn, cfg)
    assert out is not None
    prompt = calls[0]["prompt"]
    assert "LATEST WEEKLY DIGEST" not in prompt
    assert "TOP RECENT CURATIONS (JSON):" in prompt
    assert "Only a pick" in prompt


@pytest.mark.integration
def test_trends_guard_no_data_returns_none(conn, cfg, tmp_path, monkeypatch,
                                           capsys):
    """No weekly digest and no recent picks -> None, adapter never called."""
    _freeze_kb_datetime(monkeypatch)
    _set_repo(cfg, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("adapter.complete must not be called")

    monkeypatch.setattr(adapter_mod, "complete", _boom)

    assert kb.trends(conn, cfg) is None
    assert "no digest or recent curations" in capsys.readouterr().out


@pytest.mark.integration
def test_trends_guard_archive_body_refused(conn, cfg, seed, tmp_path,
                                           monkeypatch):
    """Model output citing archive.* is refused, logged, and returns None."""
    _freeze_kb_datetime(monkeypatch)
    _set_repo(cfg, tmp_path)
    seed.digest()

    result = {
        "trends_md": "## AI\n\nSee https://archive.ph/xyz for details.",
        "changes": "bad",
    }
    monkeypatch.setattr(adapter_mod, "complete",
                        _make_fake_complete(result, []))

    assert kb.trends(conn, cfg) is None
    rows = _health_rows(conn)
    assert any(
        r["job"] == "publish"
        and r["level"] == "error"
        and "archive link" in r["message"]
        for r in rows
    )


@pytest.mark.integration
def test_trends_guard_llm_error_returns_none(conn, cfg, seed, tmp_path,
                                             monkeypatch, capsys):
    """adapter raising LLMError is caught, logged to health, returns None."""
    from signalpipe.llm import LLMError

    _freeze_kb_datetime(monkeypatch)
    _set_repo(cfg, tmp_path)
    seed.digest()

    monkeypatch.setattr(adapter_mod, "complete",
                        _make_fake_complete(LLMError("boom"), []))

    assert kb.trends(conn, cfg) is None
    rows = _health_rows(conn)
    assert any(
        r["job"] == "publish"
        and r["level"] == "error"
        and "kb trends LLM: boom" in r["message"]
        for r in rows
    )
    assert "kb trends failed: boom" in capsys.readouterr().out


@pytest.mark.integration
def test_trends_guard_spend_cap_returns_none(conn, cfg, seed, tmp_path,
                                             monkeypatch):
    """SpendCapExceeded (an LLMError subclass) is caught the same way."""
    from signalpipe.llm import SpendCapExceeded

    _freeze_kb_datetime(monkeypatch)
    _set_repo(cfg, tmp_path)
    seed.digest()

    monkeypatch.setattr(adapter_mod, "complete",
                        _make_fake_complete(SpendCapExceeded("cap hit"), []))

    assert kb.trends(conn, cfg) is None
    rows = _health_rows(conn)
    assert any("kb trends LLM: cap hit" in r["message"] for r in rows)


# --------------------------------------------------------------------------- #
# small local helper: write a health row (no seeder method for health)
# --------------------------------------------------------------------------- #
def kb_db_log(conn, job, level, message, ts):
    conn.execute(
        "INSERT INTO health(ts, job, level, message, stats) VALUES(?,?,?,?,?)",
        (ts, job, level, message, None),
    )
