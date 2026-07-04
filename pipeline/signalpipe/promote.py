"""Promote a staged digest to Ghost — explicit, review-first, never scheduled.

Separation guarantees (hard requirements):
  - tag is ALWAYS "Signal" -> slug `signal` -> the /signal/ routes.yaml
    collection. Digests never join /blog/ or /blog/rss.
  - --no-feature ALWAYS: the homepage Writing section only shows
    featured:true posts, so digests never appear there.
  - The existing publishers are shelled, never reimplemented; they never
    pass a `newsletter` param, so Ghost emails nothing to members.
  - archive.* links refuse to publish.

Flow: promote --target local --apply        -> published on localhost:2368
      promote --target prod  --apply        -> DRAFT on starikov.co
      promote --target prod  --apply --publish-now -> published on prod
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Optional

from . import db as db_mod
from .publish import _ARCHIVE_RE

TAG = "Signal"


def _latest_digest(conn, week: Optional[str]):
    """Weekly-only Ghost semantics: promote never touches the other kinds."""
    if week:
        return conn.execute(
            "SELECT * FROM digests WHERE kind='weekly' AND period_key=?",
            (week,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM digests WHERE kind='weekly' "
        "ORDER BY period_key DESC LIMIT 1"
    ).fetchone()


def run(cfg, week: Optional[str], target: str, apply: bool,
        publish_now: bool = False) -> int:
    conn = db_mod.connect_rw(cfg.db_path)
    try:
        d = _latest_digest(conn, week)
        if not d:
            print("no staged digest found — run `python3 -m signalpipe digest`",
                  file=sys.stderr)
            return 1
        staged = pathlib.Path(d["staged_path"] or "")
        if not staged.exists():
            print("staged file missing: %s" % staged, file=sys.stderr)
            return 1
        body = staged.read_text()
        if _ARCHIVE_RE.search(body):  # same breadth as the publish scrub
            print("REFUSED: staged digest cites archive.* links", file=sys.stderr)
            return 1

        # Copy the staged artifact into the repo (source of truth) — promote
        # always runs interactively, so writing into iCloud is safe here.
        repo_md = cfg.blog_repo / "markdown" / "draft" / staged.name
        repo_md.parent.mkdir(parents=True, exist_ok=True)
        if staged.resolve() != repo_md.resolve():
            repo_md.write_text(body)
            print("copied staged digest -> %s" % repo_md)

        slug = staged.stem.replace("_", "-")
        title = d["title"] or "Signal Digest %s" % d["period_key"]

        if target == "local":
            cmd = [
                "python3", str(cfg.blog_repo / "scripts" / "_publish_to_local.py"),
                str(repo_md), "--tag", TAG, "--no-feature",
                "--title", title, "--slug", slug,
            ]
        else:
            cmd = [
                "python3", str(cfg.blog_repo / "scripts" / "_publish_to_prod.py"),
                str(repo_md), "--tag", TAG, "--no-feature",
                "--title", title, "--slug", slug, "--replace",
            ]
            if publish_now:
                cmd.append("--publish")

        if not apply:
            print("DRY RUN — would execute:")
            print("  " + " ".join("'%s'" % c if " " in c else c for c in cmd))
            if target == "prod":
                print("(prod default is a DRAFT; add --publish-now to go live)")
            return 0

        print("running: %s" % " ".join(cmd[-6:]))
        proc = subprocess.run(cmd, cwd=str(cfg.blog_repo / "scripts"))
        if proc.returncode != 0:
            print("publisher failed (%d)" % proc.returncode, file=sys.stderr)
            return proc.returncode

        if target == "prod" and publish_now:
            with db_mod.write_tx(conn):
                conn.execute(
                    "UPDATE digests SET promoted=1 WHERE id=?", (d["id"],)
                )
            db_mod.log_health(
                conn, "digest", "info",
                "digest %s PUBLISHED to prod (/signal/%s/)"
                % (d["period_key"], slug),
            )
        print()
        print("separation check: confirm the post is ABSENT from /blog/rss "
              "and present under /signal/ —")
        base = "http://localhost:2368" if target == "local" else "https://starikov.co"
        print("  curl -s %s/blog/rss/ | grep -c %s   # expect 0" % (base, slug))
        print("  curl -s %s/signal/rss/ | grep -c %s # expect >=1" % (base, slug))
        return 0
    finally:
        conn.close()
