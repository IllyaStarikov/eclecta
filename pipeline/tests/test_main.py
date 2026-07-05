"""Tests for the ``signalpipe.__main__`` CLI dispatcher.

The dispatcher is a thin argparse tree that routes each subcommand to a stage
entrypoint via a lazy, function-local ``from . import X`` import. That laziness is
exactly the seam we exploit: every handler resolves ``X.run`` (etc.) on the module
object at call time, so patching the attribute on the already-imported submodule
(e.g. ``signalpipe.score.run``) makes the stage a spy without touching argparse.

Hard rules honored here:
* NO network / no real stage side effects — every heavy entrypoint is a spy, and
  ``config.load`` is stubbed to hand back the in-memory ``cfg`` fixture so no real
  config file on disk is read.
* ``cmd_status`` / ``cmd_runs`` integration paths use the shared tmp sqlite DB
  (``conn``/``seed``) and stub the macOS downtime probes.
* argparse ``sys.exit(2)`` on bad input is asserted via ``pytest.raises(SystemExit)``.
"""

from __future__ import annotations

import argparse
import datetime
import os
import pathlib

import pytest

import signalpipe
import signalpipe.__main__ as cli
import signalpipe.config as config_mod

# An arbitrary, unmistakable return code so we can prove main() passes the stage
# entrypoint's rc straight through (not a coerced 0).
SENTINEL = 4242


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
class _Spy:
    """Records positional/keyword calls and returns a fixed rc."""

    def __init__(self, rc: int = SENTINEL):
        self.rc = rc
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.rc


@pytest.fixture
def stub_load(cfg, monkeypatch):
    """Make ``_load_cfg(args)`` return the in-memory ``cfg`` fixture regardless of
    ``args.config`` — so dispatch tests never read a real config file from disk."""
    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    return cfg


# --------------------------------------------------------------------------- #
# _load_cfg
# --------------------------------------------------------------------------- #
def test_load_cfg_delegates_to_config_load(cfg, monkeypatch):
    captured = {}

    def fake_load(p=None):
        captured["path"] = p
        return cfg

    monkeypatch.setattr(config_mod, "load", fake_load)
    ns = argparse.Namespace(config=pathlib.Path("/some/where/signal.json"))
    result = cli._load_cfg(ns)
    assert result is cfg
    assert captured["path"] == pathlib.Path("/some/where/signal.json")


def test_config_flag_is_coerced_to_path_and_passed_through(cfg, monkeypatch):
    captured = {}

    def fake_load(p=None):
        captured["path"] = p
        return cfg

    monkeypatch.setattr(config_mod, "load", fake_load)
    spy = _Spy()
    monkeypatch.setattr("signalpipe.score.run", spy)

    rc = cli.main(["--config", "/tmp/z.json", "score", "--show", "3"])
    assert rc == SENTINEL
    # --config type=pathlib.Path -> the raw string becomes a Path.
    assert captured["path"] == pathlib.Path("/tmp/z.json")
    assert spy.calls == [((cfg,), {"show": 3})]


# --------------------------------------------------------------------------- #
# main() dispatch table — routing + arg passthrough + rc passthrough
# --------------------------------------------------------------------------- #
# Each row: (argv, "module.attr" to patch, expected kwargs). Every one of these
# handlers passes cfg as the sole positional, so expected positional is (cfg,).
DISPATCH_CASES = [
    (
        ["ingest", "--source", "hn", "--limit", "3"],
        "signalpipe.ingest.pipeline.run",
        {"only": "hn", "limit": 3},
    ),
    (
        ["ingest"],
        "signalpipe.ingest.pipeline.run",
        {"only": None, "limit": None},
    ),
    (
        ["score", "--show", "5"],
        "signalpipe.score.run",
        {"show": 5},
    ),
    (
        ["score"],
        "signalpipe.score.run",
        {"show": 20},
    ),
    (
        ["fetch", "--limit", "9"],
        "signalpipe.fetch_article.run",
        {"limit": 9},
    ),
    (
        ["curate", "--limit", "4", "--dry-run"],
        "signalpipe.curate.run",
        {"limit": 4, "dry_run": True},
    ),
    (
        ["curate"],
        "signalpipe.curate.run",
        {"limit": None, "dry_run": False},
    ),
    (
        ["digest", "--kind", "daily", "--period", "2026-07-04", "--force"],
        "signalpipe.digest.run",
        {"kind": "daily", "period": "2026-07-04", "force": True},
    ),
    (
        ["digest"],
        "signalpipe.digest.run",
        {"kind": "weekly", "period": None, "force": False},
    ),
    (
        ["retag", "--dry-run", "--limit", "2"],
        "signalpipe.retag.run",
        {"dry_run": True, "limit": 2},
    ),
    (
        ["publish", "--what", "picks", "--no-push"],
        "signalpipe.publish.run",
        {"what": "picks", "push": False, "backfill_since": None},
    ),
    (
        ["publish"],
        "signalpipe.publish.run",
        {"what": "all", "push": True, "backfill_since": None},
    ),
    (
        ["promote", "--week", "2026-W27", "--target", "prod", "--apply",
         "--publish-now"],
        "signalpipe.promote.run",
        {"week": "2026-W27", "target": "prod", "apply": True,
         "publish_now": True},
    ),
    (
        ["promote"],
        "signalpipe.promote.run",
        {"week": None, "target": "local", "apply": False, "publish_now": False},
    ),
    (
        ["serve", "--host", "0.0.0.0", "--port", "9000"],
        "signalpipe.server.run",
        {"host": "0.0.0.0", "port": 9000},
    ),
    (
        ["worker"],
        "signalpipe.worker.run",
        {},
    ),
    (
        ["install", "--no-start"],
        "signalpipe.installer.install",
        {"start": False},
    ),
    (
        ["install"],
        "signalpipe.installer.install",
        {"start": True},
    ),
    (
        ["sync", "--restart"],
        "signalpipe.installer.sync",
        {"restart": True},
    ),
    (
        ["sync"],
        "signalpipe.installer.sync",
        {"restart": False},
    ),
]

DISPATCH_IDS = [
    "ingest-args", "ingest-defaults", "score-show", "score-default",
    "fetch", "curate-args", "curate-defaults", "digest-args", "digest-defaults",
    "retag", "publish-picks-nopush", "publish-defaults", "promote-args",
    "promote-defaults", "serve", "worker", "install-nostart", "install-default",
    "sync-restart", "sync-default",
]


@pytest.mark.parametrize("argv,target,kwargs", DISPATCH_CASES, ids=DISPATCH_IDS)
def test_main_dispatches_and_passes_rc(stub_load, monkeypatch, argv, target, kwargs):
    cfg = stub_load
    spy = _Spy()
    monkeypatch.setattr(target, spy)
    rc = cli.main(argv)
    assert rc == SENTINEL
    assert spy.calls == [((cfg,), kwargs)]


# --------------------------------------------------------------------------- #
# backfill sub-dispatch (fetch / curate / merge) + unknown fallthrough
# --------------------------------------------------------------------------- #
def test_backfill_fetch(stub_load, monkeypatch):
    cfg = stub_load
    spy = _Spy()
    monkeypatch.setattr("signalpipe.backfill.fetch", spy)
    rc = cli.main(["backfill", "fetch", "--since", "2026-01-01",
                   "--until", "2026-02-01", "--top-n", "10"])
    assert rc == SENTINEL
    assert spy.calls == [
        ((cfg,), {"since": "2026-01-01", "until": "2026-02-01", "top_n": 10})
    ]


def test_backfill_curate_defaults_top_n(stub_load, monkeypatch):
    cfg = stub_load
    spy = _Spy()
    monkeypatch.setattr("signalpipe.backfill.curate", spy)
    rc = cli.main(["backfill", "curate", "--since", "2026-01-01",
                   "--until", "2026-02-01", "--dry-run"])
    assert rc == SENTINEL
    assert spy.calls == [
        ((cfg,), {"since": "2026-01-01", "until": "2026-02-01",
                  "top_n": 40, "dry_run": True})
    ]


def test_backfill_merge(stub_load, monkeypatch):
    cfg = stub_load
    spy = _Spy()
    monkeypatch.setattr("signalpipe.backfill.merge", spy)
    rc = cli.main(["backfill", "merge", "--src", "/copy.db"])
    assert rc == SENTINEL
    assert spy.calls == [((cfg,), {"src_db": "/copy.db"})]


def test_backfill_unknown_subcommand_returns_2(stub_load, capsys):
    # The subparser is required=True so argparse blocks a bogus value at parse
    # time; the fallthrough branch is only reachable by a direct call.
    ns = argparse.Namespace(config=None, backfill_cmd="bogus")
    rc = cli.cmd_backfill(ns)
    assert rc == 2
    assert "unknown backfill subcommand" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# sources sub-dispatch (stats / seed / probe / import / expand / bulk)
# --------------------------------------------------------------------------- #
SOURCES_CASES = [
    (["sources", "stats"], "signalpipe.ingest.registry.stats", {}),
    (["sources", "seed"], "signalpipe.ingest.registry.seed", {}),
    (["sources", "expand"], "signalpipe.ingest.registry.expand", {}),
    (
        ["sources", "probe", "--url", "http://x.example", "--import-ok"],
        "signalpipe.ingest.registry.probe_cmd",
        {"candidates": None, "url": "http://x.example", "import_ok": True},
    ),
    (
        ["sources", "import", "/p/to.json"],
        "signalpipe.ingest.registry.import_cmd",
        {"path": pathlib.Path("/p/to.json")},
    ),
    (
        ["sources", "bulk", "--entry", "foo", "--limit", "5", "--no-resume"],
        "signalpipe.ingest.bulk_import.run",
        {"manifest_path": None, "only_entry": "foo", "wave_size": 200,
         "max_workers": 16, "limit": 5, "no_resume": True},
    ),
]

SOURCES_IDS = ["stats", "seed", "expand", "probe", "import", "bulk"]


@pytest.mark.parametrize("argv,target,kwargs", SOURCES_CASES, ids=SOURCES_IDS)
def test_sources_sub_dispatch(stub_load, monkeypatch, argv, target, kwargs):
    cfg = stub_load
    spy = _Spy()
    monkeypatch.setattr(target, spy)
    rc = cli.main(argv)
    assert rc == SENTINEL
    assert spy.calls == [((cfg,), kwargs)]


def test_sources_unknown_subcommand_returns_2(stub_load, capsys):
    ns = argparse.Namespace(config=None, sources_cmd="bogus")
    rc = cli.cmd_sources(ns)
    assert rc == 2
    assert "unknown sources subcommand" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# publish --backfill-kb guard
# --------------------------------------------------------------------------- #
def test_publish_backfill_kb_without_since_returns_2(stub_load, monkeypatch, capsys):
    spy = _Spy()
    monkeypatch.setattr("signalpipe.publish.run", spy)
    rc = cli.main(["publish", "--what", "kb", "--backfill-kb"])
    assert rc == 2
    assert "--backfill-kb requires --since DATE" in capsys.readouterr().err
    assert spy.calls == []  # guard trips before the stage runs


def test_publish_backfill_kb_with_since_passes_through(stub_load, monkeypatch):
    cfg = stub_load
    spy = _Spy()
    monkeypatch.setattr("signalpipe.publish.run", spy)
    rc = cli.main(["publish", "--what", "kb", "--backfill-kb",
                   "--since", "2026-01-01"])
    assert rc == SENTINEL
    assert spy.calls == [
        ((cfg,), {"what": "kb", "push": True, "backfill_since": "2026-01-01"})
    ]


# --------------------------------------------------------------------------- #
# backup — happy path + DBError handling
# --------------------------------------------------------------------------- #
def test_backup_happy(stub_load, monkeypatch, capsys):
    cfg = stub_load
    import signalpipe.db as db_mod

    spy = _Spy(rc=pathlib.Path("/backups/signal-x.db"))
    monkeypatch.setattr(db_mod, "backup", spy)
    rc = cli.main(["backup", "--dir", "/some/dir", "--keep", "3"])
    assert rc == 0
    # backup takes the DB path (not cfg) as the first positional.
    assert spy.calls == [
        ((cfg.db_path,), {"backup_dir": pathlib.Path("/some/dir"), "keep": 3})
    ]
    assert "backup -> /backups/signal-x.db" in capsys.readouterr().out


def test_backup_dberror_returns_1(stub_load, monkeypatch, capsys):
    import signalpipe.db as db_mod

    def boom(*a, **k):
        raise db_mod.DBError("disk full")

    monkeypatch.setattr(db_mod, "backup", boom)
    rc = cli.main(["backup"])
    assert rc == 1
    assert "backup failed: disk full" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# downtime control: pause / resume / downtime status
# --------------------------------------------------------------------------- #
def test_cmd_pause_reports_and_unloads(stub_load, monkeypatch, capsys):
    seen = {}

    def fake_parse(s):
        seen["duration"] = s
        return 7200

    fixed_until = 1_751_630_400.0
    monkeypatch.setattr("signalpipe.downtime.parse_duration", fake_parse)
    monkeypatch.setattr("signalpipe.downtime.pause",
                        lambda secs, reason="manual": fixed_until)
    monkeypatch.setattr("signalpipe.downtime.ollama_unload",
                        lambda c: ["qwen2.5:14b", "llama3"])
    rc = cli.main(["pause", "2h"])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["duration"] == "2h"  # the positional flows into parse_duration
    assert "paused local pipeline for 120 min" in out  # round(7200/60)
    expect_hhmm = datetime.datetime.fromtimestamp(fixed_until).strftime("%H:%M")
    assert ("until %s)" % expect_hhmm) in out
    assert "unloaded model(s): qwen2.5:14b, llama3" in out


def test_cmd_pause_default_duration_and_no_unload(stub_load, monkeypatch, capsys):
    seen = {}

    def fake_parse(s):
        seen["duration"] = s
        return 1800

    monkeypatch.setattr("signalpipe.downtime.parse_duration", fake_parse)
    monkeypatch.setattr("signalpipe.downtime.pause",
                        lambda secs, reason="manual": 1_751_630_400.0)
    monkeypatch.setattr("signalpipe.downtime.ollama_unload", lambda c: [])
    rc = cli.main(["pause"])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["duration"] is None  # nargs='?' default -> None
    assert "paused local pipeline for 30 min" in out  # round(1800/60)
    assert "unloaded model(s)" not in out  # empty list -> no line


def test_cmd_resume(monkeypatch, capsys):
    called = []
    monkeypatch.setattr("signalpipe.downtime.resume", lambda: called.append(True))
    rc = cli.main(["resume"])
    out = capsys.readouterr().out
    assert rc == 0
    assert called == [True]
    assert "resumed" in out


def test_cmd_downtime_prints_status(stub_load, monkeypatch, capsys):
    monkeypatch.setattr("signalpipe.downtime.status",
                        lambda c: "downtime gate: OPEN — local stages may run")
    rc = cli.main(["downtime"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "downtime gate: OPEN — local stages may run" in out


# --------------------------------------------------------------------------- #
# argparse enforcement — choices / required subcommands -> SystemExit(2)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        [],                               # no subcommand (required=True)
        ["digest", "--kind", "hourly"],   # bad choice for --kind
        ["publish", "--what", "bogus"],   # bad choice for --what
        ["promote", "--target", "staging"],  # bad choice for --target
        ["backfill"],                     # nested required subparser missing
        ["backfill", "merge"],            # missing required --src
        ["sources"],                      # nested required subparser missing
        ["score", "--show", "not-an-int"],  # int coercion failure
    ],
    ids=[
        "no-subcommand", "digest-bad-kind", "publish-bad-what",
        "promote-bad-target", "backfill-no-sub", "backfill-merge-no-src",
        "sources-no-sub", "score-bad-int",
    ],
)
def test_argparse_errors_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_version_and_help_exit_0():
    # -h is argparse's own action -> exit 0 (sanity that the tree parses).
    with pytest.raises(SystemExit) as exc:
        cli.main(["-h"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- #
# main() exception mapping
# --------------------------------------------------------------------------- #
def test_main_maps_configerror_to_1(monkeypatch, capsys):
    def boom(p=None):
        raise config_mod.ConfigError("bad config")

    monkeypatch.setattr(config_mod, "load", boom)
    rc = cli.main(["status"])
    assert rc == 1
    assert "config error: bad config" in capsys.readouterr().err


def test_main_maps_keyboardinterrupt_to_130(stub_load, monkeypatch):
    def interrupt(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr("signalpipe.score.run", interrupt)
    rc = cli.main(["score"])
    assert rc == 130


# --------------------------------------------------------------------------- #
# cmd_status — integration against a real tmp sqlite DB
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_cmd_status_happy_path(cfg, conn, seed, monkeypatch, capsys, tmp_path):
    # An existing cli_bin so the "found" branch fires.
    binf = tmp_path / "claude-bin"
    binf.write_text("#!/bin/sh\n")
    cfg.data["backend"]["cli_bin"] = str(binf)
    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    # Neutralize the macOS downtime probes (pmset/ioreg/vm_stat/sysctl).
    monkeypatch.setattr("signalpipe.downtime.is_open", lambda c: (True, "open"))

    src = seed.source(slug="hn", name="Hacker News", verified_at="2026-01-01")
    cid = seed.cluster()
    seed.item(cid, src)
    seed.article(cid)
    seed.curation(cid)
    seed.digest()
    today = conn.execute("SELECT date('now')").fetchone()[0]
    seed.spend(day=today, cli_usd=1.25, api_usd=2.5, calls=9)
    conn.execute(
        "INSERT INTO health(ts,job,level,message) "
        "VALUES('2026-07-04T10:00:00Z','ingest','info','all clear')"
    )

    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "signal %s" % signalpipe.__version__ in out
    assert "(found)" in out                      # cli_bin exists
    assert "routing: triage=" in out             # _tier_desc string builder
    assert "digest=" in out
    assert "downtime gate: OPEN" in out
    assert "sources    1" in out                 # table count, %-10s formatting
    assert "verified   1" in out                 # enabled AND verified_at NOT NULL
    assert "spend today: cli $1.2500  api $2.5000  (9 calls)" in out
    assert "recent health:" in out
    assert "all clear" in out


@pytest.mark.integration
def test_cmd_status_local_tier_routing_desc(cfg, conn, seed, monkeypatch, capsys):
    # Force the digest tier to route local so _tier_desc's local branch renders.
    cfg.data["backend"]["tier_overrides"] = {"triage": "local"}
    cfg.data["tiers"]["triage"]["local"] = "qwen2.5:14b"
    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    monkeypatch.setattr("signalpipe.downtime.is_open", lambda c: (True, "open"))
    seed.source()
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "triage=local:qwen2.5:14b" in out
    # non-overridden tiers still resolve to backend:model form.
    assert "judge=subscription:claude-haiku-4-5" in out


def test_cmd_status_db_not_created(cfg, monkeypatch, capsys, tmp_path):
    # db_path points at a file that does not exist -> early "not created" return.
    cfg.data["db_path"] = str(tmp_path / "does-not-exist.db")
    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    monkeypatch.setattr("signalpipe.downtime.is_open",
                        lambda c: (False, "on battery"))
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "db not created yet" in out
    # gate-closed branch appends the reason.
    assert "downtime gate: CLOSED" in out
    assert "(on battery)" in out


def test_cmd_status_reports_tracked_changes(cfg, monkeypatch, capsys, tmp_path):
    # A tracking entry whose recorded hash no longer matches -> "tracked inputs
    # changed" line. Point it at an absolute file we control.
    tracked = tmp_path / "sources.json"
    tracked.write_text("{}")
    cfg.data["tracking"] = {str(tracked): "stale-hash-that-will-not-match"}
    cfg.data["db_path"] = str(tmp_path / "missing.db")
    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    monkeypatch.setattr("signalpipe.downtime.is_open", lambda c: (True, "open"))
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tracked inputs changed:" in out
    assert str(tracked) in out


# --------------------------------------------------------------------------- #
# cmd_runs
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_cmd_runs_happy(cfg, conn, seed, monkeypatch, capsys):
    import signalpipe.db as db_mod

    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    seed.source()  # ensure the DB file exists on disk

    rows = [
        # A nested dict/list value is dropped from the one-line summary.
        {"ts": "2026-07-04T09:00:00Z", "job": "ingest",
         "config_hash": "abc123def456",
         "stats": '{"new": 5, "dupes": 2, "by_src": {"hn": 3}}'},
        # Malformed stats JSON -> summary falls back to empty (no crash).
        {"ts": "2026-07-04T09:30:00Z", "job": "fetch",
         "config_hash": "abc123def456", "stats": "not-json{"},
        {"ts": "2026-07-04T10:00:00Z", "job": "score",
         "config_hash": "zzz999zzz999", "stats": '{"finalists": 40}'},
    ]
    captured = {}

    def fake_recent_runs(c, job=None, limit=None):
        captured["job"] = job
        captured["limit"] = limit
        return rows

    monkeypatch.setattr(db_mod, "recent_runs", fake_recent_runs)

    rc = cli.main(["runs", "--job", "ingest", "--limit", "7"])
    out = capsys.readouterr().out
    assert rc == 0
    assert captured == {"job": "ingest", "limit": 7}
    assert "current config: %s" % cfg.config_fingerprint()["hash"] in out
    assert "new=5" in out and "dupes=2" in out
    assert "by_src=" not in out  # nested dict value filtered out of the summary
    assert "finalists=40" in out
    # oldest-first, and the config change is flagged between the two rows.
    assert "<- config changed" in out


@pytest.mark.integration
def test_cmd_runs_no_runs(cfg, conn, seed, monkeypatch, capsys):
    import signalpipe.db as db_mod

    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    seed.source()
    monkeypatch.setattr(db_mod, "recent_runs", lambda c, job=None, limit=None: [])
    rc = cli.main(["runs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no runs recorded yet" in out


def test_cmd_runs_db_not_created(cfg, monkeypatch, capsys, tmp_path):
    cfg.data["db_path"] = str(tmp_path / "nope.db")
    monkeypatch.setattr(config_mod, "load", lambda p=None: cfg)
    rc = cli.main(["runs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "db not created yet" in out


# --------------------------------------------------------------------------- #
# Live smoke — real module against real config+DB. Non-hermetic (macOS probes,
# real sqlite); deselected by default (addopts -m 'not live') AND env-gated.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_status_end_to_end_live():
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live smoke: set SIGNAL_LIVE=1 to run against the real box")
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "signalpipe", "status"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "routing:" in proc.stdout
