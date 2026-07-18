"""The scheduler process (sole DB writer). One APScheduler BlockingScheduler
under launchd: ingest -> score -> fetch -> curate on configured cadences,
plus the multi-cadence digest engine (ONE 'editions' dispatcher job — see
run_due_editions), the site publish refresher, the weekly kb trends update,
and a weekly DB backup. Overlap-guarded (max_instances=1, coalesce=True),
sleep-aware (misfire_grace_time), SIGTERM-clean. The server process stays
untouched by anything that happens here.
"""

from __future__ import annotations

import os
import signal as stdlib_signal  # package is `signalpipe`, so this is stdlib
import socket
import sys
import time
import traceback

from . import config as config_mod
from . import downtime

DIGEST_TZ = "America/Los_Angeles"
DIGEST_GRACE_SEC = 8 * 3600
# Days 1..N of the month during which a missed monthly/quarterly/yearly run
# is still caught up by the editions dispatcher.
CATCH_UP_DAYS = 7
BACKUP_CRON = "0 9 * * sun"


# Live job-start times, read by the stuck_check job. GIL-atomic dict ops.
RUNNING: dict = {}

# A job past its limit is logged; past 2x, the worker exits non-zero and
# launchd (KeepAlive SuccessfulExit=false) restarts it inside 30s. WAL +
# publish's crash-dirt cleanup make the hard exit safe. This converts
# "silent multi-day starvation" (Jul 2-4) into one logged restart.
STUCK_LIMITS_SEC = {
    "ingest": 40 * 60, "score": 30 * 60, "fetch": 80 * 60,
    "curate": 3 * 3600, "editions": 3 * 3600, "publish_refresh": 20 * 60,
}


def _stuck_check() -> None:
    now = time.time()
    for name, started in list(RUNNING.items()):
        limit = STUCK_LIMITS_SEC.get(name)
        if not limit:
            continue
        elapsed = now - started
        if elapsed > 2 * limit:
            print("[worker] %s stuck %ds (2x limit) — exiting for restart"
                  % (name, elapsed), file=sys.stderr, flush=True)
            os._exit(70)
        if elapsed > limit:
            try:
                cfg = config_mod.load()
                conn = db_mod_top.connect_rw(cfg.db_path)
                try:
                    db_mod_top.log_health(
                        conn, name, "error",
                        "stuck: running %ds (limit %ds)" % (elapsed, limit))
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 — never let telemetry kill us
                pass


from . import db as db_mod_top  # noqa: E402 — after the constants above


def _job(name):
    """Wrap a stage so one failing run never kills the scheduler, and each
    run reloads config (picks up edits without a restart)."""

    def runner():
        RUNNING[name] = time.time()
        try:
            cfg = config_mod.load()
            # Local heavy stages run ONLY during machine downtime (AC + user-idle
            # + free RAM). Light stages (ingest/score/fetch/publish/backup) stay
            # ungated so the funnel keeps filling and the site stays fresh.
            need_key = {"curate": "min_free_gb_curation",
                        "editions": "min_free_gb_digest"}.get(name)
            if need_key is not None:
                need = float(cfg.data.get("downtime", {}).get(need_key, 0))
                ok, why = downtime.is_open(cfg, need_gb=need)
                if not ok:
                    print("[worker] %s deferred (downtime: %s)" % (name, why),
                          flush=True)
                    return
                # Subscription usage-limit hold: same defer shape as downtime.
                # The quota_probe job clears the hold and pulls these jobs
                # forward the moment usage is back.
                from .llm import quota

                held, hold_why = quota.status()
                if held:
                    print("[worker] %s deferred (%s)" % (name, hold_why),
                          flush=True)
                    return
            if name == "ingest":
                from .ingest import pipeline

                pipeline.run(cfg)
            elif name == "score":
                from . import score

                score.run(cfg, show=0)
            elif name == "fetch":
                from . import fetch_article

                fetch_article.run(cfg)
            elif name == "curate":
                from . import curate

                curate.run(cfg)
            elif name == "editions":
                downtime.set_digest_lock()  # keeps a 14B curate off the 47B digest
                try:
                    run_due_editions(cfg)
                finally:
                    downtime.clear_digest_lock()
            elif name == "publish_refresh":
                from . import publish

                if cfg.site.get("push"):
                    publish.refresh(cfg)
            elif name == "kb_trends":
                from . import publish

                if cfg.site.get("push"):
                    publish.publish_trends(cfg)
            elif name == "momentum":
                from . import publish

                if cfg.site.get("push"):
                    publish.publish_momentum(cfg)
            elif name == "backup":
                from . import db as db_mod

                dest = db_mod.backup(cfg.db_path)
                print("[worker] db backup -> %s" % dest, flush=True)
            elif name == "retention":
                from . import retention
                import datetime as _dt

                # monthly VACUUM on the first Sunday, right after the backup
                retention.run(cfg, vacuum=_dt.date.today().day <= 7)
        except Exception:  # noqa: BLE001 — isolation per job run
            print("[worker] %s failed:\n%s" % (name, traceback.format_exc()),
                  file=sys.stderr, flush=True)
            # Failures must reach the dashboard, not just stderr (the Jul 2-4
            # outage was invisible because only the log knew).
            try:
                conn = db_mod_top.connect_rw(config_mod.load().db_path)
                try:
                    db_mod_top.log_health(conn, name, "error",
                                          traceback.format_exc()[-2000:])
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 — logging never masks the error
                pass
        finally:
            RUNNING.pop(name, None)

    runner.__name__ = "job_%s" % name
    return runner


def run_due_editions(cfg, today=None) -> None:
    """The single digest dispatcher, replacing one cron job per kind.

    Why one job: monthly/quarterly/yearly are HIERARCHICAL (each consumes
    the tier below it), but APScheduler runs distinct job ids concurrently —
    after a laptop-sleep misfire the old per-kind jobs all fired at wake and
    raced, so a quarterly could gather its monthlies before the new monthly
    committed. Running every kind sequentially, in tier order daily ->
    weekly -> monthly -> quarterly -> yearly inside ONE job, makes the
    ordering structural instead of relying on cron stagger.

    Per-kind gates:
      daily/weekly         period.is_due(today) — the dispatcher cron plus
                           the 8h misfire grace covers these short cadences.
      monthly/quarterly/   catch-up: due when the period's first-weekday
      yearly               run-date has passed and today is within days
                           1..CATCH_UP_DAYS. digest.run is idempotent (it
                           skips periods that already have a full-window
                           row), so re-dispatching across the window is
                           safe and retries a failed/missed first run.

    When a hierarchical lower tier was due but produced no row, its
    dependent higher tiers are deferred to the catch-up window instead of
    baking a permanently incomplete digest.
    """
    import datetime

    from . import digest, period

    today = today or datetime.date.today()
    failed = set()
    for kind in period.KINDS:  # daily, weekly, monthly, quarterly, yearly
        try:
            if kind in ("daily", "weekly"):
                if not period.is_due(kind, today):
                    continue
                if _edition_covered(cfg, kind, today):
                    continue  # already made — cheap no-op for the interval run
                rc = digest.run(cfg, kind=kind)
                if rc != 0:
                    failed.add(kind)
                if kind == "daily" and cfg.site.get("push"):
                    _publish_kb_window(cfg, today)
                continue

            run_date = period.due_run_date(kind, today)
            if (run_date is None or today < run_date
                    or today.day > CATCH_UP_DAYS):
                continue
            deps = {"quarterly": ("monthly",),
                    "yearly": ("monthly", "quarterly")}.get(kind, ())
            if failed.intersection(deps):
                print("[worker] %s digest deferred — lower tier(s) %s "
                      "failed this run; the 1st-%d catch-up window retries"
                      % (kind, ", ".join(sorted(failed & set(deps))),
                         CATCH_UP_DAYS), flush=True)
                continue
            rc = digest.run(cfg, kind=kind,
                            period=period.period_key(kind, run_date))
            if rc != 0:
                failed.add(kind)
        except Exception:  # noqa: BLE001 — one kind never kills the rest
            failed.add(kind)
            print("[worker] digest_%s failed:\n%s"
                  % (kind, traceback.format_exc()),
                  file=sys.stderr, flush=True)


def _edition_covered(cfg, kind, today) -> bool:
    """True when a digest row already covers this period's full window. Lets the
    gated interval editions dispatcher no-op cheaply once the edition exists,
    instead of re-running digest.run + kb publish every cycle. Mirrors the
    window-coverage check inside digest.run."""
    from . import db as db_mod, period

    key = period.period_key(kind, today)
    _, until = period.window(kind, today)
    try:
        conn = db_mod.connect_ro(cfg.db_path)
    except Exception:  # noqa: BLE001 — never let the guard kill the dispatcher
        return False
    try:
        row = conn.execute(
            "SELECT window_end FROM digests WHERE kind=? AND period_key=?",
            (kind, key)).fetchone()
        return bool(row and (row["window_end"] or "") >= until)
    finally:
        conn.close()


def _publish_kb_window(cfg, today) -> None:
    """Publish a kb ledger for EVERY date the daily window covers — Monday's
    window is [Friday, Monday), so Monday publishes Fri+Sat+Sun ledgers
    (Friday's run covers Thursday; weekend curations exist because
    ingest/curate run 7 days a week)."""
    import datetime

    from . import period, publish

    since, until = period.window("daily", today)
    start = datetime.date.fromisoformat(since[:10])
    end = datetime.date.fromisoformat(until[:10])
    dates = []
    d = start
    while d < end:
        dates.append(d)
        d += datetime.timedelta(days=1)
    publish.publish_kb_daily(cfg, dates)


def _check_runtime_staleness(cfg):
    """Warning string when the runtime copy's config is older than the
    repo's config/signal.json (repo edited without `sync` — the worker only
    ever reads the runtime copy), else None."""
    try:
        runtime_cfg = cfg.path
        repo_cfg = cfg.blog_repo / "config" / "signal.json"
        if not repo_cfg.exists():
            return None
        if repo_cfg.resolve() == runtime_cfg.resolve():
            return None  # running straight from the repo
        if repo_cfg.stat().st_mtime > runtime_cfg.stat().st_mtime:
            return ("runtime config is STALE: %s is newer than %s — run "
                    "`python3 -m signalpipe sync` so the worker picks up "
                    "the repo edit" % (repo_cfg, runtime_cfg))
    except OSError:
        return None
    return None


def _heartbeat() -> None:
    """Touch a heartbeat file every few minutes so an external watchdog can tell
    the scheduler LOOP is alive — not just the process. A hung APScheduler still
    shows a live pid, which launchd KeepAlive (exit-only) cannot catch; the
    watchdog restarts the worker when this timestamp goes stale."""
    import datetime

    hb = config_mod.STATE_DIR / "heartbeat"
    try:
        config_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        hb.write_text(
            datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n")
    except OSError as e:  # a heartbeat hiccup must never kill the scheduler
        print("[worker] heartbeat write failed: %s" % e,
              file=sys.stderr, flush=True)
    from . import monitoring

    monitoring.ping("worker")


def run(cfg) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    # Belt-and-braces for any library that bypasses httpx timeouts
    # (feedparser URL mode et al) — a hung socket must never hold a job slot.
    socket.setdefaulttimeout(30)

    from . import db as db_mod

    cad = cfg.cadences
    grace = int(cad.get("misfire_grace_sec", 3600))
    jitter = int(cad.get("jitter_sec", 120))
    sched = BlockingScheduler(
        job_defaults={
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": grace,
        }
    )

    # Loudly surface a stale runtime copy (live incident: curation ran for
    # hours against an old $5 spend cap after a repo config edit).
    stale = _check_runtime_staleness(cfg)
    if stale:
        print("[worker] WARN: %s" % stale, flush=True)
        try:
            conn = db_mod.connect_rw(cfg.db_path)
            try:
                db_mod.log_health(conn, "worker", "warn", stale)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — the warning must never kill startup
            pass

    # Stagger first runs so startup isn't a thundering herd.
    import datetime

    now = datetime.datetime.now()

    def soon(minutes):
        return now + datetime.timedelta(minutes=minutes)

    # Heartbeat first: proves the scheduler loop is alive for the watchdog.
    sched.add_job(_stuck_check, IntervalTrigger(minutes=10),
                  id="stuck_check", name="stuck_check")
    sched.add_job(_job("retention"),
                  CronTrigger.from_crontab("0 10 * * sun", timezone=DIGEST_TZ),
                  id="retention", name="retention")
    sched.add_job(_heartbeat, IntervalTrigger(minutes=5),
                  id="heartbeat", next_run_time=soon(0.1))
    sched.add_job(_job("ingest"), IntervalTrigger(
        minutes=int(cad.get("ingest_min", 45)), jitter=jitter),
        next_run_time=soon(0.2), id="ingest")
    sched.add_job(_job("score"), IntervalTrigger(
        minutes=int(cad.get("score_min", 60)), jitter=jitter),
        next_run_time=soon(10), id="score")
    sched.add_job(_job("fetch"), IntervalTrigger(
        minutes=int(cad.get("fetch_min", 90)), jitter=jitter),
        next_run_time=soon(15), id="fetch")
    sched.add_job(_job("curate"), IntervalTrigger(
        minutes=int(cad.get("curate_min", 120)), jitter=jitter),
        next_run_time=soon(25), id="curate")

    # ONE dispatcher job for ALL digest kinds (see run_due_editions). It
    # fires on the daily cron — the earliest digest cron — with a long
    # grace + coalesce so a sleeping laptop still gets exactly one run at
    # wake. The per-kind crons in config remain as documentation of the
    # intended cadence; the in-job gates are the real authority.
    # Editions run on a gated INTERVAL, not a fixed cron: the daily covers
    # YESTERDAY (its window closed at midnight), so producing it at the first
    # downtime window of the day is correct. run_due_editions is idempotent
    # (_edition_covered short-circuits once made), so frequent firing is cheap.
    editions_int = int(
        cfg.data.get("downtime", {}).get("editions_interval_min", 30))
    sched.add_job(
        _job("editions"),
        IntervalTrigger(minutes=editions_int, jitter=jitter),
        id="digest_editions",
        next_run_time=soon(3),
        coalesce=True,
    )

    # Usage-limit recovery probe. Free no-op while no hold exists or while the
    # hold window is still running. Once retry_at passes, one tiny probe call
    # per interval answers "is usage back?": success clears the hold (in
    # backend_cli) and pulls curate/editions forward immediately; another
    # limit hit re-arms the hold with a fresh window. This is what makes the
    # pipeline resilient to running out of Max-plan quota mid-day.
    def _quota_probe():
        try:
            from .llm import adapter, quota

            if not quota.exists():
                return
            held, why = quota.status()
            if held:
                print("[worker] usage hold active (%s)" % why, flush=True)
                return
            cfg2 = config_mod.load()
            ok, msg = adapter.probe_auth(cfg2)
            if not ok:
                print("[worker] usage still limited or probe failed — will "
                      "re-check: %s" % str(msg)[:200], flush=True)
                return
            quota.clear()
            print("[worker] subscription usage restored — pulling "
                  "curate/editions forward", flush=True)
            for job_id in ("curate", "digest_editions"):
                job = sched.get_job(job_id)
                if job:
                    job.modify(next_run_time=datetime.datetime.now())
        except Exception:  # noqa: BLE001 — the probe must never kill the loop
            print("[worker] quota_probe failed:\n%s" % traceback.format_exc(),
                  file=sys.stderr, flush=True)

    quota_probe_min = int(cad.get("quota_probe_min", 15))
    sched.add_job(_quota_probe, IntervalTrigger(minutes=quota_probe_min),
                  id="quota_probe", next_run_time=soon(2))

    sched.add_job(_job("publish_refresh"), IntervalTrigger(
        minutes=int(cad.get("publish_refresh_min", 240)), jitter=jitter),
        id="publish_refresh")
    sched.add_job(
        _job("kb_trends"),
        CronTrigger.from_crontab(
            cad.get("kb_trends_cron", "45 7 * * fri"), timezone=DIGEST_TZ),
        id="kb_trends",
        misfire_grace_time=DIGEST_GRACE_SEC,
        coalesce=True,
    )
    sched.add_job(
        _job("momentum"),
        CronTrigger.from_crontab(
            cad.get("momentum_cron", "40 7 * * *"), timezone=DIGEST_TZ),
        id="momentum",
        misfire_grace_time=DIGEST_GRACE_SEC,
        coalesce=True,
    )
    sched.add_job(
        _job("backup"),
        CronTrigger.from_crontab(
            cad.get("backup_cron", BACKUP_CRON), timezone=DIGEST_TZ),
        id="backup",
        misfire_grace_time=DIGEST_GRACE_SEC,
        coalesce=True,
    )

    def _term(signum, frame):  # noqa: ARG001
        print("[worker] SIGTERM — shutting down cleanly", flush=True)
        try:
            sched.shutdown(wait=False)
        finally:
            sys.exit(0)

    stdlib_signal.signal(stdlib_signal.SIGTERM, _term)
    stdlib_signal.signal(stdlib_signal.SIGINT, _term)

    print("[worker] starting (LOCAL, downtime-gated): ingest/%dm score/%dm "
          "fetch/%dm curate/%dm publish/%dm editions/%dm backup@'%s'" % (
              int(cad.get("ingest_min", 45)), int(cad.get("score_min", 60)),
              int(cad.get("fetch_min", 90)), int(cad.get("curate_min", 120)),
              int(cad.get("publish_refresh_min", 240)), editions_int,
              cad.get("backup_cron", BACKUP_CRON)), flush=True)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(run(config_mod.load()))
