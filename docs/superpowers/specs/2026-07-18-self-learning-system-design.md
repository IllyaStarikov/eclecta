# Self-learning layer for eclecta / signalpipe — design

Date: 2026-07-18
Status: approved (design), pre-implementation

## Goal

Make eclecta's curation get measurably better on its own over time, without ever
touching live services or the live DB from the improvement loop. Five parts, one
spine (the nightly self-improvement pass gains **instruments, memory, and a
rising bar**):

1. **Footer** — replace the unprofessional "Written by Claude models" label.
2. **Eval sets** — a versioned, growing gold corpus + a way to score the current
   judge against it. Measurement + regression detection.
3. **Adaptive featured bar** — selection thresholds that harden over time,
   bounded and opt-in.
4. **Topic momentum** — learn what matters now and what's emerging; nudge scoring.
5. **Library** — a growing world-knowledge wiki (internal KB, deepened) surfaced
   as a reader-facing `/library/` section.
6. **The loop** — the nightly pass, upgraded to use 2–5.

## Hard constraints (inherited, non-negotiable)

- **Repo-side only.** The improvement loop never starts/reloads launchd services,
  never edits `~/.local/state/signal/app`, never writes the live DB. It reads the
  DB read-only (as the dashboard does) and reads committed repo artifacts.
- **Green or revert.** Every landed change keeps `npm run build`, `npm test`, and
  `pytest` green. Pipeline CODE going live is a separate, human-approved
  `rsync + launchctl kickstart`.
- **Additive / opt-in.** New live-selection behavior (the adaptive bar, the
  momentum multiplier) ships **disabled by default**; the existing constants keep
  working until Illya flips a config knob.
- **Cost.** Nightly eval defaults to the **free local model** ($0). No new metered
  spend without an explicit `--backend` override.
- **Editorial/legal.** Model-written public pages carry the existing AI-content
  declaration; no `archive.*` links (reuse `publish.no_archive` / `_ARCHIVE_RE`);
  Library first cut is **non-person entities only** (company, model, technology,
  standard, event, project) to avoid biography-accuracy risk.

## Current-state anchors (from code map)

- Funnel gates are static constants: `score.finalists()` reads
  `funnel.min_score_to_curate` (3.5) + `funnel.daily_finalists`
  (`score.py:175-176`); the judge writes integer `curations.relevance_score`
  (`curate.py:166`); picks read `min_relevance_for_feed` (6) at
  `publish.export_picks` (`publish.py:106-122`); editions read
  `digests.<kind>.min_relevance` at `digest._gather` (`digest.py:185-186,88`);
  live feed at `feed.py:134-139`. **No adaptivity anywhere.**
- `runs` + `config_versions` + `db.record_run` (`db.py:205-213,514-540`) already
  record a trailing outcome series tagged by config fingerprint.
- Dashboard server is **pure read-only** — no human review signal is captured
  anywhere. Eval labels must therefore be built repo-side, not via the server.
- Topics: fixed `TAXONOMY` + free-form LLM `channels[]`; `topic_match` is a static
  0/0.7/1.0 title-lexicon hit (`score.py:108-124`). No per-topic importance or
  momentum. `CHANNEL_TO_CATEGORY` (`topics.py:202-211`) maps a title match → a
  taxonomy category at score time.
- KB: `kb/days/*.md` (deterministic) + `kb/trends.md` (Sonnet-maintained,
  changelog managed in code), builders in `kb.py`, published via
  `publish.publish_trends` on `kb_trends_cron` (Fri 07:45). **Not rendered on the
  site.** Worker's allowed-write set is `src/data | src/content/digests | kb`.

---

## Part 0 — Footer (trivial, ship first)

- `src/layouts/Base.astro:228`: link text `Written by Claude models` → `Colophon`
  (href unchanged: `/about/#how-it-works`).
- **Keep** the machine-readable declaration: JSON-LD `creditText` (`Base.astro:81`)
  and `<meta name="ai-content-declaration">` (`Base.astro:107`) — Art. 50.
- Update the footer-text assertion in `tests/e2e/` that expects the old string.

**Acceptance:** footer reads Colophon; head declaration intact; e2e green.

---

## Part 1 — Eval sets

### Layout (repo, versioned)
```
eval/
  README.md
  gold/curation.jsonl        # one labeled example per line
  results/YYYY-MM-DD.json    # metrics per eval run
```

### Gold example schema (one JSON object per line)
```json
{
  "id": "<story_id or synthetic>",
  "title": "...", "source": "...", "url": "...",
  "excerpt": "...",                       // same short excerpt the judge sees
  "human": {"featured": true, "relevance": 8, "category": "ai", "reason": "..."},
  "provenance": "edition:daily/2026-07-17 | kb-day:2026-07-12:top-uncurated | skipped",
  "labeled_by": "seed | nightly | illya",
  "labeled_at": "2026-07-18",
  "confidence": "provisional | confirmed"
}
```

### Module `signalpipe/eval.py` + CLI `python3 -m signalpipe eval …`
- `build_candidates(repo_root, conn=None)` → provisional examples sourced from
  **committed artifacts** (never the live server):
  - **positives** — story_ids present in `src/content/digests/**` editions;
  - **hard negatives** — `kb/days/*.md` "Top uncurated" entries with a decent
    score that never appear in any edition;
  - **negatives** — skipped curations (read-only DB or a committed snapshot).
- `run(gold, cfg, backend="local")` — for each example, rebuild the judge prompt
  with the **exact live construction** (`curate._build_prompt` + `schemas.SYSTEM_JUDGE`
  + `JUDGE_SCHEMA`), call `adapter.complete("judge", …)` on the chosen backend
  (default `local`, $0; respects spend caps), compare to the human label.
- `score_predictions(preds, golds)` → pure metrics dict:
  `{n, agreement_featured, relevance_mae, featured_precision, featured_recall,
    category_accuracy, model_used, backend, cost_usd, judge_prompt_hash}`.
- `grow(gold, candidates, k)` — add ≤k new provisional examples not already present
  (dedup by `id`).
- `label(gold, id, human=…)` — upsert a `confidence:"confirmed"` label (Illya's
  corrections). Repo-side write.
- CLI subcommands in `__main__.py`: `eval run|grow|label|report`, writing
  `eval/results/`.

### Tests `pipeline/tests/test_eval.py`
- `score_predictions` math on fixtures (precision/recall/MAE/agreement).
- `grow` dedup; `build_candidates` from a fixture repo tree; `run` against a stub
  backend; `label` upsert.

**Acceptance:** `eval run` produces a metrics file from a fixture gold set on the
local backend at $0; metrics are correct; pytest green.

---

## Part 2 — Adaptive featured bar (opt-in, default OFF)

### Module `signalpipe/adaptive.py` (pure, testable)
- `percentile(values, p)` — linear-interpolation percentile.
- `ramped_percentile(now, cfg)` → `p_start + (p_end-p_start) *
  clamp((now - ramp_start)/ramp_days, 0, 1)` — the target rises over time.
- `effective_min_score(conn, cfg, now, prev=None)` → float: percentile of
  `clusters.score` over `window_hours`, clamped `[score_floor, score_ceiling]`,
  step-limited by `max_daily_step` vs `prev` (prev read read-only from the last
  `runs.stats`; skipped if unavailable). Falls back to the static constant if the
  window is empty (**never starves**).
- `effective_min_relevance(conn, cfg, now, prev=None)` → int, same over
  `curations.relevance_score`, clamped `[relevance_floor, relevance_ceiling]`.

### Config `funnel.adaptive` (added to `signal.example.json`, default disabled)
```json
"adaptive": {
  "enabled": false,
  "window_hours": 336,
  "percentile_start": 50, "percentile_end": 70,
  "ramp_start": "2026-08-01", "ramp_days": 120,
  "score_floor": 3.5, "score_ceiling": 7.0,
  "relevance_floor": 6, "relevance_ceiling": 8,
  "max_daily_step": 0.5
}
```

### Integration (each guarded by `enabled`; else unchanged constant)
- `score.finalists()` (`score.py:175`) — `min_score`.
- `publish.export_picks` (`publish.py:106`) — `min_rel`.
- `digest._gather` (`digest.py:185`) — `min_relevance` per kind (uses the kind's
  configured `min_relevance` as the floor).
- `feed.py:134` — `min_relevance` (query `?min_relevance=` override still wins).
- Record `effective_min_score` / `effective_min_relevance` into `runs.stats` at the
  curate/publish record sites so the ratchet is observable.

### Tests `pipeline/tests/test_adaptive.py`
- percentile correctness; clamp to floor/ceiling; ramp over mocked `now`;
  empty-window → constant fallback; `enabled:false` → constants unchanged;
  `max_daily_step` limits movement.

**Acceptance:** with `enabled:false`, selection is byte-identical to today; with
`enabled:true` the effective bar sits within `[floor, ceiling]`, rises with the
ramp, never starves an empty window; pytest green.

---

## Part 3 — Topic momentum ("what matters now & next")

### Module `signalpipe/momentum.py` (deterministic, zero-LLM)
- `compute(conn, cfg, now)` → per-category dict:
  `{volume_recent, volume_baseline, featured_rate, momentum, trend:
    "rising|steady|fading", emerging: bool}`. Aggregates `curations.category`
  (via `topics.match_taxonomy` when category is null) over `clusters.first_seen`;
  `momentum = recent_rate / max(baseline_rate, eps)`; `emerging` when baseline≈0
  and `volume_recent >= emerging_min_recent`.
- `importance_multipliers(momentum, cfg)` → `{category: m}` with
  `m ∈ [multiplier_min, multiplier_max]` (monotone in momentum, clamped).
- Builder `momentum_artifact(conn, cfg, now)` → `("kb/momentum.json", json)` for
  `publish` to commit (KB builder pattern; `kb/` is already an allowed path).

### Injection at score time (opt-in)
- In `score.py` topic contribution (`score.py:124`): map the title match →
  category via `CHANNEL_TO_CATEGORY`, multiply the `topic_match` contribution by
  that category's multiplier from `kb/momentum.json` (loaded once per run). When
  `momentum.enabled:false` or the file is absent → multiplier 1.0 (**no-op**).

### Config `momentum` (default disabled)
```json
"momentum": {
  "enabled": false, "recent_hours": 168, "baseline_hours": 720,
  "multiplier_min": 0.85, "multiplier_max": 1.25, "emerging_min_recent": 3
}
```

### Cadence
- New worker job `momentum` (deterministic, cheap) on a daily interval →
  `publish.publish_momentum(cfg)` writes `kb/momentum.json`.

### Tests `pipeline/tests/test_momentum.py`
- momentum math on a fixture DB; multiplier clamp + monotonicity; emerging
  detection; disabled/missing → no-op multiplier of 1.0.

**Acceptance:** `kb/momentum.json` is produced deterministically; multipliers are
clamped; with `enabled:false` scores are unchanged; pytest green.

---

## Part 4 — Library (world-knowledge wiki + reader-facing section)

### Internal KB (extends `kb/`)
- `kb/library/registry.json` — `{slug, name, type, aliases[]}` per tracked entity;
  types restricted to non-person: `company|model|technology|standard|event|project`.
- `kb/library/<slug>.md` — dense, sourced, **dated timeline** of what happened,
  built from editions + recent curations mentioning the entity; changelog managed
  deterministically in code; no `archive.*` links; model-maintained (deep tier),
  like `trends.md`.
- `kb/library/index.json` — `{slug, name, type, updated, coverage}` for the site.
- Module `signalpipe/library.py`:
  - `propose_entities(conn, cfg, k)` — ≤k new non-person entities from recent
    high-signal curations (frequency of channel/category + recurring proper-noun
    candidates), appended to the registry (provisional).
  - `refresh(conn, cfg, k)` — (re)generate ≤k entity pages that have **fresh
    activity**; returns `[(relpath, content)]` for publish to commit. Deterministic
    guards (archive scrub, changelog in code).
- Cadence: fold into the existing `kb_trends`/a daily KB job; **≤3 entities/run**.

### Reader-facing site
- New content collection `library` in `src/content.config.ts`:
  `schema { name, slug, type, summary, updated: date, coverage?: number }`.
- Publish exports the **reader-safe** slice of `kb/library/` into
  `src/content/library/*.md` (scrubbed). Extend the worker's allowed-write set
  (`_clean_pipeline_dirt`) and publish paths to include `src/content/library`.
- Pages:
  - `src/pages/library/index.astro` — the world-knowledge map (entities grouped by
    type, most-recently-updated first) + "What changed lately" (from changelogs) +
    the durable trendlines. **Graceful empty-state** when the corpus is small.
  - `src/pages/library/[...slug].astro` — an entity page rendered from its markdown,
    with its timeline and links to the editions that covered it.
- Nav + footer: add **Library** to the header nav and the footer "Read" column.
- SEO: `/library/**` in the sitemap; OG + JSON-LD (`CollectionPage` for the index,
  `DefinedTerm`/`Article` for entities); category feed optional (out of scope v1).

### Tests
- unit: `library` collection schema; `library.py` builders (archive scrub,
  changelog, ≤k limit) in `pipeline/tests/test_library.py`.
- e2e: `/library/` renders + lists entities; an entity page renders its timeline;
  empty-state; no `archive.*` links; **Library present in nav + footer**; sitemap
  includes a library URL.

**Acceptance:** `/library/` and one entity page build and render (with a seeded
entity), degrade gracefully when empty, carry no archive links, appear in nav +
sitemap; unit + e2e green.

---

## Part 5 — The loop (nightly pass, upgraded)

Extend `ops/self-improve.md` (and the `runat` `eclecta-nightly` task instruction):

1. `signal eval run` (local backend) → write `eval/results/`; compare to the prior
   run; **flag a regression** if the current judge dropped vs. gold.
2. `signal eval grow` → add a few new provisional gold candidates from committed
   artifacts.
3. Read `kb/momentum.json` → note rising/emerging categories in the journal; a
   surging category may justify a topic-importance nudge (**propose, `[?]`**).
4. `signal … library refresh` → refresh ≤3 entity pages; commit.
5. Sanity-check the adaptive bar (if enabled): read `runs.stats` effective values;
   if the floor is pinned constantly, **flag for Illya** (bar too high / supply too
   low).
6. Everything green-or-revert, repo-only. Risky knobs (raising percentile targets,
   enabling adaptive/momentum live, model routing) stay `[?]` — propose, never
   decide. Record learnings in `ops/LEARNINGS.md`; move items in `IMPROVEMENTS.md`.

The eval metric is the north-star the loop optimizes; the adaptive bar makes
"featured gets harder" real and observable; momentum + Library give the loop
foresight and memory.

---

## Sequencing (all in this pass; each phase commits green)

- **A** — Part 0 (footer) + Part 1 (eval) + Part 3 artifact (momentum.json,
  injection default-off). Additive, ~$0.
- **B** — Part 2 (adaptive bar, default OFF) + wire the momentum multiplier
  (default OFF). Pipeline code → normal deploy when Illya says go.
- **C** — Part 4 (Library reader-facing) + Part 5 (loop/runbook wiring).

## Out of scope (v1, noted for later)

- Person entities in the Library (biography accuracy / BLP risk).
- Wiring the Library back into novelty grounding at judge time.
- Adding write routes to the dashboard server for human labels (kept read-only;
  labels are repo-side via `eval label`).
- Per-category RSS feeds for the Library.
```
