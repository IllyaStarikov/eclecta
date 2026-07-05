"""Site publisher — the ONLY module that touches the site repo.

Everything the public site sees flows through here: digest markdown under
src/content/digests/<kind>/, machine-readable src/data/picks.json and
src/data/stats.json, and the kb/ working notes. All repo writes go through
git_publish(), which serializes via a flock lockfile, rebases on the remote
BEFORE writing (never auto-resolves, never force-pushes), commits with the
pipeline author, and pushes with a single pull --rebase retry.

Privacy guarantees: archive.* URLs are never exported (defensive scrub on
every link field), and stats carry no spend dollars and no health/error
text.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from . import db as db_mod
from .config import STATE_DIR

LOCK_PATH = STATE_DIR / "publish.lock"
AUTHOR_NAME = "Signal Pipeline"
AUTHOR_EMAIL = "signal@starikov.io"

_ARCHIVE_RE = re.compile(r"(^|[./])archive\.(ph|today|is|org)(/|$)", re.I)

# Paths in the site repo the pipeline regenerates on every publish. Leftover
# dirt under these (a crash between write and commit) is safe to discard;
# dirt anywhere else means a human is mid-edit and we refuse to touch it.
PIPELINE_OWNED = ("src/data/", "src/content/digests/", "kb/")


class PublishError(Exception):
    pass


# ---------------------------------------------------------------------------
# config + helpers
# ---------------------------------------------------------------------------

def site_config(cfg) -> Dict[str, Any]:
    """Validate the site block at publish time (never at config load)."""
    site = dict(cfg.site)
    if not site.get("repo"):
        raise PublishError("config site.repo is not set")
    repo = pathlib.Path(os.path.expanduser(site["repo"]))
    if not repo.is_dir():
        raise PublishError("site repo %s does not exist" % repo)
    site["repo_path"] = repo
    site.setdefault("branch", "main")
    site.setdefault("remote", "origin")
    site.setdefault("picks_window_days", 7)
    site.setdefault("picks_limit", 60)
    return site


def no_archive(url: Optional[str]) -> Optional[str]:
    """archive.* URLs never leave the pipeline; scrub defensively."""
    if url and _ARCHIVE_RE.search(url):
        return None
    return url


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _iso_days_ago(days: int) -> str:
    """Python-generated ISO bound for SQL comparisons. Stored timestamps are
    Python isoformat ('...T...+00:00'); SQLite's datetime('now', ...) yields
    space-separated strings that compare wrong against them ('T' > ' '), so
    every window bound must be generated here and passed as a parameter."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=days)
    ).isoformat()


def _iso_hours_ago(hours: int) -> str:
    """Same convention as _iso_days_ago, hour-granular (spotlight window)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours)
    ).isoformat()


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------

def export_picks(conn: sqlite3.Connection, cfg) -> List[Dict[str, Any]]:
    """The current picks window as plain dicts for src/data/picks.json."""
    site = cfg.site
    since = _iso_days_ago(int(site.get("picks_window_days", 7)))
    limit = int(site.get("picks_limit", 60))
    min_rel = int(site.get("picks_min_relevance",
                           cfg.funnel.get("min_relevance_for_feed", 6)))
    from . import topics
    rows = conn.execute(
        "SELECT c.id, c.title, c.first_seen, c.surface_count, c.score, "
        "c.canonical_url, c.story_id, "
        "cu.relevance_score, cu.why_it_matters, cu.notes, cu.summary, "
        "cu.channels, cu.category, cu.subcategories, cu.novelty, cu.audience, "
        "cu.model_used, cu.curated_at, "
        "a.source_url, a.read_url, a.read_kind, a.paywalled "
        "FROM curations cu JOIN clusters c ON c.id=cu.cluster_id "
        "LEFT JOIN articles a ON a.cluster_id=cu.cluster_id "
        "WHERE cu.status='done' AND cu.skip=0 AND cu.relevance_score >= ? "
        "AND cu.curated_at >= ? "
        "ORDER BY cu.relevance_score DESC, cu.curated_at DESC LIMIT ?",
        (min_rel, since, limit),
    ).fetchall()

    picks = []
    for r in rows:
        surfaces = [
            {
                "url": no_archive(s["url"]),
                "points": s["points"],
                "comments": s["comments"],
                # the site contract (Pick.astro / lib/schema) names this "name"
                "name": s["name"],
            }
            for s in conn.execute(
                "SELECT s.url, s.points, s.comments, src.name "
                "FROM surfaces s JOIN sources src ON src.id=s.source_id "
                "WHERE s.cluster_id=? "
                "ORDER BY s.points IS NULL, s.points DESC LIMIT 8",
                (r["id"],),
            ).fetchall()
            if no_archive(s["url"])
        ]
        # Canonical contract: source_url is the REQUIRED primary link (the
        # original source; cluster canonical URL when no article row yet);
        # free_link is null unless a distinct legit free read exists. The
        # site schema demands a non-empty string, so when every candidate is
        # archive-scrubbed we fall back to the best surface link, and skip
        # the pick entirely rather than ever emitting "".
        source_url = (no_archive(r["source_url"])
                      or no_archive(r["canonical_url"]))
        if not source_url:
            source_url = next((s["url"] for s in surfaces if s["url"]), None)
        if not source_url:
            continue
        read_url = no_archive(r["read_url"])
        free_link = read_url if (read_url and read_url != source_url) else None
        channels = json.loads(r["channels"] or "[]")
        category = r["category"]
        subcategories = (
            json.loads(r["subcategories"]) if r["subcategories"] else []
        )
        if not category:  # un-retagged row: derive deterministically
            tax = topics.match_taxonomy(r["title"] or "", channels)
            category = tax["category"]
            subcategories = subcategories or tax["subcategories"]
        picks.append({
            "id": r["id"],
            "story_id": r["story_id"],
            "title": r["title"],
            "relevance": r["relevance_score"],
            "score": r["score"],
            "why": r["why_it_matters"],
            "notes": json.loads(r["notes"] or "[]"),
            "summary": r["summary"],
            "channels": channels,
            "category": category,
            "subcategories": subcategories,
            "state": "confident",
            "novelty": r["novelty"],
            "audience": r["audience"],
            "source_url": source_url,
            "read_kind": r["read_kind"],
            "free_link": free_link,
            "paywalled": bool(r["paywalled"]),
            "surfaces": surfaces,
            "sources_count": r["surface_count"],
            "first_seen": r["first_seen"],
            "published_at": r["curated_at"],
            "curated_at": r["curated_at"],
            "model": r["model_used"],
        })
    return picks


# Spotlight: stories gaining traction across the internet right now.
# Pure SQL over clusters/surfaces/items — no LLM, no schema migration.
# Site contract: eclecta src/lib/schema.ts spotlightFileSchema.
SPOTLIGHT_DEFAULTS = {"window_hours": 48, "min_surfaces": 3,
                      "limit": 8, "velocity_n": 3}
SPOTLIGHT_W_BREADTH = 1.0      # per distinct surface
SPOTLIGHT_W_ENGAGEMENT = 0.5   # x log1p(points + comments)
SPOTLIGHT_W_VELOCITY = 6.0     # / hours from 1st to Nth distinct source


def export_spotlight(conn: sqlite3.Connection, cfg) -> Dict[str, Any]:
    """src/data/spotlight.json: clusters with unusual cross-surface breadth
    and velocity inside the window, curated or not. Velocity comes from
    items.ingested_at (immutable first sighting per source) — NEVER from
    surfaces.seen_at, which is overwritten on every re-ingest. Same
    archive-URL scrub as picks; no spend, no health text."""
    sp = dict(SPOTLIGHT_DEFAULTS)
    sp.update(cfg.site.get("spotlight") or {})
    min_rel = int(cfg.site.get("picks_min_relevance",
                               cfg.funnel.get("min_relevance_for_feed", 6)))
    since = _iso_hours_ago(int(sp["window_hours"]))

    rows = conn.execute(
        "SELECT c.id, c.story_id, c.title, c.canonical_url, c.first_seen, "
        "c.surface_count, "
        "cu.status AS cu_status, cu.skip AS cu_skip, "
        "cu.relevance_score AS cu_rel "
        "FROM clusters c LEFT JOIN curations cu ON cu.cluster_id = c.id "
        "WHERE c.first_seen >= ? AND c.surface_count >= ? "
        "ORDER BY c.surface_count DESC LIMIT 40",
        (since, int(sp["min_surfaces"])),
    ).fetchall()

    items: List[Dict[str, Any]] = []
    for r in rows:
        surfaces = [
            {
                "url": no_archive(s["url"]),
                "name": s["name"],
                "points": s["points"],
                "comments": s["comments"],
            }
            for s in conn.execute(
                "SELECT s.url, s.points, s.comments, src.name "
                "FROM surfaces s JOIN sources src ON src.id=s.source_id "
                "WHERE s.cluster_id=? "
                "ORDER BY s.points IS NULL, s.points DESC LIMIT 5",
                (r["id"],),
            ).fetchall()
            if no_archive(s["url"])
        ]
        url = no_archive(r["canonical_url"]) or next(
            (s["url"] for s in surfaces if s["url"]), None)
        if not url or not r["title"]:
            continue

        eng = conn.execute(
            "SELECT COALESCE(SUM(points),0) AS pts, "
            "COALESCE(SUM(comments),0) AS com "
            "FROM surfaces WHERE cluster_id=?", (r["id"],)).fetchone()
        points, comments = int(eng["pts"] or 0), int(eng["com"] or 0)

        firsts = [
            row[0] for row in conn.execute(
                "SELECT MIN(ingested_at) FROM items WHERE cluster_id=? "
                "GROUP BY source_id ORDER BY 1", (r["id"],)).fetchall()
            if row[0]
        ]
        velocity = None
        n = int(sp["velocity_n"])
        if len(firsts) >= n:
            try:
                t0 = datetime.datetime.fromisoformat(firsts[0])
                tn = datetime.datetime.fromisoformat(firsts[n - 1])
                velocity = round(max((tn - t0).total_seconds(), 0.0) / 3600.0, 1)
            except ValueError:
                pass

        traction = (
            SPOTLIGHT_W_BREADTH * (r["surface_count"] or 0)
            + SPOTLIGHT_W_ENGAGEMENT * math.log1p(points + comments)
            + (SPOTLIGHT_W_VELOCITY / max(velocity, 1.0)
               if velocity is not None else 0.0)
        )
        curated = (r["cu_status"] == "done" and not r["cu_skip"])
        pick_id = (r["id"] if curated and (r["cu_rel"] or 0) >= min_rel
                   else None)
        items.append({
            "story_id": r["story_id"],
            "title": r["title"],
            "canonical_url": url,
            "first_seen": r["first_seen"],
            "surface_count": r["surface_count"],
            "surfaces": surfaces,
            "velocity_hours": velocity,
            "points": points,
            "comments": comments,
            "score": round(traction, 2),
            "curated": curated,
            "pick_id": pick_id,
        })

    items.sort(key=lambda x: -x["score"])
    return {
        "generated_at": _now_iso(),
        "window_hours": int(sp["window_hours"]),
        "items": items[: int(sp["limit"])],
    }


def export_stats(conn: sqlite3.Connection, cfg) -> Dict[str, Any]:
    """Pipeline stats for src/data/stats.json.
    NO spend dollars, NO archive URLs, NO health/error text."""
    def one(sql, args=()):
        return conn.execute(sql, args).fetchone()[0]

    site = cfg.site
    window_since = _iso_days_ago(int(site.get("picks_window_days", 7)))
    week_since = _iso_days_ago(7)
    min_rel = int(site.get("picks_min_relevance",
                           cfg.funnel.get("min_relevance_for_feed", 6)))

    by_category = {
        r["category"]: r["n"]
        for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM sources WHERE enabled=1 "
            "GROUP BY category ORDER BY n DESC"
        ).fetchall()
    }
    by_tier = {
        str(r["tier"]): r["n"]
        for r in conn.execute(
            "SELECT tier, COUNT(*) AS n FROM sources WHERE enabled=1 "
            "GROUP BY tier ORDER BY tier"
        ).fetchall()
    }

    avg_rel = conn.execute(
        "SELECT AVG(relevance_score) FROM curations "
        "WHERE status='done' AND skip=0 AND curated_at >= ?",
        (week_since,),
    ).fetchone()[0]

    digests_by_kind = {
        r["kind"]: r["n"]
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM digests GROUP BY kind"
        ).fetchall()
    }
    latest = conn.execute(
        "SELECT kind, period_key, title, generated_at FROM digests "
        "ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()

    channels = []
    for slug in cfg.channels:
        if slug == "everything":
            continue
        n = one(
            "SELECT COUNT(*) FROM curations WHERE status='done' AND skip=0 "
            "AND relevance_score >= ? AND curated_at >= ? "
            "AND channels LIKE ?",
            (min_rel, window_since, '%"' + slug + '"%'),
        )
        channels.append({"slug": slug, "picks_current": n})

    top_surfaces = [
        {"name": r["name"], "clusters": r["clusters"]}
        for r in conn.execute(
            "SELECT src.name AS name, COUNT(DISTINCT s.cluster_id) AS clusters "
            "FROM surfaces s JOIN sources src ON src.id=s.source_id "
            "WHERE s.seen_at >= ? "
            "GROUP BY src.name ORDER BY clusters DESC LIMIT 10",
            (week_since,),
        ).fetchall()
    ]

    pipeline = {
        "items_total": one("SELECT COUNT(*) FROM items"),
        "clusters_total": one("SELECT COUNT(*) FROM clusters"),
        "curations_done": one(
            "SELECT COUNT(*) FROM curations WHERE status='done' AND skip=0"
        ),
        "items_7d": one(
            "SELECT COUNT(*) FROM items WHERE ingested_at >= ?",
            (week_since,),
        ),
        "curated_7d": one(
            "SELECT COUNT(*) FROM curations WHERE status='done' "
            "AND skip=0 AND curated_at >= ?",
            (week_since,),
        ),
    }
    # OMIT (never null) when no curations completed in the window: the site
    # schema is z.number().optional() — absent key passes, null is rejected.
    if avg_rel is not None:
        pipeline["avg_relevance_7d"] = round(avg_rel, 2)

    return {
        "generated_at": _now_iso(),
        "site_name": site.get("name", "Signal"),
        "sources": {
            "total": one("SELECT COUNT(*) FROM sources"),
            "enabled": one("SELECT COUNT(*) FROM sources WHERE enabled=1"),
            "verified": one(
                "SELECT COUNT(*) FROM sources "
                "WHERE enabled=1 AND verified_at IS NOT NULL"
            ),
            "by_category": by_category,
            "by_tier": by_tier,
        },
        "pipeline": pipeline,
        "digests": {
            "total": one("SELECT COUNT(*) FROM digests"),
            "by_kind": digests_by_kind,
            "latest": {
                "kind": latest["kind"],
                "period": latest["period_key"],
                "title": latest["title"],
                "date": (latest["generated_at"] or "")[:10],
            } if latest else None,
        },
        "channels": channels,
        "top_surfaces_7d": top_surfaces,
        "models": {t: cfg.model_for(t) for t in ("triage", "deep", "digest")},
    }


# ---------------------------------------------------------------------------
# digest markdown
# ---------------------------------------------------------------------------

def digest_display_date(kind: str, period_key: str) -> datetime.date:
    """Representative date for a period: daily=the day, weekly=Monday of the
    ISO week, monthly/quarterly/yearly=first day of the period."""
    if kind == "daily":
        return datetime.date.fromisoformat(period_key)
    if kind == "weekly":
        year, wnum = period_key.split("-W")
        return datetime.date.fromisocalendar(int(year), int(wnum), 1)
    if kind == "monthly":
        year, month = (int(x) for x in period_key.split("-"))
        return datetime.date(year, month, 1)
    if kind == "quarterly":
        year, qn = period_key.split("-Q")
        return datetime.date(int(year), (int(qn) - 1) * 3 + 1, 1)
    return datetime.date(int(period_key), 1, 1)  # yearly


def _human_date(d: datetime.date) -> str:
    return "%s %d, %d" % (d.strftime("%B"), d.day, d.year)


def digest_title(kind: str, period_key: str) -> str:
    d = digest_display_date(kind, period_key)
    if kind == "daily":
        return "%s, %s" % (d.strftime("%A"), _human_date(d))
    if kind == "weekly":
        return "Week of %s" % _human_date(d)
    if kind == "monthly":
        return "%s %d" % (d.strftime("%B"), d.year)
    if kind == "quarterly":
        return "Q%d %d" % ((d.month - 1) // 3 + 1, d.year)
    return period_key  # yearly: "2026"


def _fm_quote(s: str) -> str:
    return '"%s"' % (s or "").replace("\\", "\\\\").replace('"', '\\"')


def write_digest_md(cfg, row, blurb: Optional[str] = None) -> Tuple[str, str]:
    """digests row -> (repo-relative path, markdown with frontmatter).
    Writing happens inside git_publish (after the rebase), never here."""
    kind = row["kind"]
    key = row["period_key"]
    try:
        items = len(json.loads(row["cluster_ids"] or "[]"))
    except ValueError:
        items = 0
    row_blurb = row["blurb"] if "blurb" in row.keys() else None
    blurb = blurb or row_blurb or row["title"] or ""
    date = digest_display_date(kind, key).isoformat()
    # Lowercase filename: Astro's glob loader lowercases route ids, and the
    # site repo lives on a case-insensitive filesystem — a case-mismatched
    # path makes the publisher's own `git add` a silent no-op.
    relpath = "src/content/digests/%s/%s.md" % (kind, key.lower())
    # period MUST be quoted: the site schema requires a string, but unquoted
    # daily keys (2026-06-10) YAML-parse as Date and yearly keys (2026) as
    # number, which fails astro build. date stays UNQUOTED — the schema is
    # z.coerce.date() and wants the bare ISO date.
    model = row["model_used"] if "model_used" in row.keys() else None
    fm = [
        "title: %s" % _fm_quote(digest_title(kind, key)),
        "kind: %s" % kind,
        "period: %s" % _fm_quote(key),
        "date: %s" % date,
        "blurb: %s" % _fm_quote(blurb),
        "items: %d" % items,
    ]
    if model:  # provenance: the site colophon states who wrote the edition
        fm.append("model: %s" % _fm_quote(model))
    content = "---\n%s\n---\n\n%s\n" % (
        "\n".join(fm), (row["body_md"] or "").strip(),
    )
    return relpath, content


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def _git(repo: pathlib.Path, *args: str) -> "subprocess.CompletedProcess":
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "GIT_TERMINAL_PROMPT": "0",
    })
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True, text=True, env=env,
    )


def _dirty_paths(repo: pathlib.Path) -> List[Tuple[str, str]]:
    """[(porcelain status code, path)] for every dirty/untracked entry."""
    st = _git(repo, "status", "--porcelain")
    entries = []
    for line in st.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:  # rename: the new path is what's dirty
            path = path.split(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        entries.append((code, path))
    return entries


def _clean_pipeline_dirt(repo: pathlib.Path,
                         conn: Optional[sqlite3.Connection]) -> None:
    """A crash between write_text and commit leaves the site repo dirty,
    which would make the pre-write rebase fail forever (misreported as a
    conflict). Pipeline-owned paths are regenerated on every publish, so we
    discard that dirt; any OTHER dirty path is a human's work in progress —
    refuse with a distinct 'working tree dirty' error, never touch it."""
    entries = _dirty_paths(repo)
    if not entries:
        return
    foreign = sorted(p for _, p in entries if not p.startswith(PIPELINE_OWNED))
    if foreign:
        err = ("site repo working tree is dirty outside pipeline-owned "
               "paths (%s) — refusing to publish until resolved"
               % ", ".join(foreign[:5]))
        if conn is not None:
            db_mod.log_health(conn, "publish", "error", err)
        raise PublishError(err)
    # Only pipeline-owned dirt: unstage everything (safe — nothing else is
    # dirty), then restore tracked files and delete untracked leftovers.
    _git(repo, "reset", "-q", "HEAD")
    for code, path in _dirty_paths(repo):
        target = repo / path
        if code == "??":
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    target.unlink()
                except OSError:
                    pass
        else:
            _git(repo, "checkout", "--", path)
    warn = ("discarded stale pipeline-owned changes in the site repo "
            "(crash between write and commit): %s"
            % ", ".join(sorted(p for _, p in entries)[:5]))
    print("[publish] %s" % warn)
    if conn is not None:
        db_mod.log_health(conn, "publish", "warn", warn)


def git_publish(cfg, message: str, writes: Dict[str, str],
                push: bool = True,
                conn: Optional[sqlite3.Connection] = None) -> str:
    """Serialize, rebase, write, commit, push.

    writes: {repo-relative path: file content}. Files are written AFTER the
    rebase so a conflicted rebase never eats fresh exports. Never resolves
    conflicts, never force-pushes. Returns one of:
      'skipped-lock' | 'noop' | 'pushed' | 'committed-local' | 'push-failed'
    Raises PublishError on preflight or rebase-conflict failures.
    """
    import fcntl

    site = site_config(cfg)
    repo = site["repo_path"]
    branch = site["branch"]
    remote = site["remote"]

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_f = open(LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            msg = "publish lock held by another run — skipping"
            print("[publish] %s" % msg)
            if conn is not None:
                db_mod.log_health(conn, "publish", "info", msg)
            return "skipped-lock"

        # Preflight: a real git repo, on the configured branch.
        if _git(repo, "rev-parse", "--git-dir").returncode != 0:
            raise PublishError("%s is not a git repository" % repo)
        head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if head.stdout.strip() != branch:
            raise PublishError(
                "site repo is on %r, expected %r — refusing to publish"
                % (head.stdout.strip(), branch)
            )

        # Discard pipeline-owned dirt (crash between write and commit) so
        # the rebase below can't wedge; refuse on any other dirt.
        _clean_pipeline_dirt(repo, conn)

        # Rebase on the remote BEFORE writing files.
        fetched = _git(repo, "fetch", remote).returncode == 0
        if fetched:
            rb = _git(repo, "rebase", "%s/%s" % (remote, branch))
            if rb.returncode != 0:
                _git(repo, "rebase", "--abort")
                err = ("rebase onto %s/%s conflicted — aborted, manual "
                       "resolution required" % (remote, branch))
                if conn is not None:
                    db_mod.log_health(conn, "publish", "error", err)
                raise PublishError(err)
        else:
            warn = "git fetch %s failed (offline?) — publishing locally" % remote
            print("[publish] %s" % warn)
            if conn is not None:
                db_mod.log_health(conn, "publish", "warn", warn)

        # Write files, stage exactly them.
        for rel, content in writes.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        paths = sorted(writes.keys())
        add = _git(repo, "add", "--", *paths)
        if add.returncode != 0:
            raise PublishError("git add failed: %s" % add.stderr.strip())

        if _git(repo, "diff", "--cached", "--quiet").returncode == 0:
            return "noop"

        commit = _git(repo, "commit", "-m", message)
        if commit.returncode != 0:
            raise PublishError("git commit failed: %s" % commit.stderr.strip())

        if not push:
            return "committed-local"

        pushed = _git(repo, "push", remote, branch)
        if pushed.returncode != 0:
            # One pull --rebase retry (remote moved between fetch and push).
            pulled = _git(repo, "pull", "--rebase", remote, branch)
            if pulled.returncode == 0:
                pushed = _git(repo, "push", remote, branch)
            else:
                _git(repo, "rebase", "--abort")
        if pushed.returncode != 0:
            warn = ("git push failed (offline?) — local commit kept: %s"
                    % pushed.stderr.strip()[:300])
            print("[publish] %s" % warn)
            if conn is not None:
                db_mod.log_health(conn, "publish", "warn", warn)
            return "push-failed"
        return "pushed"
    finally:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
        except (IOError, OSError):
            pass
        lock_f.close()


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def publish_digest(cfg, conn: sqlite3.Connection, kind: str, period_key: str,
                   blurb: Optional[str] = None) -> int:
    """Write one digest's markdown to the site repo and push."""
    row = conn.execute(
        "SELECT * FROM digests WHERE kind=? AND period_key=?",
        (kind, period_key),
    ).fetchone()
    if row is None:
        print("no %s digest for %s" % (kind, period_key))
        return 1
    relpath, content = write_digest_md(cfg, row, blurb=blurb)
    push = bool(cfg.site.get("push", True))
    try:
        status = git_publish(
            cfg, "signal: %s digest %s" % (kind, period_key),
            {relpath: content}, push=push, conn=conn,
        )
    except PublishError as e:
        with db_mod.write_tx(conn):
            conn.execute(
                "UPDATE digests SET publish_error=? "
                "WHERE kind=? AND period_key=?",
                (str(e), kind, period_key),
            )
        print("[publish] %s" % e)
        return 1
    if status in ("pushed", "committed-local", "noop"):
        with db_mod.write_tx(conn):
            conn.execute(
                "UPDATE digests SET published_at=?, publish_error=NULL "
                "WHERE kind=? AND period_key=?",
                (_now_iso(), kind, period_key),
            )
        print("[publish] %s digest %s -> %s (%s)"
              % (kind, period_key, relpath, status))
        return 0
    if status == "push-failed":
        with db_mod.write_tx(conn):
            conn.execute(
                "UPDATE digests SET publish_error=? "
                "WHERE kind=? AND period_key=?",
                ("push failed; local commit kept", kind, period_key),
            )
        return 1
    # skipped-lock: record it so the row is visibly unpublished; the
    # periodic refresher re-exports any digest with published_at IS NULL.
    with db_mod.write_tx(conn):
        conn.execute(
            "UPDATE digests SET publish_error=? "
            "WHERE kind=? AND period_key=?",
            ("publish lock contention; retry pending", kind, period_key),
        )
    return 1


def _collect_writes(conn: sqlite3.Connection, cfg, what: str,
                    backfill_since: Optional[str] = None) -> Dict[str, str]:
    from . import kb

    writes: Dict[str, str] = {}
    if what in ("picks", "all", "refresh"):
        writes["src/data/picks.json"] = (
            json.dumps(export_picks(conn, cfg), indent=2, ensure_ascii=False)
            + "\n"
        )
    if what in ("spotlight", "all", "refresh"):
        writes["src/data/spotlight.json"] = (
            json.dumps(export_spotlight(conn, cfg), indent=2,
                       ensure_ascii=False)
            + "\n"
        )
    if what in ("stats", "all", "refresh"):
        writes["src/data/stats.json"] = (
            json.dumps(export_stats(conn, cfg), indent=2, ensure_ascii=False)
            + "\n"
        )
    if what in ("digests", "all"):
        for row in conn.execute(
            "SELECT * FROM digests ORDER BY generated_at"
        ).fetchall():
            rel, content = write_digest_md(cfg, row)
            writes[rel] = content
    if what in ("kb", "all"):
        rel, content = kb.readme()
        writes[rel] = content
        if backfill_since:
            writes.update(kb.backfill(conn, cfg, backfill_since))
        else:
            yesterday = datetime.date.today() - datetime.timedelta(days=1)
            rel, content = kb.daily_ledger(conn, cfg, yesterday)
            writes[rel] = content
    return writes


def run(cfg, what: str = "all", push: bool = True,
        backfill_since: Optional[str] = None) -> int:
    """CLI entry: python3 -m signalpipe publish [--what ...] [--no-push]."""
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        writes = _collect_writes(conn, cfg, what, backfill_since)
        if not writes:
            print("nothing to publish for --what %s" % what)
            return 0
        try:
            status = git_publish(
                cfg, "signal: publish %s" % what, writes,
                push=push and bool(cfg.site.get("push", True)), conn=conn,
            )
        except PublishError as e:
            db_mod.log_health(conn, "publish", "error", str(e))
            print("publish failed: %s" % e)
            return 1
        print("[publish] %s: %d file(s), %s" % (what, len(writes), status))
        if status in ("pushed", "committed-local") and what in ("digests", "all"):
            with db_mod.write_tx(conn):
                conn.execute(
                    "UPDATE digests SET published_at=?, publish_error=NULL "
                    "WHERE published_at IS NULL",
                    (_now_iso(),),
                )
        db_mod.log_health(
            conn, "publish", "info",
            "publish %s: %d file(s), %s" % (what, len(writes), status),
        )
        return 0 if status in ("pushed", "committed-local", "noop") else 1
    finally:
        conn.close()


def refresh(cfg) -> int:
    """Worker job: re-export picks + stats, commit only if changed. Also
    retries any digest still unpublished (published_at IS NULL — e.g. a
    digest publish that lost the flock race), so a missed digest reaches
    the site within one refresh interval instead of never."""
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        writes = _collect_writes(conn, cfg, "refresh")
        unpublished = conn.execute(
            "SELECT * FROM digests WHERE published_at IS NULL"
        ).fetchall()
        for row in unpublished:
            rel, content = write_digest_md(cfg, row)
            writes[rel] = content
        try:
            status = git_publish(
                cfg, "signal: refresh picks + stats", writes,
                push=bool(cfg.site.get("push", True)), conn=conn,
            )
        except PublishError as e:
            db_mod.log_health(conn, "publish", "error", str(e))
            print("publish refresh failed: %s" % e)
            return 1
        if unpublished and status in ("pushed", "committed-local", "noop"):
            with db_mod.write_tx(conn):
                conn.execute(
                    "UPDATE digests SET published_at=?, publish_error=NULL "
                    "WHERE published_at IS NULL",
                    (_now_iso(),),
                )
        print("[publish] refresh: %s (%d digest(s) retried)"
              % (status, len(unpublished)))
        return 0 if status in ("pushed", "committed-local", "noop") else 1
    finally:
        conn.close()


def publish_kb_daily(cfg, dates=None) -> int:
    """Worker job: commit deterministic kb ledger(s). `dates` is a single
    date or an iterable of dates (the Monday digest window is Fri+Sat+Sun,
    so the Monday run must publish THREE ledgers); default: yesterday."""
    from . import kb

    if dates is None:
        dates = [datetime.date.today() - datetime.timedelta(days=1)]
    elif isinstance(dates, (datetime.date, str)):
        dates = [dates]
    dates = [d if isinstance(d, datetime.date)
             else datetime.date.fromisoformat(str(d)) for d in dates]

    conn = db_mod.connect_rw(cfg.db_path)
    try:
        writes = dict([kb.readme()])
        for d in dates:
            rel, content = kb.daily_ledger(conn, cfg, d)
            writes[rel] = content
        label = ", ".join(d.isoformat() for d in dates)
        try:
            status = git_publish(
                cfg, "signal: kb ledger %s" % label, writes,
                push=bool(cfg.site.get("push", True)), conn=conn,
            )
        except PublishError as e:
            db_mod.log_health(conn, "publish", "error", str(e))
            print("kb publish failed: %s" % e)
            return 1
        print("[publish] kb %s: %s" % (label, status))
        return 0 if status in ("pushed", "committed-local", "noop") else 1
    finally:
        conn.close()


def publish_trends(cfg) -> int:
    """Worker job: Sonnet rolling update of kb/trends.md (spends)."""
    from . import kb

    conn = db_mod.connect_rw(cfg.db_path)
    try:
        try:
            result = kb.trends(conn, cfg)
        except Exception as e:  # noqa: BLE001 — LLM failures stay contained
            db_mod.log_health(conn, "publish", "error",
                              "kb trends failed: %s" % e)
            print("kb trends failed: %s" % e)
            return 1
        if result is None:
            return 1
        rel, content = result
        try:
            status = git_publish(
                cfg, "signal: kb trends update", {rel: content},
                push=bool(cfg.site.get("push", True)), conn=conn,
            )
        except PublishError as e:
            db_mod.log_health(conn, "publish", "error", str(e))
            print("kb trends publish failed: %s" % e)
            return 1
        print("[publish] trends: %s" % status)
        return 0 if status in ("pushed", "committed-local", "noop") else 1
    finally:
        conn.close()
