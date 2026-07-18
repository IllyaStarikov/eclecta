"""Shared fixtures for the signalpipe test suite.

Design notes (see docs/superpowers/specs/2026-07-04-production-test-suite-design.md):

* The repo lives under ``~/Library/Mobile Documents`` (iCloud). ``db.assert_safe_path``
  and ``config.Config.db_path`` REFUSE any path containing that substring. Pytest's
  ``tmp_path`` resolves outside iCloud, so every DB-backed test uses a tmp DB.
* ``redirect_state_dirs`` (autouse) repoints every ``$HOME``-derived module singleton at
  tmp so no test can ever touch the real ``~/.local/state``, ``~/Documents/backup`` or
  ``~/Library/{Logs,LaunchAgents}``.
* Writers must NOT edit this file (avoids parallel write races). Add local helpers in your
  own ``test_<module>.py`` instead.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sqlite3
from typing import Any, Callable, Dict, List, Optional

import pytest

import signalpipe.config as config_mod
import signalpipe.db as db_mod
import signalpipe.downtime as downtime_mod
import signalpipe.installer as installer_mod
import signalpipe.llm.quota as quota_mod
import signalpipe.publish as publish_mod
from signalpipe.ingest.fetch_http import FetchResult

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

# A fixed instant used across time-sensitive tests. Chosen well clear of DST edges.
FROZEN_ISO = "2026-07-04T12:00:00+00:00"


# --------------------------------------------------------------------------- #
# Fixture-file loaders
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    return FIXTURES_DIR


@pytest.fixture
def load_bytes() -> Callable[[str], bytes]:
    def _load(name: str) -> bytes:
        return (FIXTURES_DIR / name).read_bytes()

    return _load


@pytest.fixture
def load_text() -> Callable[..., str]:
    def _load(name: str, encoding: str = "utf-8") -> str:
        return (FIXTURES_DIR / name).read_text(encoding)

    return _load


@pytest.fixture
def load_json() -> Callable[[str], Any]:
    def _load(name: str) -> Any:
        return json.loads((FIXTURES_DIR / name).read_text())

    return _load


# --------------------------------------------------------------------------- #
# SAFETY: redirect every $HOME-derived singleton to tmp (autouse, always on)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def redirect_state_dirs(tmp_path, monkeypatch):
    """Repoint every module-level path constant computed from $HOME at import.

    These were resolved once at import time, so patching ``config.STATE_DIR`` does
    NOT retroactively fix constants derived from it (e.g. ``downtime.PAUSE_FILE``) —
    each is patched explicitly. Attribute names are asserted to exist (default
    ``raising=True``), so a rename in the product code fails this fixture loudly
    instead of silently letting a test escape to the real home directory.
    """
    state = tmp_path / "state"
    app = state / "app"
    logs = tmp_path / "logs"
    agents = tmp_path / "agents"
    backup = tmp_path / "backup"
    binpath = tmp_path / "bin"
    for d in (state, app, logs, agents, backup, binpath):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config_mod, "STATE_DIR", state)
    monkeypatch.setattr(db_mod, "BACKUP_DIR", backup)
    monkeypatch.setattr(quota_mod, "STATE_DIR", state)
    monkeypatch.setattr(quota_mod, "HOLD_PATH", state / "quota_hold.json")
    monkeypatch.setattr(downtime_mod, "PAUSE_FILE", state / "downtime.json")
    monkeypatch.setattr(downtime_mod, "DIGEST_LOCK", state / "digest.lock")
    monkeypatch.setattr(publish_mod, "LOCK_PATH", state / "publish.lock")
    monkeypatch.setattr(installer_mod, "APP_DIR", app)
    monkeypatch.setattr(installer_mod, "LOGS_DIR", logs)
    monkeypatch.setattr(installer_mod, "AGENTS_DIR", agents)
    monkeypatch.setattr(installer_mod, "WRAPPER", app / "run-worker.sh")
    monkeypatch.setattr(installer_mod, "WATCHDOG", app / "signal-watchdog.sh")
    monkeypatch.setattr(installer_mod, "SIGNAL_SHIM", binpath / "signal")
    return state


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_path(tmp_path) -> pathlib.Path:
    """A tmp SQLite path, guaranteed outside iCloud so the safe-path guard passes."""
    return tmp_path / "signal.db"


@pytest.fixture
def conn(db_path):
    """A writer connection with schema applied and the ``sqlite3.Row`` factory that
    spend/feed/publish code requires (a bare ``:memory:`` tuple cursor breaks them)."""
    connection = db_mod.connect_rw(db_path)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg(tmp_path):
    """A real, fully-valid ``Config`` loaded from a tmp COPY of signal.min.json.

    Loading from a copy (not the fixture in place) means code paths that call
    ``cfg.save()`` / ``write_last_run()`` / ``update_tracking()`` mutate the copy,
    never the committed fixture. ``db_path`` is repointed at tmp so cfg-driven code
    never touches real state.
    """
    src = (FIXTURES_DIR / "signal.min.json").read_text()
    path = tmp_path / "signal.json"
    path.write_text(src)
    config = config_mod.load(path)
    config.data["db_path"] = str(tmp_path / "signal.db")
    return config


# --------------------------------------------------------------------------- #
# HTTP seams
# --------------------------------------------------------------------------- #
class FakePoliteClient:
    """Drop-in for ``PoliteClient`` used by ingest-parser unit tests.

    ``fetch()`` returns a canned ``FetchResult`` chosen by exact URL match, with an
    optional default. Every requested URL is recorded on ``.requested`` for
    assertions (pagination shape, give-up counters, resolve calls). No rate limiting.
    """

    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
        default: Any = None,
        resolver: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self._responses = dict(responses or {})
        self._default = default
        self._resolver = resolver
        self.requested: List[str] = []
        self.resolved: List[str] = []

    def fetch(self, url: str, conditional: bool = True) -> FetchResult:
        self.requested.append(url)
        if url in self._responses:
            value = self._responses[url]
            return value() if callable(value) else value
        if self._default is not None:
            return self._default() if callable(self._default) else self._default
        raise AssertionError("FakePoliteClient: no canned response for %r" % url)

    def resolve(self, url: str) -> Optional[str]:
        self.resolved.append(url)
        if self._resolver is not None:
            return self._resolver(url)
        return url

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def __enter__(self) -> "FakePoliteClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def fake_client():
    """Factory for :class:`FakePoliteClient`."""
    return FakePoliteClient


@pytest.fixture
def make_result() -> Callable[..., FetchResult]:
    """Build a ``FetchResult``. ``content`` may be str (utf-8 encoded) or bytes."""

    def _make(
        content: Any = b"",
        status: int = 200,
        unchanged: bool = False,
        error: Optional[str] = None,
        final_url: Optional[str] = None,
    ) -> FetchResult:
        if isinstance(content, str):
            content = content.encode("utf-8")
        return FetchResult(
            status=status,
            content=content,
            unchanged=unchanged,
            error=error,
            final_url=final_url,
        )

    return _make


@pytest.fixture
def polite_client_factory(cfg, conn):
    """Build a REAL ``PoliteClient`` whose httpx client is backed by a
    ``httpx.MockTransport``. Rate limiting is neutralized so tests never sleep.

    Usage::

        def handler(request):
            return httpx.Response(200, content=b"...")
        pc = polite_client_factory(handler)          # cache-backed
        pc = polite_client_factory(handler, cache=False)
    """
    import httpx

    from signalpipe.ingest import fetch_http

    created: List[Any] = []

    def _make(handler: Callable, cache: bool = True):
        client = fetch_http.PoliteClient(cfg, conn if cache else None)
        client.client = httpx.Client(transport=httpx.MockTransport(handler))
        client.host_intervals = {}
        client.default_interval = 0.0
        created.append(client)
        return client

    try:
        yield _make
    finally:
        for client in created:
            try:
                client.client.close()
            except Exception:  # pragma: no cover
                pass


# --------------------------------------------------------------------------- #
# Clock helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def freeze_now_iso(monkeypatch):
    """Freeze a module's ``_now_iso()`` helper to a fixed ISO instant.

    Many modules define a private ``_now_iso`` used for all their timestamps;
    freezing it makes their DB writes deterministic. Returns the frozen value.
    """

    def _freeze(module, iso: str = FROZEN_ISO) -> str:
        monkeypatch.setattr(module, "_now_iso", lambda: iso)
        return iso

    return _freeze


# --------------------------------------------------------------------------- #
# DB seeding — consistent, schema-accurate row inserts for integration tests
# --------------------------------------------------------------------------- #
def _iso(offset_hours: float = 0.0) -> str:
    return (
        datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)
        + datetime.timedelta(hours=offset_hours)
    ).isoformat()


class Seeder:
    """Insert schema-accurate rows into a test DB and return their ids/rows.

    Columns and defaults mirror ``db.SCHEMA`` (schema version 5). Every method takes
    keyword overrides. Timestamps default to a fixed instant so tests are stable.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _insert(self, table: str, values: Dict[str, Any]) -> int:
        cols = list(values.keys())
        placeholders = ",".join("?" for _ in cols)
        cur = self.conn.execute(
            "INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), placeholders),
            [values[c] for c in cols],
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def source(self, **over: Any) -> int:
        values: Dict[str, Any] = dict(
            slug="example",
            name="Example Source",
            category="ai",
            type="rss",
            url="https://example.com/feed.xml",
            homepage="https://example.com",
            topics=json.dumps(["ai"]),
            reputation=1.0,
            tier=2,
            cadence_min=60,
            paywalled=0,
            enabled=1,
            mode=None,
            added_at=_iso(-72),
        )
        values.update(over)
        return self._insert("sources", values)

    def cluster(self, **over: Any) -> int:
        title = over.pop("title", "Example story about AI models")
        from signalpipe.dedup import story_id, title_key

        canonical = over.get("canonical_url", "https://example.com/story")
        tk = over.pop("title_key", title_key(title))
        values: Dict[str, Any] = dict(
            canonical_url=canonical,
            title=title,
            title_key=tk,
            first_seen=_iso(-6),
            last_seen=_iso(-1),
            surface_count=1,
            score=None,
            score_at=None,
            story_id=story_id(canonical, tk),
        )
        values.update(over)
        return self._insert("clusters", values)

    def item(self, cluster_id: int, source_id: int, **over: Any) -> int:
        values: Dict[str, Any] = dict(
            cluster_id=cluster_id,
            source_id=source_id,
            guid="guid-%d" % (over.get("_n", 1)),
            raw_url="https://example.com/story",
            canonical_url="https://example.com/story",
            title="Example story about AI models",
            author="Jane Doe",
            published_at=_iso(-2),
            ingested_at=_iso(-1),
            points=100,
            comments=42,
            extra=None,
        )
        over.pop("_n", None)
        values.update(over)
        return self._insert("items", values)

    def surface(self, cluster_id: int, source_id: int, **over: Any) -> None:
        values: Dict[str, Any] = dict(
            cluster_id=cluster_id,
            source_id=source_id,
            url="https://news.ycombinator.com/item?id=1",
            points=100,
            comments=42,
            seen_at=_iso(-1),
        )
        values.update(over)
        cols = list(values.keys())
        self.conn.execute(
            "INSERT INTO surfaces(%s) VALUES(%s)" % (",".join(cols), ",".join("?" for _ in cols)),
            [values[c] for c in cols],
        )

    def article(self, cluster_id: int, **over: Any) -> None:
        values: Dict[str, Any] = dict(
            cluster_id=cluster_id,
            source_url="https://example.com/story",
            read_url="https://example.com/story",
            read_kind="primary",
            paywalled=0,
            extracted_at=_iso(-1),
            word_count=800,
            text="Full article body.",
            excerpt="An excerpt.",
            lang="en",
            fetch_status="ok",
        )
        values.update(over)
        cols = list(values.keys())
        self.conn.execute(
            "INSERT INTO articles(%s) VALUES(%s)" % (",".join(cols), ",".join("?" for _ in cols)),
            [values[c] for c in cols],
        )

    def curation(self, cluster_id: int, **over: Any) -> None:
        values: Dict[str, Any] = dict(
            cluster_id=cluster_id,
            status="done",
            tier_used="triage",
            backend_used="subscription",
            model_used="claude-haiku-4-5",
            relevance_score=8,
            why_it_matters="It matters because reasons.",
            notes=json.dumps(["point one", "point two"]),
            summary="A concise summary.",
            channels=json.dumps(["ai"]),
            category="ai",
            subcategories=json.dumps(["ml-research"]),
            novelty="incremental",
            audience="practitioners",
            skip=0,
            cost_usd=0.01,
            curated_at=_iso(-1),
        )
        values.update(over)
        cols = list(values.keys())
        self.conn.execute(
            "INSERT INTO curations(%s) VALUES(%s)" % (",".join(cols), ",".join("?" for _ in cols)),
            [values[c] for c in cols],
        )

    def digest(self, **over: Any) -> int:
        values: Dict[str, Any] = dict(
            kind="weekly",
            period_key="2026-W27",
            window_start=_iso(-168),
            window_end=_iso(0),
            generated_at=_iso(-1),
            model_used="claude-opus-4-8",
            title="This week in tech",
            blurb="A one-sentence standfirst.",
            body_md="# This week\n\nBody.",
            body_html="<h1>This week</h1><p>Body.</p>",
            cluster_ids=json.dumps([1]),
            promoted=0,
        )
        values.update(over)
        return self._insert("digests", values)

    def spend(self, **over: Any) -> None:
        values: Dict[str, Any] = dict(
            day="2026-07-04",
            cli_usd=0.0,
            api_usd=0.0,
            digest_usd=0.0,
            calls=0,
        )
        values.update(over)
        cols = list(values.keys())
        self.conn.execute(
            "INSERT INTO spend(%s) VALUES(%s)" % (",".join(cols), ",".join("?" for _ in cols)),
            [values[c] for c in cols],
        )

    def ledger(self, story_id: str, surface: str, **over: Any) -> None:
        values: Dict[str, Any] = dict(
            story_id=story_id,
            surface=surface,
            edition_key=over.pop("edition_key", ""),
            cluster_id=None,
            first_at=_iso(-1),
        )
        values.update(over)
        cols = list(values.keys())
        self.conn.execute(
            "INSERT INTO published_ledger(%s) VALUES(%s)"
            % (",".join(cols), ",".join("?" for _ in cols)),
            [values[c] for c in cols],
        )


@pytest.fixture
def seed(conn) -> Seeder:
    """A :class:`Seeder` bound to the test ``conn`` for building DB state."""
    return Seeder(conn)
