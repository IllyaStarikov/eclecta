# Eclecta cold-start: the exemplary month (design + record)

**Date:** 2026-07-04 · **Status:** executed

## Problem

Eclecta's prose digests had stopped at June 23 while `picks.json`/`stats.json`
stayed fresh (the worker kept curating), so the homepage showed fresh picks but
a stale edition. The goal: make the site look as if it had run beautifully for a
month, and leave behind the guidance, tuning, and versioned code that make the
*future* automated runs that good. Everything hand-authored on Opus, from the
DB's real curated finalists, with the hindsight the live pipeline never had.

## Decisions

1. **Authorship:** hand-authored from the DB's real curated finalists, not a
   blind pipeline replay.
2. **Scope:** bridge the gap (dailies Jun 24→Jul 3, weeklies W25–W27) plus a
   flagship **Q2 2026 quarterly** (never produced before). Refresh picks/stats
   to Jul 4. Existing May/June content: light QA only.
3. **Go-live is gated.** Everything staged and verified; the launchd worker is
   not reloaded until an explicit go.

## Architecture & source of truth

- **The `digests` DB table is canonical.** `publish.py:write_digest_md()`
  renders `src/content/digests/<kind>/<key>.md` from a row. Each authored
  edition is inserted into `digests` (+ `published_ledger`, mirroring
  `digest.py:run`) so future pipeline runs treat it as published and dedup
  against it.
- **Finalist selection replicates `digest.py:_gather`:** `curations` with
  `status='done'`, `skip=0`, `relevance ≥ min`, `curated_at` in the period
  window, excluded if a prior edition of the same cadence already ran the
  `story_id`. Daily min 6 / ≤25; weekly min 7 / ≤40; quarterly min 7 / ≤30 +
  the May & June monthly bodies (hierarchical).
- **Windows/keys from `period.py`.** Gap = 8 dailies, 3 weeklies (W25/W26/W27),
  1 quarterly (`2026-Q2`, Apr 1–Jul 1). June monthly already covers June.

## Worker coordination (safety)

The worker is live and auto-pushes to origin (→ GitHub Pages). To gate the
deploy without stopping a service (which fights the watchdog):

- **Stage `site.push=false`** so the worker keeps committing picks/stats
  *locally* but nothing deploys until go-live.
- The worker owns `picks.json`/`stats.json`; the backfill owns digests
  (authored rows carry `published_at` so the worker won't re-touch them).
- Author → export `.md` → **commit promptly**: uncommitted files under
  `src/content/digests/` are discarded by the worker's `_clean_pipeline_dirt`,
  and an uncommitted file *outside* the pipeline-owned paths makes its publish
  refuse. The DB is backed up before any write.

## Guidance docs (AI-authored, versioned, pipeline-consumed)

- `docs/digest-style.md` — voice canon.
- `docs/editorial-policy.md` — *what to publish* + emphasis (`digest.py` injects
  this into the digest system prompt; it was absent until this cold-start).
- `docs/cadence-templates.md` — per-kind skeletons + blurb/header/lede craft.
- `docs/operating-runbook.md` — the day-to-day/weekly/monthly workflow.

## Verification & iterate ×5

Each cycle: `astro check` → build → `test:unit` + `test:e2e` → subscribe to the
feeds locally (fetch built `rss.xml`, per-kind, per-category; confirm they parse
and the new editions appear) → `npm run capture` (light/dark/mobile) and eyeball.

## Non-goals

No rewrite of the strong May/June editions; no activation of pipeline v2
staging; no DNS changes.
