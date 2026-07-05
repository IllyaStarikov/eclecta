"""Harness smoke tests — prove the scaffold works before the per-module suites land.

If any of these fail, the shared fixtures are broken and every other test is suspect.
"""

from __future__ import annotations

import pathlib

import pytest

import signalpipe.config as config_mod
import signalpipe.db as db_mod
import signalpipe.downtime as downtime_mod
import signalpipe.installer as installer_mod
import signalpipe.llm.quota as quota_mod
import signalpipe.publish as publish_mod


def test_package_imports():
    import signalpipe  # noqa: F401
    import signalpipe.canonical  # noqa: F401
    import signalpipe.dedup  # noqa: F401
    import signalpipe.ingest.hn  # noqa: F401
    import signalpipe.llm.adapter  # noqa: F401
    import signalpipe.score  # noqa: F401


def test_cfg_fixture_is_valid(cfg):
    # A fully-valid Config: every accessor the suite leans on resolves.
    assert cfg.backend["selector"] in ("subscription", "api")
    assert cfg.channels
    assert cfg.model_for("triage") == "claude-haiku-4-5"
    assert cfg.backend_for("digest") == "subscription"
    assert set(cfg.digests) >= {"daily", "weekly"}


def test_conn_fixture_has_schema_and_row_factory(conn):
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"sources", "clusters", "items", "curations", "digests", "spend"} <= tables
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    # row_factory is sqlite3.Row → keyed access works (spend.py depends on this).
    assert int(row["value"]) == db_mod.SCHEMA_VERSION


def test_db_path_is_outside_icloud(db_path):
    assert "Mobile Documents" not in str(db_path)


def test_redirect_state_dirs_repoints_every_home_singleton(tmp_path):
    # Autouse fixture already ran; all singletons must now live under tmp, never $HOME.
    for value in (
        config_mod.STATE_DIR,
        db_mod.BACKUP_DIR,
        quota_mod.HOLD_PATH,
        quota_mod.STATE_DIR,
        downtime_mod.PAUSE_FILE,
        downtime_mod.DIGEST_LOCK,
        publish_mod.LOCK_PATH,
        installer_mod.APP_DIR,
        installer_mod.LOGS_DIR,
        installer_mod.AGENTS_DIR,
        installer_mod.WRAPPER,
        installer_mod.WATCHDOG,
        installer_mod.SIGNAL_SHIM,
    ):
        text = str(value)
        assert "/Library/Logs/signal" not in text
        assert "/LaunchAgents" not in text
        assert ".local/state/signal" not in text
        assert "Documents/backup/signal" not in text


def test_seed_builds_related_rows(conn, seed):
    src = seed.source(slug="hn", name="Hacker News")
    cid = seed.cluster(title="A story about GPUs and compilers")
    seed.item(cid, src)
    seed.surface(cid, src)
    seed.article(cid)
    seed.curation(cid, relevance_score=9)
    did = seed.digest(kind="daily", period_key="2026-07-04")

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert conn.execute("SELECT relevance_score FROM curations").fetchone()["relevance_score"] == 9
    assert conn.execute("SELECT kind FROM digests WHERE id=?", (did,)).fetchone()["kind"] == "daily"


def test_fake_client_returns_canned_and_records(fake_client, make_result):
    client = fake_client(responses={"https://x/1": make_result(content=b"hello", status=200)})
    res = client.fetch("https://x/1")
    assert res.content == b"hello" and res.status == 200
    assert client.requested == ["https://x/1"]
    with pytest.raises(AssertionError):
        client.fetch("https://x/unmapped")


def test_polite_client_factory_uses_mock_transport(polite_client_factory):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok", headers={"ETag": "abc"})

    pc = polite_client_factory(handler)
    result = pc.fetch("https://example.com/feed")
    assert result.status == 200
    assert result.content == b"ok"


def test_load_helpers(load_json, fixtures_dir):
    data = load_json("signal.min.json")
    assert data["backend"]["selector"] == "subscription"
    assert isinstance(fixtures_dir, pathlib.Path)
