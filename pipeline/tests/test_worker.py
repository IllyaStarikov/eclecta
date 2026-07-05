"""Tests for signalpipe.worker — the APScheduler BlockingScheduler process.

The scheduler is the sole DB writer. Every external boundary is faked:
  * ``BlockingScheduler.start()`` BLOCKS forever and registers process-global
    SIGTERM/SIGINT handlers — the real class is replaced with a no-op fake and
    ``stdlib_signal.signal`` is stubbed to a recorder.
  * ``_job``'s runner reloads config via ``config_mod.load()`` on every run — it
    is monkeypatched to return the test ``cfg``.
  * downtime/quota gates, digest.run, publish.*, db.backup and the ingest/score/
    fetch/curate stages are all patched; nothing shells out or spends money.

The APScheduler triggers are replaced with recorders so the wiring (job ids,
per-job cadences, job_defaults) can be asserted without a live scheduler.
"""

from __future__ import annotations

import datetime
import pathlib
import types

import pytest

import signalpipe.curate as curate_mod
import signalpipe.db as db_mod
import signalpipe.digest as digest_mod
import signalpipe.fetch_article as fetch_mod
import signalpipe.ingest.pipeline as pipeline_mod
import signalpipe.llm.adapter as adapter_mod
import signalpipe.llm.quota as quota_mod
import signalpipe.period as period_mod
import signalpipe.publish as publish_mod
import signalpipe.score as score_mod
from signalpipe import worker


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
class Spy:
    """Records every call; optionally returns a value or raises."""

    def __init__(self, ret=None, raise_exc=None):
        self.calls = []
        self.ret = ret
        self.raise_exc = raise_exc

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.ret

    @property
    def called(self):
        return bool(self.calls)


class FakeInterval:
    """Stand-in for apscheduler IntervalTrigger; just captures kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeCron:
    """Stand-in for apscheduler CronTrigger; captures the crontab expr + tz."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.crontab = None
        self.timezone = None

    @classmethod
    def from_crontab(cls, expr, timezone=None):
        obj = cls()
        obj.crontab = expr
        obj.timezone = timezone
        return obj


class FakeJob:
    def __init__(self, job_id):
        self.job_id = job_id
        self.modified = []

    def modify(self, **kwargs):
        self.modified.append(kwargs)


def _install_fake_scheduler(monkeypatch):
    """Replace the function-local apscheduler imports in ``worker.run`` with
    recorders. Returns the list that captures every constructed scheduler."""
    created = []

    class FakeScheduler:
        def __init__(self, job_defaults=None, **kw):
            self.job_defaults = job_defaults
            self.init_kwargs = kw
            self.jobs = []
            self.started = False
            self.shutdown_calls = []
            self._job_provider = lambda jid: None
            created.append(self)

        def add_job(self, func, trigger=None, **kwargs):
            self.jobs.append(
                {
                    "func": func,
                    "trigger": trigger,
                    "id": kwargs.get("id"),
                    "next_run_time": kwargs.get("next_run_time"),
                    "kwargs": kwargs,
                }
            )

        def start(self):
            self.started = True

        def shutdown(self, wait=True):
            self.shutdown_calls.append(wait)

        def get_job(self, job_id):
            return self._job_provider(job_id)

    import apscheduler.schedulers.blocking as _bl
    import apscheduler.triggers.cron as _cr
    import apscheduler.triggers.interval as _iv

    monkeypatch.setattr(_bl, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(_cr, "CronTrigger", FakeCron)
    monkeypatch.setattr(_iv, "IntervalTrigger", FakeInterval)
    return created


class _RunResult:
    def __init__(self, rc, scheduler, handlers):
        self.rc = rc
        self.scheduler = scheduler
        self.handlers = handlers
        self.jobs_by_id = {j["id"]: j for j in scheduler.jobs}


def _run_with_fakes(cfg, monkeypatch, stale=None):
    """Drive ``worker.run`` with a fake scheduler + stubbed signal registration.

    ``stale`` controls the ``_check_runtime_staleness`` return value. Returns a
    ``_RunResult`` exposing the captured scheduler, its jobs and signal handlers.
    """
    created = _install_fake_scheduler(monkeypatch)
    handlers = []
    monkeypatch.setattr(
        worker.stdlib_signal,
        "signal",
        lambda signum, handler: handlers.append((signum, handler)),
    )
    monkeypatch.setattr(worker, "_check_runtime_staleness", lambda c: stale)
    rc = worker.run(cfg)
    assert created, "worker.run never constructed a scheduler"
    return _RunResult(rc, created[0], handlers)


# ========================================================================== #
# run_due_editions — the tier-ordered dispatch state machine
# ========================================================================== #
class _EditionRecorder:
    """Patches digest.run + _edition_covered + _publish_kb_window for a run."""

    def __init__(self):
        self.digest_calls = []  # (kind, period)
        self.kb_calls = []

    def digest_run(self, cfg, kind="weekly", period=None, force=False):
        self.digest_calls.append((kind, period))
        return 0

    @property
    def kinds(self):
        return [k for (k, _p) in self.digest_calls]


def _patch_editions(monkeypatch, rec, digest_run=None, covered=False, kb=None):
    monkeypatch.setattr(digest_mod, "run", digest_run or rec.digest_run)
    monkeypatch.setattr(worker, "_edition_covered", lambda cfg, kind, today: covered)
    monkeypatch.setattr(
        worker,
        "_publish_kb_window",
        kb or (lambda cfg, today: rec.kb_calls.append(today)),
    )


def test_run_due_editions_daily_weekly_order_and_publishes_kb(cfg, monkeypatch):
    # 2026-07-10 is a Friday on day 10 (> CATCH_UP_DAYS), so only the two
    # cron-gated kinds (daily + weekly) are due; monthly/quarterly are gated out.
    cfg.data["site"]["push"] = True
    rec = _EditionRecorder()
    _patch_editions(monkeypatch, rec)

    worker.run_due_editions(cfg, today=datetime.date(2026, 7, 10))

    assert rec.digest_calls == [("daily", None), ("weekly", None)]
    # daily success + site.push -> exactly one kb publish for the daily window.
    assert rec.kb_calls == [datetime.date(2026, 7, 10)]


def test_run_due_editions_no_kinds_due_is_a_noop(cfg, monkeypatch):
    # 2026-07-12 is a Sunday on day 12: daily/weekly not due, month kinds past
    # the catch-up window -> nothing dispatches.
    rec = _EditionRecorder()
    _patch_editions(monkeypatch, rec)

    worker.run_due_editions(cfg, today=datetime.date(2026, 7, 12))

    assert rec.digest_calls == []
    assert rec.kb_calls == []


def test_run_due_editions_monthly_quarterly_yearly_catch_up(cfg, monkeypatch):
    # 2026-01-01 is the first weekday of January -> monthly, quarterly AND
    # yearly are all applicable and within the day 1..7 catch-up window.
    rec = _EditionRecorder()
    _patch_editions(monkeypatch, rec)

    worker.run_due_editions(cfg, today=datetime.date(2026, 1, 1))

    # daily is also due (Thu); weekly is not (not Friday).
    assert rec.digest_calls == [
        ("daily", None),
        ("monthly", "2025-12"),
        ("quarterly", "2025-Q4"),
        ("yearly", "2025"),
    ]


def test_run_due_editions_catch_up_window_closes_after_seven_days(cfg, monkeypatch):
    # 2026-01-08 (day 8) is past CATCH_UP_DAYS: month-anchored kinds do NOT run.
    rec = _EditionRecorder()
    _patch_editions(monkeypatch, rec)

    worker.run_due_editions(cfg, today=datetime.date(2026, 1, 8))

    assert rec.kinds == ["daily"]  # only the cron-gated daily fires


def test_run_due_editions_dependency_deferral_when_monthly_fails(cfg, monkeypatch, capsys):
    rec = _EditionRecorder()

    def digest_run(cfg, kind="weekly", period=None, force=False):
        rec.digest_calls.append((kind, period))
        return 1 if kind == "monthly" else 0

    _patch_editions(monkeypatch, rec, digest_run=digest_run)

    worker.run_due_editions(cfg, today=datetime.date(2026, 1, 1))

    # monthly ran and failed -> quarterly (dep monthly) and yearly (dep
    # monthly, quarterly) are both deferred and never dispatched.
    assert rec.kinds == ["daily", "monthly"]
    out = capsys.readouterr().out
    assert "quarterly digest deferred" in out
    assert "yearly digest deferred" in out
    assert "lower tier(s) monthly failed" in out


def test_run_due_editions_exception_isolation(cfg, monkeypatch, capsys):
    # 2026-07-03 is a Friday on day 3: daily, weekly, monthly, quarterly all due.
    rec = _EditionRecorder()

    def digest_run(cfg, kind="weekly", period=None, force=False):
        rec.digest_calls.append((kind, period))
        if kind == "weekly":
            raise RuntimeError("boom in weekly")
        return 0

    _patch_editions(monkeypatch, rec, digest_run=digest_run)

    # Must not propagate.
    worker.run_due_editions(cfg, today=datetime.date(2026, 7, 3))

    # weekly raised, yet the loop continued to monthly + quarterly.
    assert ("monthly", "2026-06") in rec.digest_calls
    assert ("quarterly", "2026-Q2") in rec.digest_calls
    err = capsys.readouterr().err
    assert "digest_weekly failed" in err


def test_run_due_editions_daily_failure_still_publishes_kb(cfg, monkeypatch):
    # A nonzero daily rc marks it failed but the kb window is still published
    # (there is no rc guard on the publish path).
    cfg.data["site"]["push"] = True
    rec = _EditionRecorder()

    def digest_run(cfg, kind="weekly", period=None, force=False):
        rec.digest_calls.append((kind, period))
        return 2  # nonzero

    _patch_editions(monkeypatch, rec, digest_run=digest_run)

    worker.run_due_editions(cfg, today=datetime.date(2026, 7, 10))

    assert ("daily", None) in rec.digest_calls
    assert rec.kb_calls == [datetime.date(2026, 7, 10)]


def test_run_due_editions_covered_short_circuits(cfg, monkeypatch):
    # _edition_covered True -> daily/weekly no-op even though due.
    rec = _EditionRecorder()
    _patch_editions(monkeypatch, rec, covered=True)

    worker.run_due_editions(cfg, today=datetime.date(2026, 7, 10))

    assert rec.digest_calls == []


def test_run_due_editions_defaults_today_to_real_date(cfg, monkeypatch):
    # today=None path: default to datetime.date.today(); we just assert it runs
    # without error and dispatches only currently-due kinds (patched no-ops).
    rec = _EditionRecorder()
    _patch_editions(monkeypatch, rec)

    worker.run_due_editions(cfg)  # today omitted

    for kind, _p in rec.digest_calls:
        assert kind in period_mod.KINDS


# ========================================================================== #
# _check_runtime_staleness — mtime comparison branches
# ========================================================================== #
def test_check_runtime_staleness_repo_newer_is_stale(tmp_path):
    runtime = tmp_path / "runtime" / "signal.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("{}")
    repo = tmp_path / "repo"
    repo_cfg = repo / "config" / "signal.json"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text("{}")

    # Make the repo copy strictly newer than the runtime copy.
    import os

    old = datetime.datetime(2026, 1, 1).timestamp()
    new = datetime.datetime(2026, 2, 1).timestamp()
    os.utime(runtime, (old, old))
    os.utime(repo_cfg, (new, new))

    stub = types.SimpleNamespace(path=runtime, blog_repo=repo)
    result = worker._check_runtime_staleness(stub)
    assert result is not None
    assert "STALE" in result
    assert str(repo_cfg) in result


def test_check_runtime_staleness_missing_repo_config_is_none(tmp_path):
    runtime = tmp_path / "signal.json"
    runtime.write_text("{}")
    stub = types.SimpleNamespace(path=runtime, blog_repo=tmp_path / "no_such_repo")
    assert worker._check_runtime_staleness(stub) is None


def test_check_runtime_staleness_same_resolved_path_is_none(tmp_path):
    repo = tmp_path / "repo"
    repo_cfg = repo / "config" / "signal.json"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text("{}")
    # Runtime path IS the repo config (running straight from the repo).
    stub = types.SimpleNamespace(path=repo_cfg, blog_repo=repo)
    assert worker._check_runtime_staleness(stub) is None


def test_check_runtime_staleness_runtime_newer_is_none(tmp_path):
    runtime = tmp_path / "runtime" / "signal.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("{}")
    repo = tmp_path / "repo"
    repo_cfg = repo / "config" / "signal.json"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text("{}")
    import os

    old = datetime.datetime(2026, 1, 1).timestamp()
    new = datetime.datetime(2026, 2, 1).timestamp()
    os.utime(repo_cfg, (old, old))
    os.utime(runtime, (new, new))  # runtime is the newer one -> not stale
    stub = types.SimpleNamespace(path=runtime, blog_repo=repo)
    assert worker._check_runtime_staleness(stub) is None


def test_check_runtime_staleness_oserror_is_none(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "signal.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("{}")
    repo = tmp_path / "repo"
    repo_cfg = repo / "config" / "signal.json"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text("{}")

    def boom(self, *a, **k):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(pathlib.Path, "stat", boom)
    stub = types.SimpleNamespace(path=runtime, blog_repo=repo)
    assert worker._check_runtime_staleness(stub) is None


# ========================================================================== #
# _publish_kb_window — date expansion over the daily window
# ========================================================================== #
def test_publish_kb_window_monday_expands_three_dates(cfg, monkeypatch):
    spy = Spy(ret=3)
    monkeypatch.setattr(publish_mod, "publish_kb_daily", spy)

    worker._publish_kb_window(cfg, datetime.date(2026, 7, 6))  # Monday

    (args, _kw) = spy.calls[0]
    passed_cfg, dates = args
    assert passed_cfg is cfg
    assert dates == [
        datetime.date(2026, 7, 3),
        datetime.date(2026, 7, 4),
        datetime.date(2026, 7, 5),
    ]


def test_publish_kb_window_tuesday_expands_one_date(cfg, monkeypatch):
    spy = Spy(ret=1)
    monkeypatch.setattr(publish_mod, "publish_kb_daily", spy)

    worker._publish_kb_window(cfg, datetime.date(2026, 7, 7))  # Tuesday

    (_args, _kw) = spy.calls[0]
    _passed_cfg, dates = _args
    assert dates == [datetime.date(2026, 7, 6)]


# ========================================================================== #
# _edition_covered — SQL coverage predicate (integration: real sqlite)
# ========================================================================== #
@pytest.mark.integration
def test_edition_covered_true_when_window_end_reaches_until(cfg, conn, seed):
    today = datetime.date(2026, 7, 7)
    key = period_mod.period_key("daily", today)  # "2026-07-07"
    _since, until = period_mod.window("daily", today)
    seed.digest(kind="daily", period_key=key, window_end=until)  # == until, >= holds
    assert worker._edition_covered(cfg, "daily", today) is True


@pytest.mark.integration
def test_edition_covered_false_when_window_end_short(cfg, conn, seed):
    today = datetime.date(2026, 7, 7)
    key = period_mod.period_key("daily", today)
    seed.digest(
        kind="daily",
        period_key=key,
        window_end="2026-07-06T00:00:00+00:00",  # earlier than until
    )
    assert worker._edition_covered(cfg, "daily", today) is False


@pytest.mark.integration
def test_edition_covered_false_when_no_row(cfg, conn, seed):
    today = datetime.date(2026, 7, 7)
    # A digest row for a DIFFERENT period_key must not satisfy the predicate.
    seed.digest(kind="daily", period_key="2026-07-01", window_end="2026-07-02T00:00:00+00:00")
    assert worker._edition_covered(cfg, "daily", today) is False


@pytest.mark.integration
def test_edition_covered_false_on_missing_db(tmp_path):
    stub = types.SimpleNamespace(db_path=tmp_path / "nonexistent.db")
    # connect_ro raises DBError -> broad except -> False.
    assert worker._edition_covered(stub, "daily", datetime.date(2026, 7, 7)) is False


# ========================================================================== #
# _job — gating + stage dispatch + exception isolation
# ========================================================================== #
def _patch_load(monkeypatch, cfg):
    monkeypatch.setattr(worker.config_mod, "load", lambda *a, **k: cfg)


def test_job_curate_deferred_on_closed_downtime(cfg, monkeypatch, capsys):
    _patch_load(monkeypatch, cfg)
    monkeypatch.setattr(worker.downtime, "is_open", lambda c, need_gb=0.0: (False, "on battery"))
    curate_spy = Spy()
    monkeypatch.setattr(curate_mod, "run", curate_spy)

    worker._job("curate")()

    assert not curate_spy.called
    assert "curate deferred (downtime: on battery)" in capsys.readouterr().out


def test_job_curate_deferred_on_quota_hold(cfg, monkeypatch, capsys):
    _patch_load(monkeypatch, cfg)
    monkeypatch.setattr(worker.downtime, "is_open", lambda c, need_gb=0.0: (True, "open"))
    monkeypatch.setattr(quota_mod, "status", lambda: (True, "usage limited until 14:00"))
    curate_spy = Spy()
    monkeypatch.setattr(curate_mod, "run", curate_spy)

    worker._job("curate")()

    assert not curate_spy.called
    assert "curate deferred (usage limited until 14:00)" in capsys.readouterr().out


def test_job_curate_runs_when_gates_open(cfg, monkeypatch):
    _patch_load(monkeypatch, cfg)
    monkeypatch.setattr(worker.downtime, "is_open", lambda c, need_gb=0.0: (True, "open"))
    monkeypatch.setattr(quota_mod, "status", lambda: (False, ""))
    curate_spy = Spy()
    monkeypatch.setattr(curate_mod, "run", curate_spy)

    worker._job("curate")()

    assert curate_spy.called
    assert curate_spy.calls[0][0][0] is cfg


def test_job_curate_reads_need_gb_from_config(cfg, monkeypatch):
    _patch_load(monkeypatch, cfg)
    captured = {}

    def is_open(c, need_gb=0.0):
        captured["need_gb"] = need_gb
        return (False, "on battery")

    monkeypatch.setattr(worker.downtime, "is_open", is_open)
    monkeypatch.setattr(curate_mod, "run", Spy())

    worker._job("curate")()

    # signal.min.json downtime.min_free_gb_curation == 14
    assert captured["need_gb"] == 14.0


def test_job_ingest_runs_ungated(cfg, monkeypatch):
    _patch_load(monkeypatch, cfg)
    spy = Spy()
    monkeypatch.setattr(pipeline_mod, "run", spy)

    worker._job("ingest")()

    assert spy.called
    assert spy.calls[0][0][0] is cfg


def test_job_ingest_swallows_and_logs_stage_error(cfg, monkeypatch, capsys):
    _patch_load(monkeypatch, cfg)
    monkeypatch.setattr(pipeline_mod, "run", Spy(raise_exc=RuntimeError("kaboom")))

    worker._job("ingest")()  # must not raise

    err = capsys.readouterr().err
    assert "ingest failed" in err
    assert "kaboom" in err


def test_job_score_runs_with_show_zero(cfg, monkeypatch):
    _patch_load(monkeypatch, cfg)
    spy = Spy()
    monkeypatch.setattr(score_mod, "run", spy)

    worker._job("score")()

    assert spy.called
    assert spy.calls[0][1].get("show") == 0


def test_job_fetch_runs(cfg, monkeypatch):
    _patch_load(monkeypatch, cfg)
    spy = Spy()
    monkeypatch.setattr(fetch_mod, "run", spy)

    worker._job("fetch")()

    assert spy.called


def test_job_editions_sets_and_clears_lock_even_on_error(cfg, monkeypatch, capsys):
    _patch_load(monkeypatch, cfg)
    monkeypatch.setattr(worker.downtime, "is_open", lambda c, need_gb=0.0: (True, "open"))
    monkeypatch.setattr(quota_mod, "status", lambda: (False, ""))
    events = []
    monkeypatch.setattr(worker.downtime, "set_digest_lock", lambda: events.append("set"))
    monkeypatch.setattr(worker.downtime, "clear_digest_lock", lambda: events.append("clear"))
    monkeypatch.setattr(worker, "run_due_editions", Spy(raise_exc=RuntimeError("digest blew up")))

    worker._job("editions")()  # must not raise

    assert events == ["set", "clear"]  # lock set before run, cleared in finally
    err = capsys.readouterr().err
    assert "editions failed" in err


def test_job_editions_deferred_on_closed_downtime_does_not_touch_lock(cfg, monkeypatch):
    _patch_load(monkeypatch, cfg)
    monkeypatch.setattr(worker.downtime, "is_open", lambda c, need_gb=0.0: (False, "thermal"))
    set_spy = Spy()
    run_spy = Spy()
    monkeypatch.setattr(worker.downtime, "set_digest_lock", set_spy)
    monkeypatch.setattr(worker, "run_due_editions", run_spy)

    worker._job("editions")()

    assert not set_spy.called
    assert not run_spy.called


def test_job_publish_refresh_runs_when_push_true(cfg, monkeypatch):
    cfg.data["site"]["push"] = True
    _patch_load(monkeypatch, cfg)
    spy = Spy()
    monkeypatch.setattr(publish_mod, "refresh", spy)

    worker._job("publish_refresh")()

    assert spy.called


def test_job_publish_refresh_skips_when_push_false(cfg, monkeypatch):
    cfg.data["site"]["push"] = False
    _patch_load(monkeypatch, cfg)
    spy = Spy()
    monkeypatch.setattr(publish_mod, "refresh", spy)

    worker._job("publish_refresh")()

    assert not spy.called


def test_job_kb_trends_runs_when_push_true(cfg, monkeypatch):
    cfg.data["site"]["push"] = True
    _patch_load(monkeypatch, cfg)
    spy = Spy()
    monkeypatch.setattr(publish_mod, "publish_trends", spy)

    worker._job("kb_trends")()

    assert spy.called


def test_job_kb_trends_skips_when_push_false(cfg, monkeypatch):
    cfg.data["site"]["push"] = False
    _patch_load(monkeypatch, cfg)
    spy = Spy()
    monkeypatch.setattr(publish_mod, "publish_trends", spy)

    worker._job("kb_trends")()

    assert not spy.called


def test_job_backup_runs_and_reports_dest(cfg, monkeypatch, capsys):
    _patch_load(monkeypatch, cfg)
    spy = Spy(ret=pathlib.Path("/tmp/backup/signal-2026.db"))
    monkeypatch.setattr(db_mod, "backup", spy)

    worker._job("backup")()

    assert spy.called
    assert spy.calls[0][0][0] == cfg.db_path
    assert "db backup -> /tmp/backup/signal-2026.db" in capsys.readouterr().out


def test_job_unknown_name_is_ungated_noop(cfg, monkeypatch):
    _patch_load(monkeypatch, cfg)
    # An unrecognized name is ungated (need_key is None) and matches no stage
    # branch: the runner falls through the if/elif chain and does nothing.
    worker._job("mystery")()  # no error, no output side effects


def test_job_runner_has_descriptive_name():
    assert worker._job("ingest").__name__ == "job_ingest"
    assert worker._job("curate").__name__ == "job_curate"


# ========================================================================== #
# _heartbeat — writes an ISO timestamp, never raises on fs error
# ========================================================================== #
def test_heartbeat_writes_parseable_iso_timestamp(monkeypatch):
    worker._heartbeat()

    hb = worker.config_mod.STATE_DIR / "heartbeat"
    assert hb.exists()
    parsed = datetime.datetime.fromisoformat(hb.read_text().strip())
    assert parsed.tzinfo is not None  # written in UTC


def test_heartbeat_swallows_write_error(monkeypatch, capsys):
    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)

    worker._heartbeat()  # must not raise

    assert "heartbeat write failed" in capsys.readouterr().err


# ========================================================================== #
# run(cfg) — wires the full job set with configured cadences
# ========================================================================== #
def test_run_wires_all_jobs_with_configured_cadences(cfg, monkeypatch):
    result = _run_with_fakes(cfg, monkeypatch, stale=None)

    assert result.rc == 0
    assert result.scheduler.started is True

    expected_ids = {
        "heartbeat",
        "ingest",
        "score",
        "fetch",
        "curate",
        "digest_editions",
        "quota_probe",
        "publish_refresh",
        "kb_trends",
        "backup",
    }
    assert set(result.jobs_by_id) == expected_ids

    def minutes(job_id):
        return result.jobs_by_id[job_id]["trigger"].kwargs["minutes"]

    # Interval cadences come straight from cfg.cadences / downtime config.
    assert minutes("heartbeat") == 5
    assert minutes("ingest") == 45
    assert minutes("score") == 60
    assert minutes("fetch") == 90
    assert minutes("curate") == 15
    assert minutes("digest_editions") == 30  # downtime.editions_interval_min
    assert minutes("publish_refresh") == 90
    assert minutes("quota_probe") == 15  # default (not in cfg)

    # Cron jobs carry the configured crontab + digest timezone.
    kb = result.jobs_by_id["kb_trends"]["trigger"]
    assert kb.crontab == "45 7 * * fri"
    assert kb.timezone == worker.DIGEST_TZ
    backup = result.jobs_by_id["backup"]["trigger"]
    assert backup.crontab == worker.BACKUP_CRON
    assert backup.timezone == worker.DIGEST_TZ


def test_run_job_defaults_are_overlap_guarded(cfg, monkeypatch):
    result = _run_with_fakes(cfg, monkeypatch, stale=None)
    assert result.scheduler.job_defaults == {
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 3600,  # cfg.cadences.misfire_grace_sec
    }


def test_run_registers_sigterm_and_sigint(cfg, monkeypatch):
    result = _run_with_fakes(cfg, monkeypatch, stale=None)
    signums = {s for (s, _h) in result.handlers}
    assert signums == {worker.stdlib_signal.SIGTERM, worker.stdlib_signal.SIGINT}
    # Same handler bound to both.
    handlers = {h for (_s, h) in result.handlers}
    assert len(handlers) == 1


def test_run_term_handler_shuts_scheduler_down_then_exits(cfg, monkeypatch):
    result = _run_with_fakes(cfg, monkeypatch, stale=None)
    term = result.handlers[0][1]

    with pytest.raises(SystemExit) as ei:
        term(worker.stdlib_signal.SIGTERM, None)

    assert ei.value.code == 0
    assert result.scheduler.shutdown_calls == [False]  # wait=False


@pytest.mark.integration
def test_run_logs_health_on_stale_runtime_config(cfg, conn, monkeypatch, capsys):
    stale_msg = "runtime config is STALE: repo is newer than runtime"
    result = _run_with_fakes(cfg, monkeypatch, stale=stale_msg)

    assert result.rc == 0
    assert "WARN" in capsys.readouterr().out

    # The stale warning is persisted to the health table (real sqlite write).
    ro = db_mod.connect_ro(cfg.db_path)
    try:
        rows = ro.execute(
            "SELECT job, level, message FROM health WHERE job='worker'"
        ).fetchall()
    finally:
        ro.close()
    assert any(r["level"] == "warn" and stale_msg in r["message"] for r in rows)


def test_run_stale_health_log_failure_is_swallowed(cfg, monkeypatch, capsys):
    def boom(_path):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(db_mod, "connect_rw", boom)
    # The stale warning + failed health write must never kill startup.
    result = _run_with_fakes(cfg, monkeypatch, stale="stale runtime config")
    assert result.rc == 0
    assert "WARN" in capsys.readouterr().out


def test_run_swallows_keyboardinterrupt_from_start(cfg, monkeypatch):
    class RaisingScheduler:
        def __init__(self, job_defaults=None, **kw):
            self.job_defaults = job_defaults

        def add_job(self, *a, **k):
            pass

        def start(self):
            raise KeyboardInterrupt()

        def shutdown(self, wait=True):
            pass

        def get_job(self, job_id):
            return None

    import apscheduler.schedulers.blocking as _bl
    import apscheduler.triggers.cron as _cr
    import apscheduler.triggers.interval as _iv

    monkeypatch.setattr(_bl, "BlockingScheduler", RaisingScheduler)
    monkeypatch.setattr(_cr, "CronTrigger", FakeCron)
    monkeypatch.setattr(_iv, "IntervalTrigger", FakeInterval)
    monkeypatch.setattr(worker.stdlib_signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker, "_check_runtime_staleness", lambda c: None)

    # start() raising KeyboardInterrupt is caught and run() returns cleanly.
    assert worker.run(cfg) == 0


# ========================================================================== #
# _quota_probe — the closure captured from run()'s add_job registration
# ========================================================================== #
def _get_quota_probe(cfg, monkeypatch):
    result = _run_with_fakes(cfg, monkeypatch, stale=None)
    return result.scheduler, result.jobs_by_id["quota_probe"]["func"]


def test_quota_probe_noop_when_no_hold_exists(cfg, monkeypatch, capsys):
    _sched, qp = _get_quota_probe(cfg, monkeypatch)
    monkeypatch.setattr(quota_mod, "exists", lambda: False)
    clear_spy = Spy()
    monkeypatch.setattr(quota_mod, "clear", clear_spy)
    capsys.readouterr()  # drop the run() startup banner

    qp()  # returns early, no probe

    assert not clear_spy.called
    assert capsys.readouterr().out == ""


def test_quota_probe_reports_active_hold(cfg, monkeypatch, capsys):
    _sched, qp = _get_quota_probe(cfg, monkeypatch)
    monkeypatch.setattr(quota_mod, "exists", lambda: True)
    monkeypatch.setattr(quota_mod, "status", lambda: (True, "limited until 15:00"))

    qp()

    assert "usage hold active (limited until 15:00)" in capsys.readouterr().out


def test_quota_probe_still_limited_when_probe_fails(cfg, monkeypatch, capsys):
    _sched, qp = _get_quota_probe(cfg, monkeypatch)
    monkeypatch.setattr(quota_mod, "exists", lambda: True)
    monkeypatch.setattr(quota_mod, "status", lambda: (False, ""))
    monkeypatch.setattr(worker.config_mod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(adapter_mod, "probe_auth", lambda c: (False, "auth error"))
    clear_spy = Spy()
    monkeypatch.setattr(quota_mod, "clear", clear_spy)

    qp()

    assert not clear_spy.called
    assert "usage still limited or probe failed" in capsys.readouterr().out


def test_quota_probe_clears_hold_and_pulls_jobs_forward(cfg, monkeypatch, capsys):
    sched, qp = _get_quota_probe(cfg, monkeypatch)
    monkeypatch.setattr(quota_mod, "exists", lambda: True)
    monkeypatch.setattr(quota_mod, "status", lambda: (False, ""))
    monkeypatch.setattr(worker.config_mod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(adapter_mod, "probe_auth", lambda c: (True, "ok"))
    clear_spy = Spy()
    monkeypatch.setattr(quota_mod, "clear", clear_spy)

    jobs = {}

    def provider(job_id):
        job = jobs.setdefault(job_id, FakeJob(job_id))
        return job

    sched._job_provider = provider

    qp()

    assert clear_spy.called
    assert "subscription usage restored" in capsys.readouterr().out
    # curate + digest_editions were pulled forward via job.modify.
    assert set(jobs) == {"curate", "digest_editions"}
    for job in jobs.values():
        assert job.modified and "next_run_time" in job.modified[0]


def test_quota_probe_restored_but_no_matching_jobs(cfg, monkeypatch, capsys):
    # Restored path where get_job returns None for every id: the hold is still
    # cleared and the message printed, but no job.modify happens.
    _sched, qp = _get_quota_probe(cfg, monkeypatch)
    monkeypatch.setattr(quota_mod, "exists", lambda: True)
    monkeypatch.setattr(quota_mod, "status", lambda: (False, ""))
    monkeypatch.setattr(worker.config_mod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(adapter_mod, "probe_auth", lambda c: (True, "ok"))
    clear_spy = Spy()
    monkeypatch.setattr(quota_mod, "clear", clear_spy)
    # default _job_provider returns None -> `if job:` is False for both ids.

    qp()

    assert clear_spy.called
    assert "subscription usage restored" in capsys.readouterr().out


def test_quota_probe_swallows_exceptions(cfg, monkeypatch, capsys):
    _sched, qp = _get_quota_probe(cfg, monkeypatch)

    def boom():
        raise RuntimeError("probe explode")

    monkeypatch.setattr(quota_mod, "exists", boom)

    qp()  # must not raise

    assert "quota_probe failed" in capsys.readouterr().err
