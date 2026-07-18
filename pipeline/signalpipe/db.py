"""SQLite store for Signal.

One writer (the worker) + read-only readers (the server). WAL mode,
synchronous=NORMAL, busy_timeout. The DB must live OUTSIDE iCloud Drive —
iCloud syncs -wal/-shm files independently and can corrupt the database.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
from typing import Optional, Tuple

# WAL-reset corruption bug (two+ connections checkpointing simultaneously)
# was fixed in 3.51.3 (2026-03-13). Signal has exactly one writer, so exposure
# is minimal — warn, don't fail, on older builds.
MIN_SQLITE = (3, 51, 3)

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key           TEXT PRIMARY KEY,
  value         TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  id            INTEGER PRIMARY KEY,
  slug          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  category      TEXT NOT NULL DEFAULT 'uncategorized',
  type          TEXT NOT NULL,                 -- rss|atom|json|api|scrape
  url           TEXT NOT NULL,
  homepage      TEXT,
  topics        TEXT,                          -- JSON array of channel tags
  reputation    REAL NOT NULL DEFAULT 1.0,     -- 0..2 weight
  tier          INTEGER NOT NULL DEFAULT 2,    -- 1 must-have .. 3 niche
  cadence_min   INTEGER NOT NULL DEFAULT 60,
  paywalled     INTEGER NOT NULL DEFAULT 0,
  enabled       INTEGER NOT NULL DEFAULT 1,
  mode          TEXT,                          -- per-source fetcher mode
  why           TEXT,
  api_notes     TEXT,
  added_at      TEXT,
  verified_at   TEXT,                          -- last successful probe/fetch
  last_fetch    TEXT,
  last_error    TEXT,
  error_count   INTEGER NOT NULL DEFAULT 0     -- consecutive failures
);

CREATE TABLE IF NOT EXISTS fetch_cache (
  url           TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  body_sha256   TEXT,
  fetched_at    TEXT NOT NULL,
  status        INTEGER
);

CREATE TABLE IF NOT EXISTS clusters (
  id            INTEGER PRIMARY KEY,
  canonical_url TEXT UNIQUE,
  title         TEXT NOT NULL,
  title_key     TEXT NOT NULL,                 -- normalized token-set key
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  surface_count INTEGER NOT NULL DEFAULT 0,
  score         REAL,
  score_at      TEXT,
  merge_reason  TEXT,
  story_id      TEXT                           -- stable content id (dedup ledger key)
);
CREATE INDEX IF NOT EXISTS idx_clusters_score    ON clusters(score DESC);
CREATE INDEX IF NOT EXISTS idx_clusters_seen     ON clusters(last_seen);
CREATE INDEX IF NOT EXISTS idx_clusters_titlekey ON clusters(title_key);

CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  cluster_id    INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
  source_id     INTEGER NOT NULL REFERENCES sources(id),
  guid          TEXT NOT NULL,
  raw_url       TEXT NOT NULL,
  canonical_url TEXT,
  title         TEXT NOT NULL,
  author        TEXT,
  published_at  TEXT,
  ingested_at   TEXT NOT NULL,
  points        INTEGER,
  comments      INTEGER,
  extra         TEXT,                          -- JSON per-source signals
  UNIQUE(source_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_items_cluster  ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_items_canon    ON items(canonical_url);
CREATE INDEX IF NOT EXISTS idx_items_ingested ON items(ingested_at);

CREATE TABLE IF NOT EXISTS surfaces (
  cluster_id    INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  source_id     INTEGER NOT NULL REFERENCES sources(id),
  url           TEXT NOT NULL,                 -- discussion/commentary link
  points        INTEGER,
  comments      INTEGER,
  seen_at       TEXT NOT NULL,
  PRIMARY KEY (cluster_id, source_id)
);

CREATE TABLE IF NOT EXISTS articles (
  cluster_id    INTEGER PRIMARY KEY REFERENCES clusters(id) ON DELETE CASCADE,
  source_url    TEXT NOT NULL,                 -- canonical, always
  read_url      TEXT NOT NULL,                 -- best free read
  read_kind     TEXT,                          -- primary|publication-free|freedium|canonical-fallback
  paywalled     INTEGER NOT NULL DEFAULT 0,
  archive_url   TEXT,                          -- INTERNAL ONLY: never in feed or any published output
  extracted_at  TEXT,
  word_count    INTEGER,
  text          TEXT,
  excerpt       TEXT,
  lang          TEXT,
  fetch_status  TEXT                           -- ok|paywalled|failed|skipped
);

CREATE TABLE IF NOT EXISTS curations (
  cluster_id      INTEGER PRIMARY KEY REFERENCES clusters(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending|triaged|done|skipped|failed
  tier_used       TEXT,
  backend_used    TEXT,
  model_used      TEXT,
  relevance_score INTEGER,
  why_it_matters  TEXT,
  notes           TEXT,                        -- JSON array of bullets
  summary         TEXT,
  channels        TEXT,                        -- JSON array (legacy beat tags)
  category        TEXT,                        -- v2 primary category
  subcategories   TEXT,                        -- v2 JSON array of subcategories
  novelty         TEXT,
  audience        TEXT,
  skip            INTEGER NOT NULL DEFAULT 0,
  skip_reason     TEXT,
  cost_usd        REAL,
  curated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_cur_status ON curations(status);
CREATE INDEX IF NOT EXISTS idx_cur_score  ON curations(relevance_score DESC);

CREATE TABLE IF NOT EXISTS digests (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL DEFAULT 'weekly', -- daily|weekly|monthly|quarterly|yearly
  period_key    TEXT NOT NULL,                 -- 2026-06-10 | 2026-W24 | 2026-06 | 2026-Q2 | 2026
  window_start  TEXT NOT NULL,
  window_end    TEXT NOT NULL,
  generated_at  TEXT NOT NULL,
  model_used    TEXT,
  title         TEXT,
  blurb         TEXT,                          -- one-sentence standfirst (v3)
  body_md       TEXT,
  body_html     TEXT,
  cluster_ids   TEXT,                          -- JSON array
  staged_path   TEXT,
  promoted      INTEGER NOT NULL DEFAULT 0,
  published_at  TEXT,
  publish_error TEXT,
  cost_usd      REAL,
  UNIQUE(kind, period_key)
);

CREATE TABLE IF NOT EXISTS published_ledger (
  story_id    TEXT NOT NULL,                 -- stable content id (clusters.story_id)
  surface     TEXT NOT NULL,                 -- picks|daily|weekly|monthly|quarterly|yearly
  edition_key TEXT NOT NULL DEFAULT '',      -- digest period_key; '' for the rolling picks feed
  cluster_id  INTEGER,                       -- cluster at publish time (audit only)
  first_at    TEXT NOT NULL,                 -- first time this story hit this surface
  PRIMARY KEY (story_id, surface, edition_key)
);
CREATE INDEX IF NOT EXISTS idx_ledger_story ON published_ledger(story_id);

CREATE TABLE IF NOT EXISTS spend (
  day           TEXT PRIMARY KEY,              -- "2026-06-09"
  cli_usd       REAL NOT NULL DEFAULT 0,
  api_usd       REAL NOT NULL DEFAULT 0,
  digest_usd    REAL NOT NULL DEFAULT 0,       -- digest-tier slice of the day
  calls         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS health (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,
  job           TEXT NOT NULL,                 -- ingest|score|fetch|curate|digest|server|sources
  level         TEXT NOT NULL,                 -- info|warn|error
  message       TEXT NOT NULL,
  stats         TEXT                           -- JSON
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON health(ts DESC);

-- Run attribution. Every completed job records its outcome stats tagged with a
-- fingerprint of the tunable config that produced them, so a knob change has a
-- real before/after instead of a guess. config_versions dedups the
-- fingerprint -> tunables snapshot; runs is the append-only outcome series.
CREATE TABLE IF NOT EXISTS config_versions (
  hash          TEXT PRIMARY KEY,             -- 12-char sha256 of the tunable knobs
  first_seen    TEXT NOT NULL,
  tunables      TEXT NOT NULL                 -- JSON snapshot of the knobs
);

CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,
  job           TEXT NOT NULL,                -- ingest|score|fetch|curate|digest
  config_hash   TEXT NOT NULL,                -- -> config_versions.hash
  stats         TEXT NOT NULL                 -- JSON outcome counts for this run
);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job, ts DESC);
"""


class DBError(Exception):
    pass


def sqlite_version() -> Tuple[int, ...]:
    return tuple(int(x) for x in sqlite3.sqlite_version.split("."))


def sqlite_version_warning() -> Optional[str]:
    if sqlite_version() < MIN_SQLITE:
        return (
            "SQLite %s < 3.51.3: WAL-reset bug (fixed 3.51.3) is unpatched. "
            "Signal runs a single writer so exposure is minimal, but avoid "
            "concurrent manual sqlite3 sessions against the live DB."
            % sqlite3.sqlite_version
        )
    return None


def assert_safe_path(db_path: pathlib.Path) -> None:
    if "Mobile Documents" in str(db_path):
        raise DBError(
            "refusing to open %s: SQLite WAL inside iCloud Drive risks "
            "corruption (iCloud syncs -wal/-shm independently)" % db_path
        )


def _apply_common_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")


def connect_rw(db_path: pathlib.Path) -> sqlite3.Connection:
    """Writer connection (worker / CLI jobs). Creates dir + schema on demand."""
    assert_safe_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None = true autocommit: no legacy implicit-BEGIN layer
    # to collide with write_tx()'s explicit BEGIN IMMEDIATE.
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _apply_common_pragmas(conn)
    init_schema(conn)
    return conn


def connect_ro(db_path: pathlib.Path) -> sqlite3.Connection:
    """Read-only connection (server). Never blocks or is blocked by the writer."""
    assert_safe_path(db_path)
    if not db_path.exists():
        raise DBError("database %s does not exist; run an ingest first" % db_path)
    uri = "file:%s?mode=ro" % db_path
    conn = sqlite3.connect(
        uri, uri=True, timeout=10, check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    _apply_common_pragmas(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    else:
        ver = int(row[0])
        if ver < 2:
            _migrate_digests_v2(conn)
        if ver < 3:
            _migrate_digests_v3(conn)
        if ver < 4:
            _migrate_spend_v4(conn)
        if ver < 5:
            _migrate_taxonomy_v5(conn)
        if ver < SCHEMA_VERSION:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
    conn.commit()


# The new-shape digests DDL, used both in SCHEMA (fresh DBs) and by the v1->v2
# table rebuild (CREATE TABLE here must NOT carry IF NOT EXISTS).
_DIGESTS_V2_DDL = """
CREATE TABLE digests (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL DEFAULT 'weekly',
  period_key    TEXT NOT NULL,
  window_start  TEXT NOT NULL,
  window_end    TEXT NOT NULL,
  generated_at  TEXT NOT NULL,
  model_used    TEXT,
  title         TEXT,
  body_md       TEXT,
  body_html     TEXT,
  cluster_ids   TEXT,
  staged_path   TEXT,
  promoted      INTEGER NOT NULL DEFAULT 0,
  published_at  TEXT,
  publish_error TEXT,
  cost_usd      REAL,
  UNIQUE(kind, period_key)
)
"""


def _iso_week_bounds(week: str) -> Tuple[str, str]:
    """v1 weekly semantics: ISO week 'YYYY-Www' -> Monday..next-Monday UTC."""
    import datetime

    year, wnum = week.split("-W")
    monday = datetime.date.fromisocalendar(int(year), int(wnum), 1)
    nxt = monday + datetime.timedelta(days=7)
    return (
        datetime.datetime(monday.year, monday.month, monday.day,
                          tzinfo=datetime.timezone.utc).isoformat(),
        datetime.datetime(nxt.year, nxt.month, nxt.day,
                          tzinfo=datetime.timezone.utc).isoformat(),
    )


def _migrate_digests_v3(conn: sqlite3.Connection) -> None:
    """v2 -> v3: persist the digest blurb (standfirst) so republishing keeps
    it instead of falling back to the title. Guarded ALTER — a fresh v3 DDL
    (or a v1->v2 rebuild that ran under v3 code) already has the column."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(digests)")}
    if "blurb" not in cols:
        conn.execute("ALTER TABLE digests ADD COLUMN blurb TEXT")


def _migrate_spend_v4(conn: sqlite3.Connection) -> None:
    """v3 -> v4: per-day digest-tier spend column, so digest_cap_usd gates
    digest spend against digest spend (not the whole day's total). Guarded
    ALTER — fresh v4 DDL already has the column."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(spend)")}
    if "digest_usd" not in cols:
        conn.execute(
            "ALTER TABLE spend ADD COLUMN digest_usd REAL NOT NULL DEFAULT 0")


def _migrate_taxonomy_v5(conn: sqlite3.Connection) -> None:
    """v4 -> v5: the public 2-level taxonomy + cross-edition de-dup ledger.

    Additive and idempotent: guarded ALTERs add clusters.story_id and
    curations.category/subcategories (a fresh v5 DDL already has them), then a
    one-time backfill stamps clusters.story_id from the existing
    canonical_url/title_key. The published_ledger table + its index are created
    by the SCHEMA script (a brand-new table, so no column-ordering hazard).
    Safe to re-run."""
    from .dedup import story_id  # local import avoids a module import cycle

    ccols = {r[1] for r in conn.execute("PRAGMA table_info(clusters)")}
    if "story_id" not in ccols:
        conn.execute("ALTER TABLE clusters ADD COLUMN story_id TEXT")
    curcols = {r[1] for r in conn.execute("PRAGMA table_info(curations)")}
    if "category" not in curcols:
        conn.execute("ALTER TABLE curations ADD COLUMN category TEXT")
    if "subcategories" not in curcols:
        conn.execute("ALTER TABLE curations ADD COLUMN subcategories TEXT")

    rows = conn.execute(
        "SELECT id, canonical_url, title_key FROM clusters WHERE story_id IS NULL"
    ).fetchall()
    updates = [(story_id(r["canonical_url"], r["title_key"]), r["id"]) for r in rows]
    if updates:
        conn.executemany("UPDATE clusters SET story_id=? WHERE id=?", updates)


def _migrate_digests_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2: table rebuild replacing iso_week UNIQUE with
    UNIQUE(kind, period_key). Old weekly rows carry over with kind='weekly',
    period_key=iso_week, and the Monday..Monday window the v1 digest covered.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(digests)").fetchall()]
    if "iso_week" not in cols:
        return  # already new shape (fresh table created by SCHEMA)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE digests RENAME TO digests_v1")
        conn.execute(_DIGESTS_V2_DDL)
        for row in conn.execute(
            "SELECT iso_week, generated_at, model_used, title, body_md, "
            "body_html, cluster_ids, staged_path, promoted, cost_usd "
            "FROM digests_v1 ORDER BY id"
        ).fetchall():
            since, until = _iso_week_bounds(row["iso_week"])
            conn.execute(
                "INSERT INTO digests(kind, period_key, window_start, "
                "window_end, generated_at, model_used, title, body_md, "
                "body_html, cluster_ids, staged_path, promoted, cost_usd) "
                "VALUES('weekly',?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["iso_week"], since, until, row["generated_at"],
                    row["model_used"], row["title"], row["body_md"],
                    row["body_html"], row["cluster_ids"], row["staged_path"],
                    row["promoted"], row["cost_usd"],
                ),
            )
        conn.execute("DROP TABLE digests_v1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def write_tx(conn: sqlite3.Connection):
    """Context manager for a short IMMEDIATE write transaction."""

    class _Tx:
        def __enter__(self):
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False

    return _Tx()


def checkpoint(conn: sqlite3.Connection) -> None:
    """Periodic WAL truncation from the writer."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


BACKUP_DIR = pathlib.Path(
    os.path.expanduser("~/Documents/backup/signal"))
BACKUP_KEEP = 8


def backup(db_path: pathlib.Path,
           backup_dir: Optional[pathlib.Path] = None,
           keep: int = BACKUP_KEEP) -> pathlib.Path:
    """Snapshot the DB via VACUUM INTO (a consistent, compacted copy that is
    safe against the live WAL writer) and prune to the newest `keep` files.
    The DB is the sole store of curations/digests/spend/registry runtime
    state — without this there is no recovery from disk loss or corruption.
    Returns the snapshot path."""
    import datetime

    backup_dir = backup_dir or BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    dest = backup_dir / ("signal_%s.db" % stamp)
    if dest.exists():  # same-minute re-run: replace
        dest.unlink()

    conn = connect_ro(db_path)  # read-only: never blocks the writer
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()

    snapshots = sorted(backup_dir.glob("signal_*.db"), reverse=True)
    for old in snapshots[max(1, int(keep)):]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def log_health(
    conn: sqlite3.Connection,
    job: str,
    level: str,
    message: str,
    stats: Optional[str] = None,
    ts: Optional[str] = None,
) -> None:
    import datetime

    # autocommit connection (isolation_level=None): no commit() — it would
    # prematurely end a caller's open write_tx.
    conn.execute(
        "INSERT INTO health(ts, job, level, message, stats) VALUES(?,?,?,?,?)",
        (
            ts or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            job,
            level,
            message,
            stats,
        ),
    )


def record_run(
    conn: sqlite3.Connection,
    job: str,
    config_hash: str,
    stats: str,
    tunables: Optional[str] = None,
    ts: Optional[str] = None,
) -> None:
    """Append an attributable run record: this run's outcome `stats` (JSON) tagged
    with the `config_hash` that produced them. When `tunables` (JSON) is given,
    upsert the config_versions row so the hash always resolves to its knobs.

    Autocommit connection (isolation_level=None): no commit() here — it would
    prematurely end a caller's open write_tx (mirrors log_health)."""
    import datetime

    now = ts or datetime.datetime.now(datetime.timezone.utc).isoformat()
    if tunables is not None:
        conn.execute(
            "INSERT OR IGNORE INTO config_versions(hash, first_seen, tunables) "
            "VALUES(?,?,?)",
            (config_hash, now, tunables),
        )
    conn.execute(
        "INSERT INTO runs(ts, job, config_hash, stats) VALUES(?,?,?,?)",
        (now, job, config_hash, stats),
    )


def recent_runs(
    conn: sqlite3.Connection,
    job: Optional[str] = None,
    limit: int = 40,
) -> list:
    """Most-recent run records (newest first): ts, job, config_hash, stats JSON."""
    if job:
        cur = conn.execute(
            "SELECT ts, job, config_hash, stats FROM runs WHERE job=? "
            "ORDER BY id DESC LIMIT ?",
            (job, int(limit)),
        )
    else:
        cur = conn.execute(
            "SELECT ts, job, config_hash, stats FROM runs "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
    return [dict(r) for r in cur.fetchall()]
