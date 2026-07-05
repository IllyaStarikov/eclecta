"""Tests for :mod:`signalpipe.promote` — staged-digest -> Ghost promotion.

Every external boundary is faked/tmp: sqlite is a real tmp DB (via the shared
``conn``/``seed``/``cfg`` fixtures), the staged file + ``cfg.blog_repo`` live under
``tmp_path``, and ``subprocess.run`` is replaced on the module so no publisher is
ever shelled. No network, ever.

All tests touch real sqlite / the filesystem, so the whole module is
integration-marked (still 100% offline).
"""

from __future__ import annotations

import subprocess

import pytest

import signalpipe.promote as promote

pytestmark = pytest.mark.integration


# A body with no archive.* links — publishes cleanly.
SAFE_BODY = "# This week\n\nSome content about AI models and tooling.\n"
# A body citing an archive mirror — must be refused.
ARCHIVE_BODY = "# This week\n\nRead more at https://archive.ph/abc123 for context.\n"


# --------------------------------------------------------------------------- #
# subprocess.run doubles (module-level replacement — no injection seam exists)
# --------------------------------------------------------------------------- #
class SubRecorder:
    """Stand-in for the ``subprocess`` module exposing only ``run``.

    Records each invocation's argv + cwd and returns a real
    :class:`subprocess.CompletedProcess` with a configurable return code.
    """

    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls = []

    def run(self, cmd, cwd=None, **kwargs):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "kwargs": kwargs})
        return subprocess.CompletedProcess(list(cmd), self.returncode)


class NoSub:
    """A ``subprocess`` double that fails loudly if ``run`` is ever reached."""

    def run(self, *args, **kwargs):  # pragma: no cover - only hit on a bug
        raise AssertionError("subprocess.run must not be called on this path")


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def _write_staged(tmp_path, name="signal_2026_W27.md", body=SAFE_BODY):
    staged = tmp_path / "staging" / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(body)
    return staged


@pytest.fixture
def promo_cfg(cfg, tmp_path):
    """`cfg` with `blog_repo` repointed at a tmp dir (never REPO_ROOT/iCloud)."""
    blog = tmp_path / "blog_repo"
    (blog / "scripts").mkdir(parents=True, exist_ok=True)
    cfg.data["blog_repo"] = str(blog)
    return cfg


def _digest_row(conn, digest_id):
    return conn.execute(
        "SELECT * FROM digests WHERE id=?", (digest_id,)
    ).fetchone()


# --------------------------------------------------------------------------- #
# _latest_digest
# --------------------------------------------------------------------------- #
def test_latest_digest_none_returns_newest_weekly_by_period_desc(conn, seed):
    seed.digest(period_key="2026-W25", staged_path="/x/a.md")
    seed.digest(period_key="2026-W27", staged_path="/x/c.md")
    seed.digest(period_key="2026-W26", staged_path="/x/b.md")
    # a non-weekly with a later-sorting key must NOT be chosen
    seed.digest(kind="daily", period_key="2026-07-02", staged_path="/x/d.md")

    row = promote._latest_digest(conn, None)
    assert row["kind"] == "weekly"
    assert row["period_key"] == "2026-W27"


def test_latest_digest_explicit_week_filters_weekly_and_key(conn, seed):
    seed.digest(period_key="2026-W25", staged_path="/x/a.md")
    seed.digest(period_key="2026-W26", staged_path="/x/b.md")

    row = promote._latest_digest(conn, "2026-W25")
    assert row is not None
    assert row["kind"] == "weekly"
    assert row["period_key"] == "2026-W25"


def test_latest_digest_explicit_week_ignores_non_weekly_kind(conn, seed):
    # Same period_key on a daily row: the kind='weekly' filter must reject it.
    seed.digest(kind="daily", period_key="2026-07-02", staged_path="/x/d.md")
    assert promote._latest_digest(conn, "2026-07-02") is None


def test_latest_digest_missing_returns_none(conn, seed):
    seed.digest(period_key="2026-W27", staged_path="/x/c.md")
    assert promote._latest_digest(conn, "2026-W99") is None
    # sanity: the single seeded weekly is still selectable via the None-week query
    assert promote._latest_digest(conn, None)["period_key"] == "2026-W27"


def test_latest_digest_empty_table_none(conn):
    assert promote._latest_digest(conn, None) is None
    assert promote._latest_digest(conn, "2026-W27") is None


# --------------------------------------------------------------------------- #
# run — early exits (no work, no subprocess)
# --------------------------------------------------------------------------- #
def test_run_no_digest_returns_1(promo_cfg, monkeypatch, capsys):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    rc = promote.run(promo_cfg, week=None, target="local", apply=True)
    assert rc == 1
    assert "no staged digest" in capsys.readouterr().err


def test_run_staged_file_missing_returns_1(promo_cfg, conn, seed, tmp_path,
                                           monkeypatch, capsys):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    missing = tmp_path / "nope.md"
    seed.digest(period_key="2026-W27", staged_path=str(missing))
    rc = promote.run(promo_cfg, week=None, target="local", apply=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert "staged file missing" in err
    assert str(missing) in err


def test_run_archive_refusal(promo_cfg, conn, seed, tmp_path,
                             monkeypatch, capsys):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    staged = _write_staged(tmp_path, body=ARCHIVE_BODY)
    seed.digest(period_key="2026-W27", staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="prod", apply=True,
                     publish_now=True)
    assert rc == 1
    assert "REFUSED" in capsys.readouterr().err
    # Refusal happens before the copy, so no repo artifact is written.
    repo_md = promo_cfg.blog_repo / "markdown" / "draft" / staged.name
    assert not repo_md.exists()


# --------------------------------------------------------------------------- #
# run — dry run (apply=False): command construction + rendering
# --------------------------------------------------------------------------- #
def test_run_dry_run_local_command_shape(promo_cfg, conn, seed, tmp_path,
                                         monkeypatch, capsys):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    staged = _write_staged(tmp_path, name="signal_2026_W27.md")
    seed.digest(period_key="2026-W27", title="This week in tech",
                staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="local", apply=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "_publish_to_local.py" in out
    assert "--no-feature" in out
    assert "--tag" in out and promote.TAG in out
    assert "--slug" in out and "signal-2026-W27" in out
    # local: never --replace, never the prod draft hint
    assert "--replace" not in out
    assert "prod default is a DRAFT" not in out
    # quoting: a value with spaces is single-quoted in the rendered line
    assert "'This week in tech'" in out


def test_run_dry_run_prod_prints_replace_and_draft_hint(promo_cfg, conn, seed,
                                                        tmp_path, monkeypatch,
                                                        capsys):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    staged = _write_staged(tmp_path)
    seed.digest(period_key="2026-W27", staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="prod", apply=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "_publish_to_prod.py" in out
    assert "--replace" in out
    assert "prod default is a DRAFT" in out
    # publish_now defaulted False -> no --publish flag in the rendered command
    # line itself (the draft hint text separately mentions --publish-now).
    cmd_line = next(l for l in out.splitlines() if "_publish_to_prod.py" in l)
    assert "--publish" not in cmd_line
    assert cmd_line.rstrip().endswith("--replace")


def test_run_dry_run_prod_publish_now_appends_publish(promo_cfg, conn, seed,
                                                      tmp_path, monkeypatch,
                                                      capsys):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    staged = _write_staged(tmp_path)
    seed.digest(period_key="2026-W27", staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="prod", apply=False,
                     publish_now=True)
    assert rc == 0
    out = capsys.readouterr().out
    # --publish appended to the command line itself, after --replace
    cmd_line = next(l for l in out.splitlines() if "_publish_to_prod.py" in l)
    assert "--replace --publish" in cmd_line


def test_run_dry_run_title_fallback_and_slug(promo_cfg, conn, seed, tmp_path,
                                             monkeypatch, capsys):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    staged = _write_staged(tmp_path, name="signal_2026_W27.md")
    # NULL title -> fallback "Signal Digest <period_key>"
    seed.digest(period_key="2026-W27", title=None, staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="local", apply=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "'Signal Digest 2026-W27'" in out  # fallback title, space-quoted
    assert "signal-2026-W27" in out           # slug = stem with _ -> -


def test_run_dry_run_copies_into_repo(promo_cfg, conn, seed, tmp_path,
                                      monkeypatch):
    monkeypatch.setattr(promote, "subprocess", NoSub())
    staged = _write_staged(tmp_path)
    seed.digest(period_key="2026-W27", staged_path=str(staged))

    promote.run(promo_cfg, week=None, target="local", apply=False)
    repo_md = promo_cfg.blog_repo / "markdown" / "draft" / staged.name
    assert repo_md.exists()
    assert repo_md.read_text() == SAFE_BODY


def test_run_dry_run_no_copy_when_paths_equal(promo_cfg, conn, seed,
                                              monkeypatch, capsys):
    # Stage the file directly where the repo copy would land: the equal-path
    # branch must skip the write + the "copied" message.
    repo_draft = promo_cfg.blog_repo / "markdown" / "draft"
    repo_draft.mkdir(parents=True, exist_ok=True)
    staged = repo_draft / "signal_2026_W27.md"
    staged.write_text(SAFE_BODY)
    seed.digest(period_key="2026-W27", staged_path=str(staged))

    monkeypatch.setattr(promote, "subprocess", NoSub())
    rc = promote.run(promo_cfg, week=None, target="local", apply=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "copied staged digest" not in out


# --------------------------------------------------------------------------- #
# run — apply=True (subprocess recorded; DB side effects)
# --------------------------------------------------------------------------- #
def test_run_apply_local_publishes_and_leaves_unpromoted(promo_cfg, conn, seed,
                                                         tmp_path, monkeypatch,
                                                         capsys):
    rec = SubRecorder(returncode=0)
    monkeypatch.setattr(promote, "subprocess", rec)
    staged = _write_staged(tmp_path, name="signal_2026_W27.md")
    did = seed.digest(period_key="2026-W27", title="This week in tech",
                      staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="local", apply=True)
    assert rc == 0

    assert len(rec.calls) == 1
    call = rec.calls[0]
    cmd = call["cmd"]
    assert any("_publish_to_local.py" in c for c in cmd)
    assert "--tag" in cmd and promote.TAG in cmd
    assert "--no-feature" in cmd
    assert "--title" in cmd and "This week in tech" in cmd
    assert "--slug" in cmd and "signal-2026-W27" in cmd
    assert "--replace" not in cmd
    assert "--publish" not in cmd
    assert call["cwd"] == str(promo_cfg.blog_repo / "scripts")

    # repo copy written with the staged body
    repo_md = promo_cfg.blog_repo / "markdown" / "draft" / staged.name
    assert repo_md.read_text() == SAFE_BODY

    # local never promotes and never logs a PUBLISHED health line
    assert _digest_row(conn, did)["promoted"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM health WHERE job='digest'"
    ).fetchone()[0] == 0

    out = capsys.readouterr().out
    assert "copied staged digest" in out
    assert "localhost:2368" in out


def test_run_apply_prod_publish_now_sets_promoted_and_logs(promo_cfg, conn, seed,
                                                           tmp_path, monkeypatch,
                                                           capsys):
    rec = SubRecorder(returncode=0)
    monkeypatch.setattr(promote, "subprocess", rec)
    staged = _write_staged(tmp_path, name="signal_2026_W27.md")
    did = seed.digest(period_key="2026-W27", title="This week in tech",
                      staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="prod", apply=True,
                     publish_now=True)
    assert rc == 0

    assert len(rec.calls) == 1
    cmd = rec.calls[0]["cmd"]
    assert any("_publish_to_prod.py" in c for c in cmd)
    # hard separation guarantees must hold on the prod publish path too:
    # tag is ALWAYS Signal (-> /signal/ collection) and --no-feature ALWAYS.
    assert "--tag" in cmd and promote.TAG in cmd
    assert "--no-feature" in cmd
    assert "--slug" in cmd and "signal-2026-W27" in cmd
    assert "--replace" in cmd
    assert "--publish" in cmd
    assert rec.calls[0]["cwd"] == str(promo_cfg.blog_repo / "scripts")

    # prod + publish_now flips promoted and logs an info health line
    assert _digest_row(conn, did)["promoted"] == 1
    health = conn.execute(
        "SELECT job, level, message FROM health WHERE job='digest'"
    ).fetchone()
    assert health["level"] == "info"
    assert "PUBLISHED to prod" in health["message"]
    # message pins the exact /signal/ route (the separation contract)
    assert "/signal/signal-2026-W27/" in health["message"]

    out = capsys.readouterr().out
    assert "starikov.co" in out


def test_run_apply_prod_draft_default_does_not_promote(promo_cfg, conn, seed,
                                                       tmp_path, monkeypatch):
    rec = SubRecorder(returncode=0)
    monkeypatch.setattr(promote, "subprocess", rec)
    staged = _write_staged(tmp_path)
    did = seed.digest(period_key="2026-W27", staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="prod", apply=True)
    assert rc == 0
    assert len(rec.calls) == 1
    cmd = rec.calls[0]["cmd"]
    assert any("_publish_to_prod.py" in c for c in cmd)
    # separation guarantees hold on the prod DRAFT path as well
    assert "--tag" in cmd and promote.TAG in cmd
    assert "--no-feature" in cmd
    assert "--replace" in cmd
    assert "--publish" not in cmd  # draft: no --publish
    # draft: promoted stays 0, no PUBLISHED health line
    assert _digest_row(conn, did)["promoted"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM health WHERE job='digest'"
    ).fetchone()[0] == 0


def test_run_apply_publisher_failure_propagates_returncode(promo_cfg, conn, seed,
                                                           tmp_path, monkeypatch,
                                                           capsys):
    rec = SubRecorder(returncode=3)
    monkeypatch.setattr(promote, "subprocess", rec)
    staged = _write_staged(tmp_path)
    did = seed.digest(period_key="2026-W27", staged_path=str(staged))

    rc = promote.run(promo_cfg, week=None, target="prod", apply=True,
                     publish_now=True)
    # returncode is propagated verbatim; promotion is skipped on failure
    assert rc == 3
    assert len(rec.calls) == 1
    assert _digest_row(conn, did)["promoted"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM health WHERE job='digest'"
    ).fetchone()[0] == 0
    assert "publisher failed (3)" in capsys.readouterr().err


def test_run_apply_explicit_week_selects_that_digest(promo_cfg, conn, seed,
                                                     tmp_path, monkeypatch):
    rec = SubRecorder(returncode=0)
    monkeypatch.setattr(promote, "subprocess", rec)
    older = _write_staged(tmp_path, name="signal_2026_W25.md")
    newer = _write_staged(tmp_path, name="signal_2026_W27.md")
    seed.digest(period_key="2026-W25", staged_path=str(older))
    seed.digest(period_key="2026-W27", staged_path=str(newer))

    rc = promote.run(promo_cfg, week="2026-W25", target="local", apply=True)
    assert rc == 0
    cmd = rec.calls[0]["cmd"]
    # the explicitly requested (older) week's slug, not the newest
    assert "signal-2026-W25" in cmd
    assert "signal-2026-W27" not in cmd


# --------------------------------------------------------------------------- #
# module constant
# --------------------------------------------------------------------------- #
def test_tag_is_signal():
    assert promote.TAG == "Signal"
