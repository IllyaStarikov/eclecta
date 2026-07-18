# Eclecta / signalpipe: Operating Runbook

How the pipeline runs day to day, and how to work on it safely.

## Mental model

`signalpipe` runs locally under launchd and pushes editions into this repo,
which GitHub Actions builds to `eclecta.co`. The site never depends on the
pipeline being online: if the worker is off, the last-published edition stays
up. The design goals are to stay live, stay inexpensive, and never contend for
memory while the machine is in interactive use.

- **Canonical source:** `pipeline/signalpipe/` in this repo.
- **Running copy:** a TCC-safe runtime copy under `~/.local/state/signal/app/`
  is what launchd actually executes. Keep it in sync (see below); never let the
  runtime copy become the only copy again.
- **Runtime config:** `~/.local/state/signal/app/config/signal.json` (see
  `pipeline/config/signal.example.json` for a redacted reference). It is
  reloaded at the start of every job, so a config edit takes effect on the next
  job with no restart.
- **Database:** `~/.local/state/signal/signal.db` (SQLite, WAL). The worker is
  the sole writer. Never place the DB on a synced/cloud filesystem (WAL
  corruption); the config guards against this.
- **Logs:** `~/Library/Logs/signal/`. **Dashboard:** `http://127.0.0.1:8765/`.

## The launchd jobs

Three agents (`launchctl list | grep signal`):

- `…signal.server`, the review dashboard + parameterized RSS on port 8765.
- `…signal.worker`, the loop: ingest → score → fetch → curate → publish →
  editions, each on its own interval, most gated by downtime.
- `…signal.watchdog`, restarts the worker if its heartbeat goes stale.

Job bodies live in `pipeline/ops/`.

## Cadence

Worker intervals live in `signal.json` (`cadences`). Editions do NOT run on a
cron: the worker fires a single editions dispatcher every
`downtime.editions_interval_min` (default 30 min) and `period.py` decides which
kinds are due. Editions run daily on weekday mornings, weekly on Fridays, monthly
on the first weekday, quarterly on the first weekday of Jan/Apr/Jul/Oct, and
yearly on the first weekday of January. The `digests.*.cron` keys are
documentation of the intended times only — the scheduler never reads them, so
editing one changes nothing. `period.py` is the single authority for windows and
due-dates.

## Downtime gating (why editions sometimes don't run)

Heavy stages (`curate`, `editions`) run only when `downtime.is_open`: on AC
power, after a few minutes of user idle, with enough free RAM and no swap
thrash. This keeps the machine responsive during interactive use. If digests
stop appearing while picks stay fresh, the usual cause is that the machine is
in active use, so the editions window never opens. Leave it idle on AC to let a
backlog clear, or loosen the gates temporarily.

## Model routing & cost

`backend.selector` selects the LLM path; on `subscription` the `claude` CLI
bills the Claude Max plan. Tier map: triage/judge run on a local model
(Ollama, free); deep/write on Sonnet; digest on Opus at high effort. The
`spend.*` caps are quota brakes on an *estimate*, not charges on the
subscription; they stop runaway loops.

- **Cost hazard:** if a real `ANTHROPIC_API_KEY` is visible to the `claude`
  CLI, it takes precedence over the Max login and you pay metered API rates.
  `backend_cli` pops the key from the child env to prevent this; keep it that
  way. If unexpected metered costs appear, check the CLI's environment first.

## Syncing repo ↔ deployment

This repo is canonical; the runtime copy is a deployment. After changing
pipeline code or docs here:

```
rsync -a --exclude __pycache__ --exclude '*.pyc' \
  pipeline/signalpipe/ ~/.local/state/signal/app/signalpipe/
cp docs/digest-style.md docs/editorial-policy.md ~/.local/state/signal/app/doc/
```

`python3 -m signalpipe install` is the supported path to (re)deploy the runtime
copy + launchd agents. The editorial docs are read at runtime from
`~/.local/state/signal/app/doc/`, so a doc change only takes effect after the
copy above (or an install).

## Hand-authoring / backfilling an edition

When an edition is needed that the pipeline can't produce (a backfill, a
repair), author it into the DB so the DB stays canonical and future runs dedup
against it. This mirrors `digest.py:run` without the model call:

1. Back up the DB.
2. Gate the deploy: set `site.push=false` so nothing ships mid-work.
3. Pull the window's finalists (same query as `digest.py:_gather`): `curations`
   with `status='done'`, `skip=0`, `relevance ≥ min`, `curated_at` in the
   period window (`period.parse_period`), not already run in a prior edition of
   that cadence (`published_ledger`).
4. Write the prose to `digest-style.md` + `editorial-policy.md` +
   `cadence-templates.md`. Insert a `digests` row (`INSERT … ON CONFLICT DO
   UPDATE`) with `title`, `blurb`, `body_md`, `cluster_ids`, `window_*`,
   `model_used`, `generated_at`; insert the edition's `story_id`s into
   `published_ledger`.
5. Export: `python3 -m signalpipe publish --what digests --no-push`, or write
   the new period's `.md` via `write_digest_md`. Commit promptly: uncommitted
   files under `src/content/digests/` are discarded by the worker's
   `_clean_pipeline_dirt`.
6. Verify (`npm run check && npm run build && npm test`), then restore `push`
   and let the worker ship.

## Tuning hyperparameters

All in `signal.json`; safe to stage, effective next job. Change one axis at a
time, keep a dated backup, and read the next few job logs before changing more.

- **Selection funnel:** `funnel.*` and per-cadence `digests.<kind>.min_relevance`
  / `max_items`.
- **Scoring:** `score_weights` (consensus / engagement / reputation / recency /
  topic_match + `recency_halflife_hours`); the sum need not be 1.
- **Routing & spend:** `backend.tier_overrides`, `tiers.*`, `spend.*`.
- **Downtime:** `downtime.*`, loosen to clear a backlog, tighten to protect an
  interactive session.

## Going live / reloading after changes

Config edits need no reload. Code changes need a redeploy + worker reload:

```
launchctl kickstart -k gui/$(id -u)/io.starikov.signal.worker
```

Do not reload or start a service without an explicit go: premature activation
has real cost (billing and machine resources). Stage everything, hand over the
evidence, wait.

## Monitoring & troubleshooting

- **Is it alive?** `cat ~/.local/state/signal/heartbeat`; tail the worker log.
- **Why no digest?** the downtime gate (above), or a `publish_error` on the row.
- **Publish refuses / "working tree dirty."** A human left an uncommitted file
  *outside* `src/data | src/content/digests | kb`. Commit or stash it; the
  worker refuses to publish over foreign dirt by design.
- **Stats/picks stale.** `python3 -m signalpipe publish --what refresh`, or wait
  for the periodic refresh. Picks window is 7 days, relevance ≥ 6.
- **Costs unexpected.** Check for a stray `ANTHROPIC_API_KEY` in the CLI's
  environment (see Model routing).
