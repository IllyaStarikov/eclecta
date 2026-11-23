# Eclecta — durable learnings

Reusable insights for future maintenance passes. Add to it when a pass discovers
something a future pass should know. Keep entries short and durable (not one-off
task notes — those go in the dated journal).

## Architecture / where things live
- The site is Astro 5 → GitHub Pages (apex `eclecta.co`, base `/`). Identity is
  centralized in `src/site.ts` (`name`, `kicker`, `tagline`, `description`,
  `KIND_LABEL`). Renaming the publication touches site.ts + pipeline
  `config/signal.json` `site.name` + the GitHub repo + `public/CNAME`.
- Design is one stylesheet, `src/styles/global.css`, driven by data-attributes on
  `<html>` stamped pre-paint in `Base.astro` (must stay in sync with
  `src/scripts/prefs.js`). Colors are `light-dark()` pairs — one source of truth,
  no duplicated dark block.
- The pipeline lives in `pipeline/signalpipe/`. The LIVE pipeline runs a SEPARATE
  deployed copy at `~/.local/state/signal/app/signalpipe` (the only config the
  worker reads is `~/.local/state/signal/app/config/signal.json`, reloaded per
  job). Editing the repo does NOT change live behavior.

## CSS gotchas learned
- `.wrap` sets the page gutter via `padding: 0 var(--gutter)`. Any element that
  ALSO carries `.wrap` and sets its own `padding` shorthand (e.g. `.foot`) will
  clobber that horizontal gutter — use `padding-top`/`padding-bottom` longhands on
  those so the gutter survives. (This was the mobile footer left-clip bug.)
- `align-items: baseline` on the masthead bottom-anchors a multi-line kicker to
  the big wordmark's baseline and reads as "floating low"; `center` balances it.
- Watch for dead responsive rules: `flex-direction` on a `display:grid` element is
  a no-op, and `.foot__links` didn't exist (the class is `.foot__col`).

## Pipeline / cost gotchas learned
- `novelty` (the lead standfirst) is unconstrained end-to-end (schema, backends,
  render). One verbose judge output pollutes the site lead + the write prompt +
  the digest prompt. Clamp at persistence (`curate.py`) — the single choke point.
- `MAX_JUDGE_CHARS` and `effort=` are documented optimizations that were never
  wired into `curate.py`; the triage/judge calls run the full article at default
  effort. Big, quality-neutral cost wins.
- `config_fingerprint()` hashes whole config blocks, so editing a DEAD knob
  (`stop_curate_on_cap`, `escalate_spread`, `digests.*.cron`) forks the
  run-attribution version with zero behavior change — remove dead knobs.
- Model IDs valid as of 2026-07: `claude-fable-5`, `claude-opus-4-8`,
  `claude-sonnet-5`, `claude-sonnet-4-6` (prev-gen, still served),
  `claude-haiku-4-5-20251001`. `backend_api.PRICING` must have an entry for any
  model used or `_cost` falls back to (5.0, 25.0) and overcharges the ledger.

## Self-learning layer (2026-07-18)
- Four modules, all repo-side + opt-in: `eval.py` (gold corpus + judge replay),
  `adaptive.py` (percentile bar), `momentum.py` (`kb/momentum.json` + multiplier),
  `library.py` (`/library/` entity wiki). With `funnel.adaptive.enabled=false` and
  `momentum.enabled=false` (the defaults), selection + scoring are byte-identical
  to before — the full pipeline suite proves it. Enabling either is a staged
  config flip (reloads per job); watch a few cycles first.
- The dashboard server is **read-only by design** — there is NO human review
  signal in the DB. Eval gold is therefore built repo-side from the DB read-only
  (`published_ledger` = "featured") + committed artifacts, never via server writes.
- `signal eval run` defaults to the **local** backend so nightly eval is $0. Only
  `--backend api|subscription` spends.
- New modules decouple from `Config`: they read plain config dicts
  (`momentum.config(cfg)`, `cfg.funnel.get("adaptive", {})`) so tests pass dicts,
  not a full Config. Cores take `now`/`date` as parameters (3.9-safe, deterministic).
- `PIPELINE_OWNED` (publish.py) now includes `src/content/library/`; the worker's
  dirt-guard treats Library pages as owned. New worker jobs: `momentum` (daily),
  `library` (daily), beside `kb_trends` (weekly). Adding a worker job means
  updating `test_worker.py`'s expected job-id set.
- Library v1 is **non-person entities only** and **deterministic** (no LLM) — the
  page body copies curation `why_it_matters`. `apply_multiplier`/timeline links
  reuse `publish.no_archive` so no archive.* URL ever ships.

## Process gotchas
- Usage: heavy multi-agent workflows burn the shared session limit fast (a
  6-dimension review spent ~2.8M subagent tokens and tripped the limit). Prefer
  smaller, targeted fan-outs; do the surgical edits in the main loop.
- The working checkout is inside iCloud Drive; heavy git churn mints hundreds of
  `"name 2.ext"` conflict duplicates. They're safe to delete (originals == HEAD).
- CI: `pr.yml` gates the site only; the pipeline pytest suite gates deploy.yml on
  push. Pipeline PRs merge untested until that's fixed.
