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


def cmd_runs(args: argparse.Namespace) -> int:
    """Recent job runs, each tagged with the config that produced it — so you can
    see whether a knob change actually moved the outcomes (attribution)."""
    import json
    import sqlite3

    from . import db as db_mod

    cfg = _load_cfg(args)
    if not cfg.db_path.exists():
        print("db not created yet — run `python3 -m signalpipe ingest` first")
        return 0
    conn = db_mod.connect_ro(cfg.db_path)
    try:
        try:
            runs = db_mod.recent_runs(conn, job=args.job, limit=args.limit)
        except sqlite3.OperationalError:
            # A pre-attribution DB has no runs table yet; the worker's first
            # job after picking up this build creates it (read-only here).
            runs = []
        if not runs:
            print("no runs recorded yet — records begin on the first job cycle "
                  "after the worker picks up this build.")
            return 0
        print("current config: %s" % cfg.config_fingerprint()["hash"])
        print("%-19s  %-7s  %-12s  %s" % ("when (UTC)", "job", "config", "outcome"))
        prev = None
        for r in reversed(runs):  # oldest-first: a config change reads top-down
            h = r["config_hash"]
            mark = "   <- config changed" if prev and h != prev else ""
            prev = h
            try:
                st = json.loads(r["stats"])
            except (ValueError, TypeError):
                st = {}
            summary = ", ".join(
                "%s=%s" % (k, st[k]) for k in list(st)[:5]
                if not isinstance(st[k], (dict, list))
            )
            print("%-19s  %-7s  %-12s  %s%s" % (
                r["ts"][:19], r["job"], h, summary, mark))
    finally:
        conn.close()
    return 0


def _repo_root(cfg) -> pathlib.Path:
    """The site repo root (where eval/ and kb/ live). Prefer the configured
    site repo; fall back to this checkout's root for in-repo dev runs."""
    import os

    repo_raw = cfg.site.get("repo") if getattr(cfg, "site", None) else None
    if repo_raw:
        return pathlib.Path(os.path.expanduser(repo_raw))
    return config_mod.REPO_ROOT.parent


def cmd_eval(args: argparse.Namespace) -> int:
    """Curation eval sets: score the current judge against a versioned gold
    corpus. Repo-side only; `run` defaults to the free local backend ($0)."""
    import datetime
    import json

    from . import db as db_mod
    from . import eval as eval_mod

    cfg = _load_cfg(args)
    repo_root = _repo_root(cfg)

    if args.eval_cmd == "grow":
        if not cfg.db_path.exists():
            print("db not created yet — run `python3 -m signalpipe ingest` first")
            return 0
        conn = db_mod.connect_ro(cfg.db_path)
        try:
            cands = eval_mod.build_candidates(conn, limit=args.limit)
        finally:
            conn.close()
        gold = eval_mod.load_gold(repo_root)
        before = len(gold)
        gold = eval_mod.grow(gold, cands, args.k)
        eval_mod.save_gold(repo_root, gold)
        print("gold: %d -> %d (+%d)" % (before, len(gold), len(gold) - before))
        return 0

    if args.eval_cmd == "label":
        gold = eval_mod.load_gold(repo_root)
        human = {"featured": bool(args.featured), "relevance": int(args.relevance)}
        if args.category:
            human["category"] = args.category
        gold = eval_mod.label(gold, args.id, human)
        eval_mod.save_gold(repo_root, gold)
        print("labeled %s (featured=%s, relevance=%d)"
              % (args.id, human["featured"], human["relevance"]))
        return 0

    if args.eval_cmd == "report":
        r = eval_mod.latest_result(repo_root)
        if not r:
            print("no eval results yet — run `python3 -m signalpipe eval run`")
            return 0
        print(json.dumps(r["metrics"], indent=2, sort_keys=True))
        return 0

    # run
    gold = eval_mod.load_gold(repo_root)
    if not gold:
        print("gold set empty — run `python3 -m signalpipe eval grow` first")
        return 0
    conn = db_mod.connect_ro(cfg.db_path) if cfg.db_path.exists() else None
    try:
        date = args.date or datetime.date.today().isoformat()
        res = eval_mod.run(gold, cfg=cfg, backend=args.backend, date=date, conn=conn)
    finally:
        if conn is not None:
            conn.close()
    eval_mod.write_result(repo_root, date, res)
    m = res["metrics"]
    print("eval %s (%s, n=%d): agreement=%.2f precision=%.2f recall=%.2f "
          "mae=%.2f cat=%.2f" % (
              date, m["backend"], m["n"], m["agreement_featured"],
              m["featured_precision"], m["featured_recall"],
              m["relevance_mae"], m["category_accuracy"]))
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    """Refresh the reader-facing Library (entity wiki). Writes working-tree
    files repo-side; the caller commits. The worker's own job commits+pushes."""
    import datetime

    from . import db as db_mod
    from . import library as library_mod
    from . import publish as publish_mod

    cfg = _load_cfg(args)
    repo_root = _repo_root(cfg)
    if not cfg.db_path.exists():
        print("db not created yet — run `python3 -m signalpipe ingest` first")
        return 0
    conn = db_mod.connect_ro(cfg.db_path)
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        if args.library_cmd == "propose":
            reg = library_mod.load_registry(repo_root)
            new = library_mod.propose_entities(conn, reg, args.k, now)
            for e in new:
                print("%-22s %-10s" % (e["slug"], e["type"]))
            print("%d proposable entit%s"
                  % (len(new), "y" if len(new) == 1 else "ies"))
            return 0
        out = library_mod.refresh(conn, repo_root, args.k, now)
        writes = dict(out["kb_writes"])
        for m in out["entities"]:
            writes["src/content/library/%s.md" % m["slug"]] = (
                publish_mod._library_frontmatter(m) + m["body_md"])
        for rel, content in writes.items():
            p = pathlib.Path(repo_root) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        print("library: wrote %d file(s), %d page(s), %d in index"
              % (len(writes), len(out["entities"]), len(out["index"])))
        return 0
    finally:
        conn.close()


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
    if args.sources_cmd == "attribution":
        return registry.attribution(cfg)
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

    p = sub.add_parser("runs", help="recent job runs tagged with the config that "
                                    "produced them (tuning attribution)")
    p.add_argument("--job", default=None,
                   help="filter to one job: ingest|score|fetch|curate|digest")
    p.add_argument("--limit", type=int, default=40, help="max runs to show")
    p.set_defaults(fn=cmd_runs)

    pe = sub.add_parser("eval", help="curation eval sets: score the judge vs. a "
                        "versioned gold corpus (repo-side; local backend = $0)")
    pes = pe.add_subparsers(dest="eval_cmd", required=True)
    per = pes.add_parser("run", help="replay the judge over the gold set + record")
    per.add_argument("--backend", default="local",
                     choices=["local", "api", "subscription"])
    per.add_argument("--date", default=None)
    peg = pes.add_parser("grow", help="add provisional gold candidates from the DB")
    peg.add_argument("-k", type=int, default=5)
    peg.add_argument("--limit", type=int, default=50)
    pel = pes.add_parser("label", help="confirm/correct a gold label by id")
    pel.add_argument("--id", required=True)
    pel.add_argument("--featured", action="store_true")
    pel.add_argument("--relevance", type=int, required=True)
    pel.add_argument("--category", default=None)
    pes.add_parser("report", help="print the latest eval metrics")
    pe.set_defaults(fn=cmd_eval)

    pl = sub.add_parser("library", help="refresh the reader-facing Library "
                        "(entity wiki built from coverage)")
    pls = pl.add_subparsers(dest="library_cmd", required=True)
    plr = pls.add_parser("refresh", help="grow the registry + rebuild a few pages")
    plr.add_argument("-k", type=int, default=3)
    plp = pls.add_parser("propose", help="show proposable entities with coverage")
    plp.add_argument("-k", type=int, default=5)
    pl.set_defaults(fn=cmd_library)

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
    ps.add_parser("attribution", help="per-source pick counts; flags zero-pick dead weight")
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
