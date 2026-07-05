"""Tests for :mod:`signalpipe.db` — schema, connection factories, versioned
migrations (v1->v5), write-tx, WAL checkpoint, VACUUM-INTO backup, health/run logging.

Hermetic: no network. sqlite runs either on an in-memory conn (migrations / write-tx /
logging) or on a pytest ``tmp_path`` file that is guaranteed OUTSIDE iCloud, so the
``assert_safe_path`` guard passes. The autouse ``redirect_state_dirs`` conftest fixture
already repoints ``db.BACKUP_DIR`` at tmp, but backup tests still pass ``backup_dir=``
explicitly per the suite's hard rules.
"""

from __future__ import annotations

import datetime
import pathlib
import sqlite3

import pytest

import signalpipe.db as db
from signalpipe.dedup import story_id as dedup_story_id


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
def _cols(conn: sqlite3.Connection, table: str):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _schema_version(conn: sqlite3.Connection) -> str:
    return conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]


def _mem_v5() -> sqlite3.Connection:
    """A fresh in-memory v5 DB: autocommit + Row factory, mirrors connect_rw."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    return conn


# Old-shape (v1) tables. Columns cover everything the current SCHEMA's CREATE
# INDEX statements reference (score/last_seen/title_key on clusters,
# status/relevance_score on curations) so ``executescript(SCHEMA)`` — which runs
# IF-NOT-EXISTS DDL + unconditional CREATE INDEX during init_schema — succeeds
# against the legacy tables. story_id / category / subcategories / digest_usd /
# blurb and the (kind, period_key) digests shape are all deliberately absent so
# the v2..v5 migrators actually fire.
_LEGACY_V1_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO meta(key, value) VALUES ('schema_version', '1');

CREATE TABLE digests (
  id            INTEGER PRIMARY KEY,
  iso_week      TEXT UNIQUE NOT NULL,
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
  cost_usd      REAL
);

CREATE TABLE clusters (
  id            INTEGER PRIMARY KEY,
  canonical_url TEXT UNIQUE,
  title         TEXT NOT NULL,
  title_key     TEXT NOT NULL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  surface_count INTEGER NOT NULL DEFAULT 0,
  score         REAL,
  score_at      TEXT,
  merge_reason  TEXT
);

CREATE TABLE curations (
  cluster_id      INTEGER PRIMARY KEY,
  status          TEXT NOT NULL DEFAULT 'pending',
  tier_used       TEXT,
  backend_used    TEXT,
  model_used      TEXT,
  relevance_score INTEGER,
  why_it_matters  TEXT,
  notes           TEXT,
  summary         TEXT,
  channels        TEXT,
  novelty         TEXT,
  audience        TEXT,
  skip            INTEGER NOT NULL DEFAULT 0,
  skip_reason     TEXT,
  cost_usd        REAL,
  curated_at      TEXT
);

CREATE TABLE spend (
  day     TEXT PRIMARY KEY,
  cli_usd REAL NOT NULL DEFAULT 0,
  api_usd REAL NOT NULL DEFAULT 0,
  calls   INTEGER NOT NULL DEFAULT 0
);
"""


def _legacy_v1() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(_LEGACY_V1_DDL)
    return conn


def _make_db_file(path: pathlib.Path) -> None:
    """Create a real writer DB file (WAL, checkpointed, closed) with one source row."""
    w = db.connect_rw(path)
    try:
        w.execute(
            "INSERT INTO sources(slug, name, type, url) VALUES('s', 'S', 'rss', 'http://x')"
        )
        db.checkpoint(w)
    finally:
        w.close()


def _open_count_sources(path: pathlib.Path) -> int:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# sqlite_version / sqlite_version_warning  (pure, env-global)
# --------------------------------------------------------------------------- #
class TestSqliteVersion:
    def test_returns_int_tuple_matching_runtime(self):
        result = db.sqlite_version()
        assert isinstance(result, tuple)
        assert all(isinstance(x, int) for x in result)
        assert result == tuple(int(x) for x in sqlite3.sqlite_version.split("."))

    def test_parses_monkeypatched_version_string(self, monkeypatch):
        monkeypatch.setattr(sqlite3, "sqlite_version", "3.45.1")
        assert db.sqlite_version() == (3, 45, 1)

    @pytest.mark.parametrize(
        "version, warns",
        [
            ((3, 51, 3), False),  # exactly the floor -> not below -> no warning
            ((3, 51, 4), False),
            ((3, 52, 0), False),
            ((4, 0, 0), False),
            ((3, 51, 2), True),
            ((3, 40, 0), True),
            ((3, 0, 0), True),
        ],
    )
    def test_warning_branches(self, monkeypatch, version, warns):
        monkeypatch.setattr(db, "sqlite_version", lambda: version)
        out = db.sqlite_version_warning()
        if warns:
            assert isinstance(out, str)
            assert "3.51.3" in out
            assert "WAL-reset" in out
        else:
            assert out is None

    def test_min_sqlite_constant(self):
        assert db.MIN_SQLITE == (3, 51, 3)


# --------------------------------------------------------------------------- #
# assert_safe_path  (pure string guard)
# --------------------------------------------------------------------------- #
class TestAssertSafePath:
    @pytest.mark.parametrize(
        "bad",
        [
            "/Users/me/Library/Mobile Documents/com~apple~CloudDocs/signal.db",
            "/a/Mobile Documents/b/signal.db",
            "/x/Mobile Documents/signal.db",
        ],
    )
    def test_rejects_icloud_paths(self, bad):
        with pytest.raises(db.DBError):
            db.assert_safe_path(pathlib.Path(bad))

    @pytest.mark.parametrize(
        "good",
        [
            "/tmp/signal.db",
            "/private/var/folders/xy/zzz/signal.db",
            "/Users/me/Documents/backup/signal.db",  # 'Documents' alone is fine
        ],
    )
    def test_allows_non_icloud_paths(self, good):
        assert db.assert_safe_path(pathlib.Path(good)) is None


# --------------------------------------------------------------------------- #
# connect_rw / connect_ro  (integration: real sqlite on tmp file)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestConnectRw:
    def test_creates_parent_dir_schema_and_pragmas(self, tmp_path):
        target = tmp_path / "nested" / "deep" / "signal.db"
        assert not target.parent.exists()
        conn = db.connect_rw(target)
        try:
            assert target.parent.exists()
            assert target.exists()
            assert _schema_version(conn) == "5"
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            # every core table exists
            for t in ("meta", "sources", "clusters", "curations", "digests", "health"):
                assert _table_exists(conn, t)
        finally:
            conn.close()

    def test_row_factory_is_row(self, tmp_path):
        conn = db.connect_rw(tmp_path / "signal.db")
        try:
            conn.execute(
                "INSERT INTO sources(slug, name, type, url) "
                "VALUES('s', 'S', 'rss', 'http://x')"
            )
            row = conn.execute("SELECT slug, name FROM sources").fetchone()
            assert row["slug"] == "s"
            assert row["name"] == "S"
        finally:
            conn.close()

    def test_rejects_icloud_path_before_any_io(self):
        with pytest.raises(db.DBError):
            db.connect_rw(pathlib.Path("/x/Mobile Documents/y/signal.db"))


@pytest.mark.integration
class TestConnectRo:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(db.DBError):
            db.connect_ro(tmp_path / "nope.db")

    def test_rejects_icloud_path(self):
        with pytest.raises(db.DBError):
            db.connect_ro(pathlib.Path("/x/Mobile Documents/y/signal.db"))

    def test_reads_but_rejects_writes(self, tmp_path):
        db_path = tmp_path / "signal.db"
        _make_db_file(db_path)
        ro = db.connect_ro(db_path)
        try:
            assert ro.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                ro.execute(
                    "INSERT INTO sources(slug, name, type, url) "
                    "VALUES('t', 'T', 'rss', 'http://y')"
                )
        finally:
            ro.close()


# --------------------------------------------------------------------------- #
# init_schema on a fresh DB  (integration)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestInitSchemaFresh:
    def test_fresh_db_has_all_v5_columns(self):
        conn = _mem_v5()
        try:
            assert "story_id" in _cols(conn, "clusters")
            assert {"category", "subcategories"} <= _cols(conn, "curations")
            assert "digest_usd" in _cols(conn, "spend")
            dcols = _cols(conn, "digests")
            assert {"blurb", "kind", "period_key", "window_start", "window_end"} <= dcols
            assert _schema_version(conn) == "5"
            # published_ledger (created by SCHEMA, not a migrator) is present
            assert _table_exists(conn, "published_ledger")
        finally:
            conn.close()

    def test_idempotent_no_migration_on_fresh_db(self):
        conn = _mem_v5()
        try:
            db.init_schema(conn)  # second run
            db.init_schema(conn)  # third run
            assert _schema_version(conn) == "5"
            # a fresh DB never takes the v1->v2 rebuild path
            assert not _table_exists(conn, "digests_v1")
            # exactly one schema_version row
            n = conn.execute(
                "SELECT COUNT(*) FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            assert n == 1
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# Migrations  (integration, in-memory old-shape DBs)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestMigrateDigestsV2:
    def _old_digests_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE digests (
              id INTEGER PRIMARY KEY, iso_week TEXT UNIQUE NOT NULL,
              generated_at TEXT NOT NULL, model_used TEXT, title TEXT,
              body_md TEXT, body_html TEXT, cluster_ids TEXT, staged_path TEXT,
              promoted INTEGER NOT NULL DEFAULT 0, published_at TEXT,
              publish_error TEXT, cost_usd REAL
            );
            """
        )
        return conn

    def test_rebuild_carries_rows_with_window(self):
        conn = self._old_digests_conn()
        try:
            conn.execute(
                "INSERT INTO digests(iso_week, generated_at, model_used, title, "
                "body_md, cluster_ids, staged_path, promoted, cost_usd) "
                "VALUES('2026-W24', 'g1', 'm', 'Wk24', 'md', '[1,2]', 'p', 1, 0.5)"
            )
            conn.execute(
                "INSERT INTO digests(iso_week, generated_at, title, promoted) "
                "VALUES('2026-W01', 'g2', 'Wk01', 0)"
            )
            db._migrate_digests_v2(conn)

            assert not _table_exists(conn, "digests_v1")
            new_cols = _cols(conn, "digests")
            assert "iso_week" not in new_cols
            assert {"kind", "period_key", "window_start", "window_end"} <= new_cols

            r24 = conn.execute(
                "SELECT * FROM digests WHERE period_key='2026-W24'"
            ).fetchone()
            assert r24["kind"] == "weekly"
            assert r24["window_start"] == "2026-06-08T00:00:00+00:00"
            assert r24["window_end"] == "2026-06-15T00:00:00+00:00"
            assert r24["title"] == "Wk24"
            assert r24["cluster_ids"] == "[1,2]"
            assert r24["promoted"] == 1
            assert r24["cost_usd"] == 0.5

            r01 = conn.execute(
                "SELECT window_start, window_end FROM digests WHERE period_key='2026-W01'"
            ).fetchone()
            assert r01["window_start"] == "2025-12-29T00:00:00+00:00"
            assert r01["window_end"] == "2026-01-05T00:00:00+00:00"
        finally:
            conn.close()

    def test_rolls_back_on_bad_iso_week(self):
        # A row whose iso_week can't be parsed makes _iso_week_bounds raise mid
        # rebuild; the except-clause rolls the transaction back (DDL included) and
        # re-raises, leaving the original iso_week table intact and no digests_v1.
        conn = self._old_digests_conn()
        try:
            conn.execute(
                "INSERT INTO digests(iso_week, generated_at, title) "
                "VALUES('not-a-week', 'g', 'T')"
            )
            with pytest.raises(ValueError):
                db._migrate_digests_v2(conn)
            assert "iso_week" in _cols(conn, "digests")  # rolled back to old shape
            assert not _table_exists(conn, "digests_v1")
            assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
        finally:
            conn.close()

    def test_guard_noop_on_new_shape(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(db._DIGESTS_V2_DDL)  # already new shape, no iso_week
            conn.execute(
                "INSERT INTO digests(kind, period_key, window_start, window_end, "
                "generated_at) VALUES('weekly', '2026-W24', 'a', 'b', 'g')"
            )
            db._migrate_digests_v2(conn)  # must early-return
            assert not _table_exists(conn, "digests_v1")
            assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
        finally:
            conn.close()

    def test_via_init_schema_upgrades_version(self):
        conn = self._old_digests_conn()
        try:
            # give it a meta row at v1 so init_schema runs the migrators
            conn.executescript(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                "INSERT INTO meta VALUES ('schema_version', '1');"
            )
            conn.execute(
                "INSERT INTO digests(iso_week, generated_at, title) "
                "VALUES('2026-W24', 'g', 'T')"
            )
            db.init_schema(conn)
            assert _schema_version(conn) == "5"
            row = conn.execute(
                "SELECT kind, period_key, blurb FROM digests"
            ).fetchone()
            assert row["kind"] == "weekly"
            assert row["period_key"] == "2026-W24"
            assert "blurb" in _cols(conn, "digests")  # v3 added it
        finally:
            conn.close()


@pytest.mark.integration
class TestMigrateDigestsV3:
    def test_adds_blurb_and_is_idempotent(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            conn.executescript(db._DIGESTS_V2_DDL)  # v2 shape: no blurb
            assert "blurb" not in _cols(conn, "digests")
            db._migrate_digests_v3(conn)
            assert "blurb" in _cols(conn, "digests")
            # second run is a no-op (guarded ALTER)
            db._migrate_digests_v3(conn)
            blurb_cols = [c for c in _cols(conn, "digests") if c == "blurb"]
            assert len(blurb_cols) == 1
        finally:
            conn.close()

    def test_noop_when_column_present(self):
        conn = _mem_v5()  # SCHEMA digests already carries blurb
        try:
            db._migrate_digests_v3(conn)  # no error, no duplicate
            assert "blurb" in _cols(conn, "digests")
        finally:
            conn.close()


@pytest.mark.integration
class TestMigrateSpendV4:
    def test_adds_digest_usd_backfills_zero_and_idempotent(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            conn.executescript(
                "CREATE TABLE spend (day TEXT PRIMARY KEY, cli_usd REAL DEFAULT 0, "
                "api_usd REAL DEFAULT 0, calls INTEGER DEFAULT 0);"
            )
            conn.execute("INSERT INTO spend(day, cli_usd) VALUES('2026-07-04', 1.25)")
            assert "digest_usd" not in _cols(conn, "spend")

            db._migrate_spend_v4(conn)
            assert "digest_usd" in _cols(conn, "spend")
            # existing row backfilled with the NOT NULL DEFAULT 0
            assert (
                conn.execute("SELECT digest_usd FROM spend WHERE day='2026-07-04'")
                .fetchone()[0]
                == 0
            )
            db._migrate_spend_v4(conn)  # idempotent
            assert len([c for c in _cols(conn, "spend") if c == "digest_usd"]) == 1
        finally:
            conn.close()

    def test_noop_when_column_present(self):
        conn = _mem_v5()  # SCHEMA spend already carries digest_usd
        try:
            db._migrate_spend_v4(conn)
            assert "digest_usd" in _cols(conn, "spend")
        finally:
            conn.close()


@pytest.mark.integration
class TestMigrateTaxonomyV5:
    def _old_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE clusters (
              id INTEGER PRIMARY KEY, canonical_url TEXT UNIQUE, title TEXT NOT NULL,
              title_key TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
              surface_count INTEGER NOT NULL DEFAULT 0, score REAL, score_at TEXT,
              merge_reason TEXT
            );
            CREATE TABLE curations (
              cluster_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
              relevance_score INTEGER
            );
            """
        )
        return conn

    def test_adds_columns_and_backfills_story_id(self):
        conn = self._old_conn()
        try:
            conn.execute(
                "INSERT INTO clusters(canonical_url, title, title_key, first_seen, "
                "last_seen) VALUES('https://x.com/a', 'T', 'key words', 't', 't')"
            )
            # discussion-only cluster: no canonical_url -> title_key fallback path
            conn.execute(
                "INSERT INTO clusters(canonical_url, title, title_key, first_seen, "
                "last_seen) VALUES(NULL, 'D', 'alpha beta', 't', 't')"
            )
            db._migrate_taxonomy_v5(conn)

            assert "story_id" in _cols(conn, "clusters")
            assert {"category", "subcategories"} <= _cols(conn, "curations")

            got_url = conn.execute(
                "SELECT story_id FROM clusters WHERE title='T'"
            ).fetchone()[0]
            got_null = conn.execute(
                "SELECT story_id FROM clusters WHERE title='D'"
            ).fetchone()[0]
            # delegates to dedup.story_id with (canonical_url, title_key)...
            assert got_url == dedup_story_id("https://x.com/a", "key words")
            assert got_null == dedup_story_id(None, "alpha beta")
            # ...and lands on the stable "never churn" golden ids: a canonical
            # URL keys on domain|path, a NULL url falls back to titlekey|<key>.
            assert got_url == "s_14d5993491e70837"
            assert got_null == "s_f265af883ab905bf"
        finally:
            conn.close()

    def test_idempotent_does_not_churn_existing_story_id(self):
        conn = self._old_conn()
        try:
            conn.execute(
                "INSERT INTO clusters(canonical_url, title, title_key, first_seen, "
                "last_seen) VALUES('https://x.com/a', 'T', 'key words', 't', 't')"
            )
            db._migrate_taxonomy_v5(conn)
            first = conn.execute("SELECT story_id FROM clusters").fetchone()[0]
            db._migrate_taxonomy_v5(conn)  # second run: no NULLs left, no error
            second = conn.execute("SELECT story_id FROM clusters").fetchone()[0]
            assert first == second == "s_14d5993491e70837"
        finally:
            conn.close()


@pytest.mark.integration
class TestFullMigrationChain:
    def test_v1_to_v5_applies_every_migration_once(self):
        conn = _legacy_v1()
        try:
            conn.execute(
                "INSERT INTO digests(iso_week, generated_at, title, promoted) "
                "VALUES('2026-W24', 'g', 'Wk24', 1)"
            )
            conn.execute(
                "INSERT INTO clusters(canonical_url, title, title_key, first_seen, "
                "last_seen) VALUES('https://x.com/a', 'T', 'key words', 't', 't')"
            )
            conn.execute("INSERT INTO curations(cluster_id, status) VALUES(1, 'done')")
            conn.execute("INSERT INTO spend(day, cli_usd) VALUES('2026-07-04', 2.0)")

            db.init_schema(conn)

            assert _schema_version(conn) == "5"
            # v2: digests rebuilt
            assert not _table_exists(conn, "digests_v1")
            drow = conn.execute(
                "SELECT kind, period_key, window_start, window_end FROM digests"
            ).fetchone()
            assert drow["kind"] == "weekly"
            assert drow["period_key"] == "2026-W24"
            assert drow["window_start"] == "2026-06-08T00:00:00+00:00"
            # v3: blurb
            assert "blurb" in _cols(conn, "digests")
            # v4: digest_usd
            assert "digest_usd" in _cols(conn, "spend")
            # v5: taxonomy columns + backfill
            assert {"category", "subcategories"} <= _cols(conn, "curations")
            sid = conn.execute("SELECT story_id FROM clusters").fetchone()[0]
            assert sid == dedup_story_id("https://x.com/a", "key words")
            assert sid == "s_14d5993491e70837"  # golden stable id
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# write_tx  (integration)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestWriteTx:
    def test_commits_on_clean_exit(self):
        conn = _mem_v5()
        try:
            with db.write_tx(conn) as c:
                assert c is conn
                c.execute("INSERT INTO meta(key, value) VALUES('k', 'v')")
            got = conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()
            assert got is not None and got[0] == "v"
        finally:
            conn.close()

    def test_rolls_back_on_exception(self):
        conn = _mem_v5()
        try:
            with pytest.raises(ValueError):
                with db.write_tx(conn):
                    conn.execute("INSERT INTO meta(key, value) VALUES('k', 'v')")
                    raise ValueError("boom")
            got = conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()
            assert got is None
        finally:
            conn.close()

    def test_exit_does_not_suppress_exception(self):
        # __exit__ returns False -> the exception propagates.
        conn = _mem_v5()
        try:
            with pytest.raises(KeyError):
                with db.write_tx(conn):
                    raise KeyError("x")
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# checkpoint  (integration)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestCheckpoint:
    def test_truncates_wal_and_keeps_data(self, tmp_path):
        db_path = tmp_path / "signal.db"
        conn = db.connect_rw(db_path)
        try:
            conn.execute(
                "INSERT INTO sources(slug, name, type, url) "
                "VALUES('s', 'S', 'rss', 'http://x')"
            )
            # The committed write lives in the -wal sidecar until checkpointed.
            wal = pathlib.Path(str(db_path) + "-wal")
            assert wal.exists() and wal.stat().st_size > 0
            # wal_checkpoint(TRUNCATE) returns nothing and shrinks the WAL to 0
            # bytes on success; a no-op checkpoint would leave the sidecar full.
            assert db.checkpoint(conn) is None
            assert wal.stat().st_size == 0
            assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# backup  (integration)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestBackup:
    def test_writes_valid_snapshot(self, tmp_path):
        db_path = tmp_path / "signal.db"
        _make_db_file(db_path)
        backup_dir = tmp_path / "bk"
        dest = db.backup(db_path, backup_dir=backup_dir, keep=8)
        assert dest.exists()
        assert dest.parent == backup_dir
        assert dest.name.startswith("signal_") and dest.name.endswith(".db")
        # a real, openable DB carrying the source row
        assert _open_count_sources(dest) == 1

    def test_prunes_to_newest_keep(self, tmp_path):
        db_path = tmp_path / "signal.db"
        _make_db_file(db_path)
        backup_dir = tmp_path / "bk"
        backup_dir.mkdir()
        for stamp in ("20200101_0000", "20200102_0000", "20200103_0000"):
            (backup_dir / ("signal_%s.db" % stamp)).write_bytes(b"old")

        dest = db.backup(db_path, backup_dir=backup_dir, keep=2)

        remaining = {p.name for p in backup_dir.glob("signal_*.db")}
        assert len(remaining) == 2
        assert dest.name in remaining  # newest (current wall clock) kept
        assert "signal_20200103_0000.db" in remaining  # newest legacy kept
        assert "signal_20200102_0000.db" not in remaining
        assert "signal_20200101_0000.db" not in remaining

    def test_keep_zero_clamps_to_one(self, tmp_path):
        db_path = tmp_path / "signal.db"
        _make_db_file(db_path)
        backup_dir = tmp_path / "bk"
        backup_dir.mkdir()
        for stamp in ("20200101_0000", "20200102_0000"):
            (backup_dir / ("signal_%s.db" % stamp)).write_bytes(b"old")

        dest = db.backup(db_path, backup_dir=backup_dir, keep=0)

        remaining = {p.name for p in backup_dir.glob("signal_*.db")}
        assert remaining == {dest.name}  # max(1, 0) -> only the newest survives

    def test_same_minute_rerun_replaces(self, tmp_path):
        # Pre-seed a garbage file at the exact minute-stamped path backup will
        # compute, forcing the ``if dest.exists(): dest.unlink()`` branch. The
        # returned snapshot must be a real DB, proving the stale file was replaced.
        db_path = tmp_path / "signal.db"
        _make_db_file(db_path)
        backup_dir = tmp_path / "bk"
        backup_dir.mkdir()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        stale = backup_dir / ("signal_%s.db" % stamp)
        stale.write_bytes(b"not a database")

        dest = db.backup(db_path, backup_dir=backup_dir, keep=8)
        assert dest.exists()
        assert _open_count_sources(dest) == 1

    def test_prune_swallows_unlink_oserror(self, tmp_path, monkeypatch):
        # If a stale snapshot can't be removed (permissions/racing sweep), the
        # pruning loop swallows the OSError and still returns the fresh snapshot.
        db_path = tmp_path / "signal.db"
        _make_db_file(db_path)
        backup_dir = tmp_path / "bk"
        backup_dir.mkdir()
        for stamp in ("20200101_0000", "20200102_0000"):
            (backup_dir / ("signal_%s.db" % stamp)).write_bytes(b"old")

        def _boom(self, *a, **k):
            raise OSError("cannot unlink")

        monkeypatch.setattr(pathlib.Path, "unlink", _boom)
        dest = db.backup(db_path, backup_dir=backup_dir, keep=1)  # no exception

        assert dest.exists()
        # unlink was blocked, so the stale files are still present
        assert (backup_dir / "signal_20200101_0000.db").exists()

    def test_default_backup_dir_is_redirected_to_tmp(self, tmp_path):
        # conftest's autouse redirect points db.BACKUP_DIR at tmp; a backup()
        # call without backup_dir must land there, never the real home dir.
        db_path = tmp_path / "signal.db"
        _make_db_file(db_path)
        dest = db.backup(db_path, keep=8)
        assert db.BACKUP_DIR in dest.parents
        assert "Documents/backup/signal" not in str(dest)


# --------------------------------------------------------------------------- #
# log_health  (integration)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestLogHealth:
    def test_inserts_row_with_injected_ts(self):
        conn = _mem_v5()
        try:
            db.log_health(
                conn, "ingest", "info", "hello",
                stats='{"n": 1}', ts="2026-07-04T00:00:00+00:00",
            )
            row = conn.execute(
                "SELECT ts, job, level, message, stats FROM health"
            ).fetchone()
            assert row["ts"] == "2026-07-04T00:00:00+00:00"
            assert row["job"] == "ingest"
            assert row["level"] == "info"
            assert row["message"] == "hello"
            assert row["stats"] == '{"n": 1}'
        finally:
            conn.close()

    def test_default_ts_is_current_iso_and_stats_nullable(self):
        conn = _mem_v5()
        try:
            db.log_health(conn, "score", "warn", "msg")
            row = conn.execute("SELECT ts, stats FROM health").fetchone()
            assert row["stats"] is None
            parsed = datetime.datetime.fromisoformat(row["ts"])
            assert parsed.tzinfo is not None  # tz-aware UTC isoformat
        finally:
            conn.close()

    def test_visible_on_same_autocommit_conn_without_explicit_commit(self):
        # log_health deliberately does NOT commit; on an autocommit conn the row
        # is nonetheless durable immediately.
        conn = _mem_v5()
        try:
            db.log_health(conn, "fetch", "error", "boom")
            assert conn.execute("SELECT COUNT(*) FROM health").fetchone()[0] == 1
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# record_run / recent_runs  (integration)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestRunAttribution:
    def test_record_run_with_tunables_upserts_config_version(self):
        conn = _mem_v5()
        try:
            db.record_run(
                conn, "ingest", "abc123", '{"kept": 5}',
                tunables='{"knob": 1}', ts="2026-07-04T00:00:00+00:00",
            )
            cv = conn.execute(
                "SELECT hash, first_seen, tunables FROM config_versions"
            ).fetchone()
            assert cv["hash"] == "abc123"
            assert cv["first_seen"] == "2026-07-04T00:00:00+00:00"
            assert cv["tunables"] == '{"knob": 1}'
            run = conn.execute(
                "SELECT job, config_hash, stats, ts FROM runs"
            ).fetchone()
            assert run["job"] == "ingest"
            assert run["config_hash"] == "abc123"
            assert run["stats"] == '{"kept": 5}'
            assert run["ts"] == "2026-07-04T00:00:00+00:00"
        finally:
            conn.close()

    def test_record_run_without_tunables_skips_config_version(self):
        conn = _mem_v5()
        try:
            db.record_run(conn, "score", "h1", '{"n": 1}')
            assert conn.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        finally:
            conn.close()

    def test_record_run_tunables_insert_or_ignore_keeps_first(self):
        conn = _mem_v5()
        try:
            db.record_run(conn, "ingest", "dup", "{}", tunables='{"v": 1}',
                          ts="2026-01-01T00:00:00+00:00")
            db.record_run(conn, "ingest", "dup", "{}", tunables='{"v": 2}',
                          ts="2026-02-02T00:00:00+00:00")
            cv = conn.execute(
                "SELECT COUNT(*) AS n, MIN(tunables) AS t FROM config_versions"
            ).fetchone()
            assert cv["n"] == 1
            assert cv["t"] == '{"v": 1}'  # OR IGNORE kept the first snapshot
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        finally:
            conn.close()

    def test_recent_runs_newest_first_job_filter_and_limit(self):
        conn = _mem_v5()
        try:
            for i in range(3):
                db.record_run(conn, "ingest", "h", '{"i": %d}' % i)
            db.record_run(conn, "score", "h", '{"s": 1}')

            all_runs = db.recent_runs(conn)
            assert [r["job"] for r in all_runs][0] == "score"  # newest by id first
            assert len(all_runs) == 4

            only_ingest = db.recent_runs(conn, job="ingest")
            assert len(only_ingest) == 3
            assert all(r["job"] == "ingest" for r in only_ingest)
            # newest ingest first
            assert only_ingest[0]["stats"] == '{"i": 2}'

            limited = db.recent_runs(conn, limit=2)
            assert len(limited) == 2
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# _iso_week_bounds  (pure)
# --------------------------------------------------------------------------- #
class TestIsoWeekBounds:
    @pytest.mark.parametrize(
        "week, start, end",
        [
            ("2026-W24", "2026-06-08T00:00:00+00:00", "2026-06-15T00:00:00+00:00"),
            ("2026-W01", "2025-12-29T00:00:00+00:00", "2026-01-05T00:00:00+00:00"),
            ("2026-W53", "2026-12-28T00:00:00+00:00", "2027-01-04T00:00:00+00:00"),
            ("2020-W53", "2020-12-28T00:00:00+00:00", "2021-01-04T00:00:00+00:00"),
            ("2021-W01", "2021-01-04T00:00:00+00:00", "2021-01-11T00:00:00+00:00"),
        ],
    )
    def test_known_answers(self, week, start, end):
        assert db._iso_week_bounds(week) == (start, end)

    def test_span_is_exactly_seven_days_and_starts_monday(self):
        start_s, end_s = db._iso_week_bounds("2026-W24")
        start = datetime.datetime.fromisoformat(start_s)
        end = datetime.datetime.fromisoformat(end_s)
        assert end - start == datetime.timedelta(days=7)
        assert start.weekday() == 0  # Monday
        assert start.tzinfo == datetime.timezone.utc
        assert (start.hour, start.minute, start.second) == (0, 0, 0)


# Property-based variant: only defined when hypothesis is importable so the rest
# of the suite still runs on a box without it (see WRITER_GUIDE marker rules).
try:  # pragma: no cover - availability probe
    import hypothesis  # noqa: F401
    from hypothesis import given, strategies as st

    _HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover
    _HAS_HYPOTHESIS = False

if _HAS_HYPOTHESIS:

    @pytest.mark.property
    @given(year=st.integers(min_value=2000, max_value=2100),
           week=st.integers(min_value=1, max_value=52))
    def test_iso_week_bounds_property(year, week):
        start_s, end_s = db._iso_week_bounds("%04d-W%02d" % (year, week))
        start = datetime.datetime.fromisoformat(start_s)
        end = datetime.datetime.fromisoformat(end_s)
        assert end - start == datetime.timedelta(days=7)
        assert start.weekday() == 0
        assert start.tzinfo == datetime.timezone.utc
        assert start.date().isocalendar()[:2] == (year, week)


# --------------------------------------------------------------------------- #
# Module constants / DBError
# --------------------------------------------------------------------------- #
class TestModuleConstants:
    def test_schema_version_and_dberror(self):
        assert db.SCHEMA_VERSION == 5
        assert issubclass(db.DBError, Exception)
        assert "CREATE TABLE" in db.SCHEMA
