"""Signal CLI dispatcher.

Run from the repo root:  python3 -m signalpipe <command> [options]

Commands map 1:1 to pipeline stages so each is independently runnable and
testable; the worker schedules the same entry points.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from . import __version__
from . import config as config_mod


def _load_cfg(args: argparse.Namespace) -> "config_mod.Config":
    return config_mod.load(args.config)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    import json
    import os
    import sqlite3 as _sq

    from . import db as db_mod

    cfg = _load_cfg(args)
    print("signal %s" % __version__)
    print("config: %s" % cfg.path)
    print("db:     %s" % cfg.db_path)

    warn = db_mod.sqlite_version_warning()
    print("sqlite: %s%s" % (_sq.sqlite_version, "  [WARN: %s]" % warn if warn else ""))

    cli_bin = cfg.backend.get("cli_bin", "")
    print(
        "claude: %s (%s)"
        % (cli_bin, "found" if os.path.exists(cli_bin) else "MISSING")
    )
    def _tier_desc(t: str) -> str:
        b = cfg.backend_for(t)
        if b == "local":
            ms = cfg.local_models_for(t)
            return "%s=local:%s" % (t, ms[0] if ms else "?")
        return "%s=%s:%s" % (t, b, cfg.model_for(t, b))

    print("routing: %s" % ", ".join(
        _tier_desc(t) for t in ("triage", "judge", "write", "digest")))
    from . import downtime as _dt

    ok, why = _dt.is_open(cfg)
    print("downtime gate: %s%s" % (
        "OPEN" if ok else "CLOSED", "" if ok else "  (%s)" % why))

    changed = cfg.tracked_changes()
    if changed:
        print("tracked inputs changed: %s" % ", ".join(changed))

    if not cfg.db_path.exists():
        print("db not created yet — run `python3 -m signalpipe ingest` first")
        return 0

    conn = db_mod.connect_ro(cfg.db_path)
    try:
        for table in (
            "sources",
            "items",
            "clusters",
            "articles",
            "curations",
            "digests",
        ):
            n = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            print("%-10s %d" % (table, n))
        row = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE enabled=1 AND verified_at IS NOT NULL"
        ).fetchone()
        print("%-10s %d" % ("verified", row[0]))
        today = conn.execute(
            "SELECT cli_usd, api_usd, calls FROM spend WHERE day=date('now')"
        ).fetchone()
        if today:
            print(
                "spend today: cli $%.4f  api $%.4f  (%d calls)"
                % (today["cli_usd"], today["api_usd"], today["calls"])
            )
        recent = conn.execute(
            "SELECT ts, job, level, message FROM health ORDER BY id DESC LIMIT 5"
        ).fetchall()
        if recent:
            print("recent health:")
            for r in recent:
                print("  %s [%s] %s: %s" % (r["ts"][:19], r["level"], r["job"], r["message"]))
    finally:
        conn.close()
    return 0


# --------------------------------------------------------------------------
# stage commands (lazy imports keep startup light and optional deps optional)
# --------------------------------------------------------------------------

def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import pipeline

    return pipeline.run(_load_cfg(args), only=args.source, limit=args.limit)


def cmd_score(args: argparse.Namespace) -> int:
    from . import score

    return score.run(_load_cfg(args), show=args.show)


def cmd_fetch(args: argparse.Namespace) -> int:
    from . import fetch_article

    return fetch_article.run(_load_cfg(args), limit=args.limit)


def cmd_curate(args: argparse.Namespace) -> int:
    from . import curate

    return curate.run(_load_cfg(args), limit=args.limit, dry_run=args.dry_run)


def cmd_digest(args: argparse.Namespace) -> int:
    from . import digest

    return digest.run(_load_cfg(args), kind=args.kind, period=args.period,
                      force=args.force)


def cmd_retag(args: argparse.Namespace) -> int:
    from . import retag

    return retag.run(_load_cfg(args), dry_run=args.dry_run, limit=args.limit)


def cmd_backfill(args: argparse.Namespace) -> int:
    from . import backfill

    cfg = _load_cfg(args)
    if args.backfill_cmd == "fetch":
        return backfill.fetch(cfg, since=args.since, until=args.until,
                              top_n=args.top_n)
    if args.backfill_cmd == "curate":
        return backfill.curate(cfg, since=args.since, until=args.until,
                               top_n=args.top_n, dry_run=args.dry_run)
    if args.backfill_cmd == "merge":
        return backfill.merge(cfg, src_db=args.src)
    print("unknown backfill subcommand", file=sys.stderr)
    return 2


def cmd_publish(args: argparse.Namespace) -> int:
    from . import publish

    backfill_since = args.since if args.backfill_kb else None
    if args.backfill_kb and not args.since:
        print("--backfill-kb requires --since DATE", file=sys.stderr)
        return 2
    return publish.run(
        _load_cfg(args), what=args.what, push=not args.no_push,
        backfill_since=backfill_since,
    )


def cmd_promote(args: argparse.Namespace) -> int:
    from . import promote

    return promote.run(
        _load_cfg(args), week=args.week, target=args.target, apply=args.apply,
        publish_now=args.publish_now,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server

    return server.run(_load_cfg(args), host=args.host, port=args.port)


def cmd_worker(args: argparse.Namespace) -> int:
    from . import worker

    return worker.run(_load_cfg(args))


def cmd_sources(args: argparse.Namespace) -> int:
    from .ingest import registry

    cfg = _load_cfg(args)
    if args.sources_cmd == "stats":
        return registry.stats(cfg)
    if args.sources_cmd == "seed":
        return registry.seed(cfg)
    if args.sources_cmd == "probe":
        return registry.probe_cmd(
            cfg, candidates=args.candidates, url=args.url, import_ok=args.import_ok
        )
    if args.sources_cmd == "import":
        return registry.import_cmd(cfg, path=args.path)
    if args.sources_cmd == "expand":
        return registry.expand(cfg)
    if args.sources_cmd == "bulk":
        from .ingest import bulk_import

        return bulk_import.run(
            cfg, manifest_path=args.manifest, only_entry=args.entry,
            wave_size=args.wave_size, max_workers=args.max_workers,
            limit=args.limit, no_resume=args.no_resume,
        )
    print("unknown sources subcommand", file=sys.stderr)
    return 2


def cmd_retention(args: argparse.Namespace) -> int:
    from . import retention

    cfg = _load_cfg(args)
    retention.run(cfg, dry_run=args.dry_run, vacuum=args.vacuum)
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from . import db as db_mod

    cfg = _load_cfg(args)
    try:
        dest = db_mod.backup(cfg.db_path, backup_dir=args.dir, keep=args.keep)
    except db_mod.DBError as e:
        print("backup failed: %s" % e, file=sys.stderr)
        return 1
    print("backup -> %s" % dest)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from . import installer

    return installer.install(_load_cfg(args), start=not args.no_start)


def cmd_sync(args: argparse.Namespace) -> int:
    from . import installer

    return installer.sync(_load_cfg(args), restart=args.restart)


# --------------------------------------------------------------------------
# downtime control (local-only operation)
# --------------------------------------------------------------------------

def cmd_pause(args: argparse.Namespace) -> int:
    import datetime

    from . import downtime

    cfg = _load_cfg(args)
    secs = downtime.parse_duration(args.duration)
    until = downtime.pause(secs, reason="manual")
    unloaded = downtime.ollama_unload(cfg)  # free the 9-47 GB immediately
    print("paused local pipeline for %d min (until %s)" % (
        round(secs / 60),
        datetime.datetime.fromtimestamp(until).strftime("%H:%M")))
    if unloaded:
        print("unloaded model(s): %s" % ", ".join(unloaded))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from . import downtime

    downtime.resume()
    print("resumed — local stages run again at the next downtime window")
    return 0


def cmd_downtime(args: argparse.Namespace) -> int:
    from . import downtime

    print(downtime.status(_load_cfg(args)))
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="signalpipe",
        description="Signal — local tech+AI feed-curation pipeline",
    )
    ap.add_argument("--config", type=pathlib.Path, default=None,
                    help="path to signal.json (default: config/signal.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="config, db, spend, health summary").set_defaults(
        fn=cmd_status
    )

    p = sub.add_parser("ingest", help="poll sources, dedup, store")
    p.add_argument("--source", help="single source slug")
    p.add_argument("--limit", type=int, default=None, help="max sources this run")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("score", help="deterministic scoring -> finalists")
    p.add_argument("--show", type=int, default=20, help="print top N")
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("fetch", help="fetch+extract finalist articles, resolve paywalls")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("curate", help="LLM curation of finalists (spends)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true", help="show what would be curated")
    p.set_defaults(fn=cmd_curate)

    p = sub.add_parser("digest", help="build an Opus digest (spends)")
    p.add_argument("--kind", choices=["daily", "weekly", "monthly",
                                      "quarterly", "yearly"],
                   default="weekly", help="digest cadence (default: weekly)")
    p.add_argument("--period", help="period key (2026-06-10 | 2026-W24 | "
                                    "2026-06 | 2026-Q2 | 2026); default: "
                                    "the period due today")
    p.add_argument("--force", action="store_true",
                   help="regenerate an existing period")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("retag", help="backfill v2 taxonomy onto historical curations")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="show the assignment distribution; write nothing")
    p.add_argument("--limit", type=int, default=None, help="cap rows (testing)")
    p.set_defaults(fn=cmd_retag)

    p = sub.add_parser("backfill", help="ONE-TIME historical re-fetch + "
                       "all-Opus curation (run against a COPY DB via --config)")
    pb = p.add_subparsers(dest="backfill_cmd", required=True)
    for _name in ("fetch", "curate"):
        sp = pb.add_parser(_name)
        sp.add_argument("--since", required=True, help="start date YYYY-MM-DD")
        sp.add_argument("--until", required=True,
                        help="end date YYYY-MM-DD (exclusive)")
        sp.add_argument("--top-n", dest="top_n", type=int, default=40,
                        help="max clusters per day (fetch 40 / curate 30)")
        if _name == "curate":
            sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                            help="show per-day candidate counts; write nothing")
    sp = pb.add_parser("merge", help="fold copy rows back into the LIVE DB")
    sp.add_argument("--src", required=True, help="path to the backfill copy DB")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("publish", help="export picks/stats/kb/digests to the site repo")
    r = sub.add_parser("retention", help="prune old uncurated data from the db")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--vacuum", action="store_true")
    r.set_defaults(fn=cmd_retention)

    p.add_argument("--what", choices=["picks", "stats", "spotlight", "kb", "digests", "all"],
                   default="all")
    p.add_argument("--no-push", dest="no_push", action="store_true",
                   help="commit locally without pushing")
    p.add_argument("--backfill-kb", dest="backfill_kb", action="store_true",
                   help="with --what kb: regenerate ledgers since --since")
    p.add_argument("--since", help="start date (YYYY-MM-DD) for --backfill-kb")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("promote", help="publish a staged digest to Ghost (/signal/)")
    p.add_argument("--week", help="ISO week (default: latest staged)")
    p.add_argument("--target", choices=["local", "prod"], default="local")
    p.add_argument("--apply", action="store_true", help="actually publish (default: dry run)")
    p.add_argument("--publish-now", dest="publish_now", action="store_true",
                   help="prod only: publish immediately instead of creating a draft")
    p.set_defaults(fn=cmd_promote)

    p = sub.add_parser("serve", help="run the feed/dashboard server")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(fn=cmd_serve)

    sub.add_parser("worker", help="run the scheduler (all jobs)").set_defaults(
        fn=cmd_worker
    )

    p = sub.add_parser("pause", help="pause local stages so you can use the Mac "
                       "(e.g. `pause 2h` / 30m / 90s)")
    p.add_argument("duration", nargs="?", default=None,
                   help="2h | 30m | 90s | bare number = minutes (default 2h)")
    p.set_defaults(fn=cmd_pause)

    sub.add_parser("resume", help="resume local stages now").set_defaults(
        fn=cmd_resume)

    sub.add_parser("downtime", help="show the downtime-gate status").set_defaults(
        fn=cmd_downtime)

    p = sub.add_parser("sources", help="registry management")
    ps = p.add_subparsers(dest="sources_cmd", required=True)
    ps.add_parser("stats", help="counts by category/tier/verified")
    ps.add_parser("seed", help="import sources.json + sources.opml into the DB")
    pp = ps.add_parser("probe", help="probe candidate homepages/feeds for valid feeds")
    pp.add_argument("--candidates", type=pathlib.Path,
                    help="JSON file: [{name, homepage, feed_url?, topics?, tier?, category?}]")
    pp.add_argument("--url", help="probe a single homepage/feed URL")
    pp.add_argument("--import-ok", dest="import_ok", action="store_true",
                    help="import verified candidates into the registry")
    pi = ps.add_parser("import", help="merge a verified-candidates JSON into registry files")
    pi.add_argument("path", type=pathlib.Path)
    ps.add_parser("expand", help="run built-in expanders (Techmeme lb.opml, arXiv, reddit)")
    pb = ps.add_parser("bulk", help="bulk-import from curated public lists (probe-verified)")
    pb.add_argument("--manifest", type=pathlib.Path, default=None,
                    help="manifest path (default: config/bulk_sources.json)")
    pb.add_argument("--entry", help="run a single manifest entry by name")
    pb.add_argument("--wave-size", type=int, default=200)
    pb.add_argument("--max-workers", type=int, default=16)
    pb.add_argument("--limit", type=int, default=None,
                    help="cap candidates per entry (testing)")
    pb.add_argument("--no-resume", action="store_true",
                    help="ignore checkpoints and reprocess")
    p.set_defaults(fn=cmd_sources)

    p = sub.add_parser("backup", help="snapshot the DB (VACUUM INTO) + prune old backups")
    p.add_argument("--dir", type=pathlib.Path, default=None,
                   help="backup directory (default: ~/Documents/backup/signal)")
    p.add_argument("--keep", type=int, default=8,
                   help="how many snapshots to keep (default: 8)")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("install", help="install launchd agents (TCC-safe runtime copy)")
    p.add_argument("--no-start", action="store_true")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("sync", help="refresh the runtime copy after repo edits")
    p.add_argument("--restart", action="store_true", help="restart agents after sync")
    p.set_defaults(fn=cmd_sync)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except config_mod.ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
