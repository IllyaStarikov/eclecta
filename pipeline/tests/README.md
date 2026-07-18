# signalpipe test suite

A hermetic-by-default pytest suite covering the signalpipe pipeline, with an opt-in
live-smoke tier for the real external integrations.

## Running

```bash
cd pipeline
python3 -m pytest                 # hermetic suite (unit + integration); live tests deselected
python3 -m pytest -m integration  # just the sqlite/subprocess/filesystem integration tests
python3 -m pytest --cov=signalpipe --cov-report=term-missing   # with coverage
python3 -m pytest -m live         # LIVE: hits real services (HN/Reddit/arXiv/...). Slow, flaky, network.
```

The pytest config lives in `pipeline/pyproject.toml` (`pythonpath=["."]` makes
`import signalpipe.*` resolve without an install step).

### Python + dev dependencies

The suite targets **Python 3.13**. A dedicated pyenv virtualenv keeps its deps isolated:

```bash
pyenv virtualenv 3.13.6 eclecta      # once
pyenv local eclecta                  # pins this repo to it (writes .python-version)
pip install -r pipeline/requirements-dev.txt
```

Installs the test runner + the runtime deps a few integration tests import directly
(`fastapi`, `apscheduler`, `feedparser`, `jinja2`, `markdown`, `anthropic`, `defusedxml`,
`httpx`, `hypothesis`). The heavy extraction stack (`trafilatura`/`readability-lxml`) is
deliberately not required — those code paths are monkeypatched.

## Markers

| Marker | Meaning | In CI? |
|---|---|---|
| *(none)* | unit — pure logic / parsers with fakes; fast, deterministic | yes (every push) |
| `integration` | real sqlite, tmp filesystem, or subprocess — still **no network** | yes |
| `live` | real external services / spends money; deselected by default | nightly only, non-blocking |
| `property` | hypothesis-based; `importorskip`-guarded so the suite runs without hypothesis | yes |

`live` tests self-skip unless `SIGNAL_LIVE=1`; money-spending LLM live tests additionally
require `SIGNAL_LIVE_LLM=1`, so the nightly `live-smoke` workflow never spends.

## Safety

`conftest.py`'s autouse `redirect_state_dirs` repoints every `$HOME`-derived module singleton
(`config.STATE_DIR`, `db.BACKUP_DIR`, `quota.HOLD_PATH`, `downtime.PAUSE_FILE`,
`publish.LOCK_PATH`, the `installer.*` paths) at pytest `tmp_path`. **No test can touch the real
`~/.local/state`, `~/Documents/backup`, or `~/Library/{Logs,LaunchAgents}`.** DB-backed tests use
`db.connect_rw(tmp_path/...)`, which is outside iCloud and so passes the "Mobile Documents"
safe-path guard.

## Shared fixtures (`conftest.py`)

`conn` / `db_path` (tmp SQLite), `cfg` (real Config from a tmp copy of `fixtures/signal.min.json`),
`seed` (schema-accurate row builder), `fake_client` + `make_result` (ingest-parser HTTP seam),
`polite_client_factory` (real `PoliteClient` over `httpx.MockTransport`), `freeze_now_iso`,
`load_bytes`/`load_text`/`load_json`. See the file for signatures.

## ⚠️ This suite tracks the working tree (in-flight WIP included)

The suite was written against the **current working tree**, which at authoring time includes
in-flight pipeline work that is not yet committed (the quota-hold / usage-limit / run-attribution
feature: `llm/quota.py` plus additions to `db.py`, `llm/adapter.py`, `llm/__init__.py`,
`llm/backend_cli.py`, `curate.py`, `digest.py`, `score.py`, `worker.py`, `__main__.py`,
`fetch_article.py`, `ingest/pipeline.py`). The tests cover that code.

**Consequence:** commit these tests **together with** that pipeline WIP. Running the suite against
a checkout that lacks the WIP will fail to import (`conftest` imports `llm.quota`). Do not push the
CI-triggering branch until the pipeline WIP it tests is committed alongside it.
