# Production Test Suite — Design & Plan

**Date:** 2026-07-04
**Branch:** `tests/production-suite` (additive only; the 18 in-flight WIP files on
`sources/expansion-2026-07` are carried in the working tree, untouched, and never committed here)
**Status:** approved, building

## Goal

Bring the whole repository to a production-grade automated-test posture:

- A full **pytest** suite for the `signalpipe` pipeline (~10k LOC / 55 modules, currently **zero** tests).
- **Expanded** vitest + Playwright coverage on the Astro/TS site.
- **Coverage reporting** everywhere (report-only, no hard gate yet).
- **Full CI + git-hook automation**: a Python CI job, pre-commit/pre-push hooks, and a nightly live-smoke workflow.

Backing recon: a 9-agent map catalogued **478 concrete test targets** — 301 unit / 130 integration /
28 property / 19 live-smoke — plus 95 testability hazards. This doc distils that into an executable plan.

## Approved decisions

| Decision | Choice |
|---|---|
| Scope | **Both**, pipeline-heavy (build signalpipe from scratch + expand JS) |
| Live tests | **Hermetic + opt-in live smoke** (fast/deterministic gate every push; real-service `-m live` runs nightly, never blocks PRs; LLM-spend tests separately env-gated) |
| CI / hooks | **Full** (Python CI job + coverage upload + pre-commit hooks + nightly scheduled live workflow) |
| Coverage gate | **Report-only for now** (ratchet later; no `--cov-fail-under`) |
| `scripts/*.mjs` | **Subprocess tests, no refactor** (product code untouched) |
| `gh` branch protection | Scripted but **not applied without an explicit go** (repo-settings change) |

## The governing hazard

`db.assert_safe_path`, `config.Config.db_path`, and the `ConfigError`/`DBError` guards **raise on the
literal substring `"Mobile Documents"`** to stop SQLite WAL running inside iCloud Drive. The entire repo
lives under `~/Library/Mobile Documents/…`, so:

- Every DB-backed test uses **`db.connect_rw(tmp_path / "signal.db")`** — pytest `tmp_path` resolves under
  `/private/var/folders/…`, outside iCloud, so the guard passes.
- An **autouse `redirect_state_dirs` fixture** monkeypatches every `$HOME`-derived module singleton
  (`quota.HOLD_PATH`, `config.STATE_DIR`, `db.BACKUP_DIR`, `downtime.PAUSE_FILE`/`DIGEST_LOCK`,
  `publish.LOCK_PATH`, `installer.APP_DIR`/`LOGS_DIR`/`AGENTS_DIR`) to tmp dirs. **No test can write to the
  real `~/.local/state`, `~/Documents/backup`, or `~/Library/LaunchAgents`.**

## Environment constraints

- **Python 3.9.22** (pyenv) — all tests must be 3.9-compatible.
- Installed: `pytest`, `pytest-cov`, `coverage`, `httpx`, `feedparser`, `trafilatura`, `anthropic`, `ruff`.
- **Missing**: `respx`, `freezegun`, `hypothesis` → the suite is **dependency-light**:
  - HTTP faking via **`httpx.MockTransport`** (for `PoliteClient` internals) and **injected fake clients**
    (for ingest parsers, which take a `client` arg).
  - Time faking via **monkeypatched module-level clocks** (`_now_iso`, `datetime`, `time.*`).
  - `hypothesis` added to dev-reqs and used **only** where it clearly pays (canonicalization idempotence,
    jaccard bounds), guarded by an import-skip so the suite still runs without it.
- SQLite `date('now')`/`datetime('now')` are DB-clock; freezegun wouldn't help anyway — **seed rows with
  in-test dates** and mind the documented `'T'`-vs-space ISO comparison gotcha.
- Dev-box libsqlite is `< MIN_SQLITE (3,51,3)`, so `sqlite_version_warning()` returns non-None here —
  version-warning assertions monkeypatch `db.sqlite_version`.

## Layout

```
pipeline/
  pyproject.toml          # pytest ini + coverage config (see below)
  requirements-dev.txt     # pytest, pytest-cov, hypothesis, + hermetic-path runtime deps for CI
  tests/
    conftest.py           # shared fixtures (below) — writers MUST NOT edit this
    fixtures/             # trimmed recorded samples (JSON/XML/RSS/Atom/OPML/HTML/markdown) + signal.min.json
    test_<module>.py      # ONE file per source module (~40 files)
```

`pipeline/pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]                 # rootdir=pipeline/ → `import signalpipe.*` resolves
testpaths = ["tests"]
addopts = "-ra --strict-markers --strict-config -m 'not live'"
markers = [
  "integration: touches sqlite/filesystem/subprocess; still no network",
  "live: hits real external services or spends money; opt-in via -m live",
  "property: randomized / property-based (needs hypothesis)",
]

[tool.coverage.run]
source = ["signalpipe"]
branch = true

[tool.coverage.report]
show_missing = true
# No fail_under yet — report-only, ratchet later.
```

- **unit** = default (unmarked), gates every push.
- `test_<module>.py` per source module; `@pytest.mark.integration` / `.live` / `.property` on individual tests.
- Default run excludes `live`; CI adds coverage flags; the nightly workflow runs `-m live`.

## Shared fixtures (`conftest.py`)

- `tmp_db` — a real `db.connect_rw(tmp_path/"signal.db")` connection (gives the `sqlite3.Row` factory
  `spend.py`/`feed.py` require; a bare `:memory:` tuple cursor would silently break them).
- `real_cfg` — a `config.Config` on a committed minimal `signal.min.json` that passes the strict
  10-section `_validate` (both-backend tier maps, valid selector, non-empty channels).
- `fake_cfg` — a lightweight duck-typed cfg for pure-logic callers that only read a few knobs.
- `FakePoliteClient` — the primary ingest seam: `.fetch(url, conditional=…)` returns canned
  `FetchResult`s **keyed on each module's hardcoded endpoint URL** (the JSON fetchers ignore `source_row`,
  so responses can't be steered by `source_row['url']`).
- `fetch_result()` — `FetchResult` factory (ok / 304 / error / oversized).
- `source_row()` — sqlite3.Row-shaped dict factory (includes `mode`, `slug`, `url`, `type`, `id` — reddit
  reads `mode` unconditionally and IndexErrors without it).
- `mock_transport(handler)` — builds an `httpx.Client(transport=httpx.MockTransport(handler))` for
  `PoliteClient`-level tests (rate-limit, conditional GET, body cap, deadline, hashing, cache).
- `frozen_clock` — monkeypatches a module's `_now_iso`/`datetime` to a fixed instant.
- `redirect_state_dirs` (**autouse**) — the safety fixture above.
- `fixture_bytes(name)` — loads `tests/fixtures/<name>` as bytes.

**Writers add local helpers inside their own file** if they need something bespoke — they never edit
`conftest.py` (prevents parallel write races during the build).

## Per-subsystem test inventory (from the map)

| Subsystem | Modules | Mapped tests | Notes |
|---|---|---|---|
| ingest — social/aggregator | hn, reddit, lobsters, devto, mastodon, bluesky, stackexchange | 56 | fake client keyed on hardcoded URL; hn 0-indexed vs lobsters 1-indexed pagination; reddit is the only one reading `source_row`; stderr diagnostics via `capsys` |
| ingest — news | arxiv, gdelt, googlenews, wikipedia_events, rss, sources_misc | 48 | feedparser/defusedxml oracles; GN resolution is order/state-dependent; wiki/GitHub scrapers coupled to real markup fixtures; bytes-not-str content contract |
| ingest — core | pipeline, registry, bulk_import, fetch_http, __init__ | 55 | `fetch_http` is the HTTP engine (MockTransport); registry uses a ThreadPool (patch `probe_url` pure); orchestrators construct their own `PoliteClient` (patch it); cascading `cfg.save()` side effects |
| llm | adapter, backend_api, backend_cli, backend_local, quota, spend, schemas, __init__ | 49 | patch `adapter.<backend>.run` (the adapter's own ref); local branch bypasses cap+ledger; `backend_cli`=subprocess, `backend_local`=urllib, `backend_api`=anthropic; `consensus()` majority math is subtle |
| core | db, config, models, canonical, dedup | 54 | migrations v1→v5 (v1→v2 is destructive rebuild — fresh DB per case); canonicalize/jaccard property tests; `registered_domain` is naive last-two-labels (no PSL) |
| scoring/curation | score, curate, promote, topics, retag, period, downtime | 46 | pure scoring math (`_consensus`/`_engagement`/`_recency`/`latin_ratio`); downtime shells to macOS binaries (fixture `subprocess`); `topics` mirrors `taxonomy.ts` |
| publishing | digest, publish, render, feed, kb, backfill, fetch_article | 63 | `git_publish` shells real git in tmp repos; ISO `'T'`-vs-space window gotcha; adapter exception taxonomy (UsageLimitExhausted/LLMError/SpendCapExceeded); extraction branch depends on trafilatura presence |
| CLI/runtime | __main__, __init__, worker, server, installer | 46 | worker `BlockingScheduler.start()` + SIGTERM (fake scheduler); server via FastAPI `TestClient`; installer real launchctl/copytree (patch); lazy optional-dep imports |
| JS site | src/lib/*, src/site.ts, scripts/*.mjs + existing tests | 61 | add `taxonomy.ts`; deepen feeds/schema/sources/site; vitest coverage; `.mjs` via subprocess+tmp fixtures; extend e2e (404, category, RSS, archive/stats/about/coverage) |

## CI + hooks

- **`.github/workflows/deploy.yml`** — add a **`pytest` job** parallel to the JS job:
  setup-python 3.9 → `pip install -r pipeline/requirements-dev.txt` → `pytest` (with coverage) →
  upload `coverage.xml` + htmlcov artifact. `deploy` gates on **both** test jobs.
- **`.pre-commit-config.yaml`** — `ruff check` + `ruff format` on commit (Python only);
  **pre-push** stage runs `pytest -m "not live and not integration"` + `npm run test:unit`.
- **`.github/workflows/live-smoke.yml`** — nightly `schedule:` cron running `pytest -m live`
  (network-only; **non-blocking**, job summary). LLM-spend tests excluded (need `SIGNAL_LIVE_LLM=1`).
- **`gh` branch protection** — a script that requires both test jobs as status checks; **NOT run without
  an explicit go** (touches repo settings / is outward-facing).

## Build plan (waves)

1. **Scaffold** (done by hand, verified green before fan-out): `conftest.py`, `fixtures/`, `signal.min.json`,
   `pyproject.toml`, `requirements-dev.txt`, CI job, hooks, live workflow. Prove `import signalpipe.*` works
   and a smoke test passes.
2. **Writer wave** — fan out ~one agent per source module; each writes `test_<module>.py` **and runs
   `pytest` on just its file until green** against the real code (characterization/regression: assertions
   derived from actual behavior, never tautologies). Agents read shared fixtures, don't edit conftest.
3. **Aggregate + repair** — run the whole suite + coverage; a repair pass fixes any straggler / collision.
4. **Adversarial review** — agents audit each file for vacuous/over-mocked/tautological tests and fix.
5. **Verify** — full green suite, coverage report captured, JS suite green, `verify` skill on the harness.

## Non-goals / honesty

- 478 is the mapped **ceiling**, not a contract. Priority is the ~431 hermetic unit+integration targets;
  live-smoke gets scaffolding + representative cases, not exhaustive real-service coverage.
- No refactor of product code (scripts stay as-is; tested via subprocess).
- No pushing / PR / branch-protection changes without an explicit go.
- Final report states the **real** test count and coverage numbers — no inflated claims.

## Risks & mitigations (top hazards from the 95 mapped)

| Risk | Mitigation |
|---|---|
| iCloud `"Mobile Documents"` guard | `tmp_path` DBs + autouse `redirect_state_dirs` |
| Un-injected `PoliteClient` in orchestrators | monkeypatch `PoliteClient` / `probe_url` at module scope |
| `adapter` imports backends at top | patch `adapter.backend_cli.run` etc., not the origin modules |
| Pervasive wall-clock | injectable seams where they exist; else monkeypatch module clock; seed DB dates for SQL `now` |
| Optional deps absent in CI (anthropic/fastapi/apscheduler/markdown) | pin them in `requirements-dev.txt`; unit tests rely on lazy imports so they don't need them |
| Real threads (registry ThreadPool) | patch the per-item worker to a pure function; cap workers |
| Destructive migration path | fresh DB per migration case, never a shared fixture |
| `.mjs` `process.exit` at import | subprocess execution against tmp fixtures, never `import` |
