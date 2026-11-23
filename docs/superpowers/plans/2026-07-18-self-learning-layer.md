# Self-Learning Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give eclecta/signalpipe a repo-side self-learning layer — eval sets, an adaptive "featured" bar, topic momentum, and a reader-facing Library — that lets the nightly pass measurably improve curation over time.

**Architecture:** Four new deterministic-or-guarded pipeline modules (`eval.py`, `adaptive.py`, `momentum.py`, `library.py`), each with pure-function cores and thin I/O shells; opt-in integration at the existing funnel gates and score contribution; a new Astro `library` content collection + pages; all driven by the existing nightly `runat` loop. Nothing touches live services or the live DB except read-only.

**Tech Stack:** Python 3.9-compatible (worker runtime) / 3.13 (tests) signalpipe; SQLite (WAL, read-only from the loop); Astro 5 + TS; Vitest (unit), Playwright (e2e), pytest (pipeline).

## Global Constraints

- Python code MUST parse and run on **3.9** (worker) and 3.13 (CI). No `match`, no `X | Y` unions in annotations, no `str.removeprefix` in hot paths without a guard. Use `typing.Optional`/`Dict`/`Tuple`.
- **Repo-side only.** No launchd start/stop/reload; no writes to `~/.local/state/signal/app`; no writes to the live DB. DB access is read-only.
- **Opt-in defaults.** `funnel.adaptive.enabled=false`, `momentum.enabled=false`. With both false, selection + scoring are byte-identical to today.
- **Cost.** `eval run` defaults to `backend="local"` ($0). No metered spend without explicit `--backend`.
- **No archive links** in any generated content: reuse `publish.no_archive` / `publish._ARCHIVE_RE`.
- **Library v1 = non-person entities only:** `company|model|technology|standard|event|project`.
- Model IDs valid 2026-07: `claude-fable-5`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`.
- Every landed task keeps `npm run build`, `npm test` (vitest), `npx playwright test`, and `pytest` green for the areas it touches. Commit per task; push per phase.

---

## File Structure

**New pipeline modules**
- `pipeline/signalpipe/eval.py` — gold-set build/grow/label + judge scoring + metrics.
- `pipeline/signalpipe/adaptive.py` — percentile/ramp/clamp pure fns + effective-threshold readers.
- `pipeline/signalpipe/momentum.py` — per-category momentum + clamped importance multipliers + artifact builder.
- `pipeline/signalpipe/library.py` — entity registry + per-entity wiki builders (guarded, ≤k/run).

**New tests**
- `pipeline/tests/test_eval.py`, `test_adaptive.py`, `test_momentum.py`, `test_library.py`.

**Modified pipeline**
- `pipeline/signalpipe/__main__.py` — `eval`, `momentum`, `library` subcommands.
- `pipeline/signalpipe/config.py` — parse `funnel.adaptive`, `momentum` blocks (+ fingerprint).
- `pipeline/signalpipe/score.py:175,124` — adaptive min_score + momentum multiplier (guarded).
- `pipeline/signalpipe/publish.py:106` (+ new `publish_momentum`, `publish_library`, export to `src/content/library`) — adaptive min_rel + KB/library publish + allowed-write set.
- `pipeline/signalpipe/digest.py:185` — adaptive min_relevance (max with kind floor).
- `pipeline/signalpipe/feed.py:134` — adaptive min_relevance (query override still wins).
- `pipeline/signalpipe/curate.py:334` — record effective thresholds into `runs.stats`.
- `pipeline/signalpipe/worker.py` — `momentum` (daily) + `library` (daily) jobs; extend allowed-write set to `src/content/library`.
- `pipeline/config/signal.example.json` — `funnel.adaptive`, `momentum` blocks.

**New repo data**
- `eval/README.md`, `eval/gold/curation.jsonl` (seeded), `eval/results/.gitkeep`.
- `kb/momentum.json` (generated), `kb/library/registry.json`, `kb/library/<slug>.md`, `kb/library/index.json` (generated).

**Modified site**
- `src/layouts/Base.astro:228` — "Colophon"; nav + footer "Library" link.
- `src/content.config.ts` — `library` collection.
- `src/pages/library/index.astro`, `src/pages/library/[...slug].astro` (new).
- `src/site.ts` — nav entry if nav is centralized there.

**Modified tests (site)**
- `tests/e2e/chrome.spec.ts` or footer spec — Colophon.
- `tests/e2e/library.spec.ts` (new), `tests/e2e/seo.spec.ts` — sitemap includes library.
- `tests/unit/*` — collection/nav as needed.

**Modified ops**
- `ops/self-improve.md`, the `runat` `eclecta-nightly` task instruction, `ops/IMPROVEMENTS.md`, `ops/LEARNINGS.md`.

---

# PHASE A — footer + eval + momentum artifact (additive, ~$0)

### Task A0: Footer → Colophon

**Files:** Modify `src/layouts/Base.astro:228`; Test `tests/e2e/chrome.spec.ts` (or wherever footer text is asserted).

- [ ] Grep the e2e suite for `Written by Claude models`; update the assertion to `Colophon`.
- [ ] Edit `Base.astro:228` link text `Written by Claude models` → `Colophon` (href unchanged). Leave `Base.astro:81` (`creditText`) and `:107` (`ai-content-declaration`) untouched.
- [ ] `npm run build` then `npx playwright test` (footer test) → PASS.
- [ ] Commit: `design(footer): 'Written by Claude models' → 'Colophon'; keep Art.50 machine declaration`.

### Task A1: Eval metrics core (pure functions, TDD)

**Files:** Create `pipeline/signalpipe/eval.py`; Test `pipeline/tests/test_eval.py`.

**Interfaces — Produces:**
- `score_predictions(preds: List[dict], golds: List[dict]) -> dict` where a pred is `{"id","relevance":int,"skip":bool,"category":str}` and a gold has `human={"featured":bool,"relevance":int,"category":str}`. Returns `{"n","agreement_featured","relevance_mae","featured_precision","featured_recall","category_accuracy"}`. "featured" prediction = `not skip and relevance >= featured_rel` (default 6, param).

- [ ] Write failing tests: perfect agreement → precision=recall=1.0, mae=0.0; a known confusion matrix → hand-computed precision/recall; MAE from `[|p-g|]/n`; category accuracy fraction; empty input → zeros with `n=0` (no ZeroDivision).
- [ ] Implement `score_predictions` with a `featured_rel=6` kwarg. Guard all divisions.
- [ ] `pytest pipeline/tests/test_eval.py -k metrics -v` → PASS.
- [ ] Commit: `feat(eval): pure prediction-scoring metrics`.

### Task A2: Eval gold I/O + candidate build + grow/label

**Files:** `pipeline/signalpipe/eval.py` (extend); `eval/README.md`, `eval/gold/curation.jsonl` (empty/seed), `eval/results/.gitkeep`; Test `test_eval.py`.

**Interfaces — Produces:**
- `load_gold(path) -> List[dict]`, `save_gold(path, rows)` (JSONL, stable key order).
- `build_candidates(repo_root, conn=None, limit=50) -> List[dict]` — provisional examples from committed artifacts: positives from `src/content/digests/**` (story ids/titles), hard-negatives from `kb/days/*.md` "Top uncurated", negatives from skipped curations if `conn` given (read-only). Each candidate carries `provenance`, `labeled_by:"seed"`, `confidence:"provisional"`, `human` filled from provenance (featured=True for editions, False for uncurated/skipped; relevance heuristic).
- `grow(gold, candidates, k) -> List[dict]` — append ≤k not already present (dedup by `id`).
- `label(gold, id, human) -> List[dict]` — upsert, set `confidence:"confirmed"`, `labeled_by:"illya"`.

- [ ] Write failing tests: `grow` dedups by id and respects k; `label` upserts + flips confidence; `build_candidates` over a tiny fixture repo tree (editions dir + a kb/days file) yields the expected provenance mix; JSONL round-trips.
- [ ] Implement. `build_candidates` parses digest frontmatter/markdown for titles+story ids; parses kb/days "## Top uncurated" bullets. No network, no live server.
- [ ] `pytest pipeline/tests/test_eval.py -v` → PASS.
- [ ] Write `eval/README.md` (schema, how to `eval label`, that nightly grows it, local-backend default). Seed `eval/gold/curation.jsonl` via `build_candidates` over the current repo (commit a modest seeded set).
- [ ] Commit: `feat(eval): gold-set I/O, candidate build from committed artifacts, grow/label`.

### Task A3: Eval run (judge replay) + CLI

**Files:** `pipeline/signalpipe/eval.py` (extend); `pipeline/signalpipe/__main__.py`; Test `test_eval.py`.

**Interfaces — Consumes:** `curate._build_prompt`, `curate.MAX_JUDGE_CHARS`, `schemas.SYSTEM_JUDGE`, `schemas.JUDGE_SCHEMA`, `llm.adapter.complete`. **Produces:** `run(gold, cfg, backend="local", conn=None) -> dict` → predictions + metrics + `{model_used,backend,cost_usd,judge_prompt_hash}`, and writes `eval/results/<date>.json` (date passed in, not `Date.now()`).

- [ ] Write failing test: `run` against a **stub adapter** (monkeypatched `adapter.complete` returning canned judge JSON) produces predictions aligned to gold and a metrics dict; asserts backend defaulting to local; asserts no metered call when backend=local.
- [ ] Implement `run`: rebuild each example's judge prompt exactly as curate does from `excerpt`, call `adapter.complete("judge", SYSTEM_JUDGE, prompt, JUDGE_SCHEMA, cfg=cfg, backend_override=backend, ...)`, collect predictions, call `score_predictions`, assemble metrics. `judge_prompt_hash = sha256(SYSTEM_JUDGE)[:12]`.
- [ ] Add `__main__.py` subcommands: `eval run [--backend local|api|subscription] [--date YYYY-MM-DD]`, `eval grow [-k N]`, `eval label --id .. --featured .. --relevance ..`, `eval report`.
- [ ] `pytest pipeline/tests/test_eval.py -v` → PASS; `python3 -m signalpipe eval report` smoke.
- [ ] Commit: `feat(eval): judge-replay run + CLI (local backend default, $0)`.

### Task A4: Momentum core + artifact (deterministic, TDD)

**Files:** Create `pipeline/signalpipe/momentum.py`; Test `pipeline/tests/test_momentum.py`.

**Interfaces — Produces:**
- `compute(conn, cfg, now) -> Dict[str, dict]` per category:
  `{"volume_recent","volume_baseline","featured_rate","momentum","trend","emerging"}`.
  `momentum = volume_recent_rate / max(volume_baseline_rate, eps)`; rates are counts normalized by window length; `trend`: rising `>1.15`, fading `<0.85`, else steady; `emerging`: `volume_baseline_rate < eps2 and volume_recent >= emerging_min_recent`.
  Category via stored `curations.category` else `topics.match_taxonomy(title, channels)`.
- `importance_multipliers(mom, cfg) -> Dict[str, float]` — clamp to `[multiplier_min, multiplier_max]`, monotone in momentum (e.g. `clamp(0.85 + 0.4*log(momentum+1)/log(3), lo, hi)` — pick a monotone map, test monotonicity).
- `momentum_artifact(conn, cfg, now) -> Tuple[str, str]` → `("kb/momentum.json", json)` with `{generated_for: <date str>, categories: {...}, multipliers: {...}}`.

- [ ] Write failing tests on a fixture DB (insert clusters+curations across two windows): a category with more recent volume → momentum>1, trend rising; a new category with only recent items → emerging True; multipliers clamped + monotone; empty DB → `{}` and multipliers all-absent (→ no-op). `now` injected.
- [ ] Implement (zero LLM). Use `clusters.first_seen` as the time axis; join curations for category/featured.
- [ ] `pytest pipeline/tests/test_momentum.py -v` → PASS.
- [ ] Commit: `feat(momentum): deterministic per-category momentum + clamped multipliers + kb/momentum.json`.

### Task A5: Config parsing for momentum (+ publish hook, worker job) — artifact only, injection still off

**Files:** `config.py`, `publish.py` (`publish_momentum`), `worker.py` (daily `momentum` job), `signal.example.json`; Test `test_config.py`, `test_momentum.py`.

**Interfaces — Produces:** `cfg.momentum` dict with defaults `{enabled:false, recent_hours:168, baseline_hours:720, multiplier_min:0.85, multiplier_max:1.25, emerging_min_recent:3}`; `publish.publish_momentum(cfg)` writes `kb/momentum.json` via the existing kb builder/commit path.

- [ ] Test: `config.load` exposes `momentum` with defaults when absent; fingerprint includes it.
- [ ] Add `momentum` block to `signal.example.json` (enabled:false). Parse in `config.py`; add to `config_fingerprint` tunables.
- [ ] `publish_momentum(cfg)`: open read-only conn, `momentum.momentum_artifact(...)`, write+commit like `publish_trends`. Add `momentum` worker job (daily interval) calling it.
- [ ] `pytest pipeline/tests/test_config.py pipeline/tests/test_momentum.py -v` → PASS.
- [ ] Commit: `feat(momentum): config block + daily publish job (artifact only; injection off)`.

**Phase A gate:** `pytest` green; `npm run build && npm test && npx playwright test` green; commit + `git push`. Report.

---

# PHASE B — adaptive bar (default OFF) + momentum multiplier

### Task B1: Adaptive pure functions (TDD)

**Files:** Create `pipeline/signalpipe/adaptive.py`; Test `pipeline/tests/test_adaptive.py`.

**Interfaces — Produces:**
- `percentile(values: List[float], p: float) -> float` (linear interpolation; empty → raises/None sentinel handled by callers).
- `ramped_percentile(now, cfg_adaptive) -> float` = `p_start + (p_end-p_start)*clamp((now-ramp_start).days/ramp_days,0,1)`.
- `clamp(x, lo, hi) -> float`.

- [ ] Failing tests: `percentile([1..10], 50)` ≈ 5.5; p0→min, p100→max; `ramped_percentile` at ramp_start→p_start, after ramp_days→p_end, midway→mean; clamp bounds. `now` injected as `datetime`.
- [ ] Implement (3.9-safe). Parse `ramp_start` ISO date.
- [ ] `pytest pipeline/tests/test_adaptive.py -k "percentile or ramp or clamp" -v` → PASS.
- [ ] Commit: `feat(adaptive): percentile/ramp/clamp primitives`.

### Task B2: Effective-threshold readers (TDD)

**Files:** `adaptive.py` (extend); Test `test_adaptive.py`.

**Interfaces — Produces:**
- `effective_min_score(conn, cfg, now, prev=None) -> float` — percentile of `clusters.score` over `window_hours`, clamped `[score_floor, score_ceiling]`, `max_daily_step` vs `prev` if given; **empty window → `cfg.funnel.min_score_to_curate` constant** (never starves). Returns the constant when `not cfg.funnel.adaptive.enabled`.
- `effective_min_relevance(conn, cfg, now, prev=None) -> int` — same over `curations.relevance_score`, clamped `[relevance_floor, relevance_ceiling]`, rounded to int; disabled → the caller's configured constant (passed as `base`).

- [ ] Failing tests on a fixture DB: enabled=false → returns base constant unchanged; enabled=true → value within [floor,ceiling]; empty window → falls back to constant; `max_daily_step` limits jump from `prev`; ramp raises the value as `now` advances.
- [ ] Implement. Read scores read-only; use `db.connect(..., read_only=True)` pattern if present, else a normal conn (tests pass a conn).
- [ ] `pytest pipeline/tests/test_adaptive.py -v` → PASS.
- [ ] Commit: `feat(adaptive): effective_min_score/relevance readers (opt-in, floor-guarded)`.

### Task B3: Config block + wiring at the four gates + runs.stats

**Files:** `config.py`, `signal.example.json`, `score.py:175`, `publish.py:106`, `digest.py:185`, `feed.py:134`, `curate.py:334`; Test `test_config.py`, `test_adaptive.py`, and the existing `test_curate.py`/`test_digest.py` (assert unchanged when disabled).

**Interfaces — Consumes:** B2 readers. **Produces:** `cfg.funnel.adaptive` with the spec defaults (enabled:false).

- [ ] Test: with `adaptive.enabled=false`, `score.finalists`, `export_picks`, `digest._gather`, `feed` produce identical thresholds to today (regression guard).
- [ ] Add `funnel.adaptive` block to `signal.example.json`; parse in `config.py` (+fingerprint).
- [ ] Wire each gate: `min_score = adaptive.effective_min_score(conn,cfg,now)` else constant; `min_rel = adaptive.effective_min_relevance(conn,cfg,now, base=<existing default>)`. Digest uses `max(kind_min, effective)`. Feed keeps `?min_relevance=` override precedence.
- [ ] Record `effective_min_score`/`effective_min_relevance` into `runs.stats` at `curate.py:334`.
- [ ] `pytest pipeline/tests -k "adaptive or curate or digest or config or feed or publish" -v` → PASS.
- [ ] Commit: `feat(adaptive): opt-in wiring at funnel gates + runs.stats attribution (default OFF)`.

### Task B4: Wire the momentum multiplier at score time (opt-in)

**Files:** `score.py:124` (+ load `kb/momentum.json` once/run), `momentum.py` (a `load_multipliers(repo_root)` helper); Test `test_score.py` (or `test_momentum.py`).

**Interfaces — Consumes:** `kb/momentum.json` multipliers; `topics.CHANNEL_TO_CATEGORY`. **Produces:** score topic term × multiplier(category), default 1.0.

- [ ] Test: with `momentum.enabled=false` or file absent → scores identical to today; with a multiplier map + enabled → the topic contribution scales for a matching category, clamped.
- [ ] Implement: derive category from title match → `CHANNEL_TO_CATEGORY`; multiply `w.topic_match*topic` by the category multiplier (1.0 fallback).
- [ ] `pytest pipeline/tests -k "score or momentum" -v` → PASS.
- [ ] Commit: `feat(momentum): opt-in topic_match multiplier at score time (default OFF)`.

**Phase B gate:** full `pytest` green; both opt-ins verified OFF-by-default identical; `npm` suites green (no site change, but build to be safe); commit + push. Report.

---

# PHASE C — Library (reader-facing) + nightly loop wiring

### Task C1: Library registry + entity page builder (guarded, TDD)

**Files:** Create `pipeline/signalpipe/library.py`; `kb/library/registry.json` (seed a few non-person entities); Test `pipeline/tests/test_library.py`.

**Interfaces — Produces:**
- `load_registry(repo_root) -> List[dict]` / `save_registry`. Entity: `{slug,name,type,aliases}`.
- `propose_entities(conn, cfg, k, existing) -> List[dict]` — ≤k new non-person candidates from recent curations (frequency of category/channel + recurring capitalized tokens filtered to the allowed types via a small keyword map). Provisional.
- `refresh(conn, cfg, k, now) -> List[Tuple[str,str]]` — for ≤k entities with fresh activity, build `("kb/library/<slug>.md", content)` (deterministic scaffold: title, summary, `## Timeline` dated bullets from editions/curations mentioning the entity, links; `no_archive` scrub; changelog marker managed in code) + rebuild `("kb/library/index.json", json)`.

- [ ] Failing tests: `refresh` respects k and only fresh entities; output contains no `archive.` links (assert against `_ARCHIVE_RE`); index.json lists built entities with `updated`; `propose_entities` never proposes a person-type; registry round-trips.
- [ ] Implement. The entity page content in v1 is **deterministic** (no LLM) to keep it testable and $0; a later task can swap the body to a deep-tier rewrite behind the spend cap. Timeline pulled from editions (`published_ledger`/digests) + curations whose title/channels match the entity name/aliases.
- [ ] `pytest pipeline/tests/test_library.py -v` → PASS.
- [ ] Commit: `feat(library): entity registry + deterministic per-entity wiki builder (≤k/run, guarded)`.

### Task C2: Publish library → src/content + allowed-write set + worker job

**Files:** `publish.py` (`publish_library`, export reader-safe md to `src/content/library/`), `worker.py` (daily `library` job; extend allowed-write set to `src/content/library`), `__main__.py` (`library refresh|propose`); Test `test_publish.py`, `test_worker.py`.

- [ ] Test: `publish_library` writes `src/content/library/<slug>.md` with frontmatter `{name,slug,type,summary,updated}`; the worker's dirt-cleaner treats `src/content/library` as allowed (not foreign).
- [ ] Implement: build via `library.refresh`, write kb/ + a scrubbed site copy with frontmatter, commit through the existing publish/git path. Add worker `library` daily job. Add `src/content/library` to the allowed-write set in `worker.py`.
- [ ] `pytest pipeline/tests -k "publish or worker or library" -v` → PASS.
- [ ] Commit: `feat(library): publish to src/content/library + daily worker job + allowed-write set`.

### Task C3: Astro `library` collection + pages + nav/footer

**Files:** `src/content.config.ts` (add `library`), `src/pages/library/index.astro`, `src/pages/library/[...slug].astro`, `src/layouts/Base.astro` (nav + footer "Library"), `src/site.ts` if nav is centralized; seed `src/content/library/<slug>.md` so pages build.

**Interfaces — Consumes:** the `library` collection frontmatter from C2.

- [ ] Add collection: `schema z.object({name,slug,type:z.enum([...]),summary,updated:z.coerce.date(),coverage:z.number().optional()})`.
- [ ] `index.astro`: entities grouped by type, newest-updated first; "What changed lately" from changelogs/updated dates; graceful empty-state (message + link to /about) when the collection is empty.
- [ ] `[...slug].astro`: render entity md; show timeline; canonical/OG/JSON-LD; AI declaration inherited from Base.
- [ ] Add "Library" to the header nav and footer "Read" column.
- [ ] `npm run build` → PASS with ≥1 seeded entity and with the collection empty (empty-state path).
- [ ] Commit: `feat(site): /library section (index + entity pages) + nav/footer`.

### Task C4: Library e2e + SEO + unit

**Files:** `tests/e2e/library.spec.ts` (new), `tests/e2e/seo.spec.ts` (sitemap includes a library URL), unit as needed.

- [ ] e2e: `/library/` renders + lists a seeded entity; entity page renders its timeline; empty-state path; **no `archive.` links**; **Library in nav + footer**.
- [ ] seo: sitemap contains a `/library/` URL; entity page has canonical + og:type.
- [ ] `npx playwright test tests/e2e/library.spec.ts tests/e2e/seo.spec.ts` → PASS.
- [ ] Commit: `test(library): e2e + sitemap/SEO coverage`.

### Task C5: Nightly loop wiring + docs

**Files:** `ops/self-improve.md`, the `runat` `eclecta-nightly` task instruction (via `schedule.py` — update the task text), `docs/operating-runbook.md` (document the new jobs + `signal eval|momentum|library` commands), `ops/IMPROVEMENTS.md`, `ops/LEARNINGS.md`.

- [ ] Extend `ops/self-improve.md` "the pass, in order" with: run `signal eval run` (local) + record/compare + regression-flag; `signal eval grow`; read `kb/momentum.json` + note rising/emerging; `signal library refresh` (≤3); adaptive-bar sanity check (if enabled); risky knobs `[?]`.
- [ ] Update the `eclecta-nightly` queued task instruction to include these steps (so a context-free future pass performs them).
- [ ] Document new worker jobs + CLI in `docs/operating-runbook.md`; add durable notes to `LEARNINGS.md`; mark items in `IMPROVEMENTS.md`.
- [ ] Commit: `docs(ops): wire eval/momentum/library into the nightly self-improvement loop`.

**Phase C gate:** full `pytest` + `npm run build && npm test && npx playwright test` green; commit + push; open/refresh the PR. Final report.

---

## Self-Review

- **Spec coverage:** Part 0→A0; Part 1→A1-A3; Part 2→B1-B3; Part 3→A4-A5,B4; Part 4→C1-C4; Part 5→C5. All covered.
- **Placeholders:** algorithmic cores (metrics, momentum, percentile/ramp) specified in-line; mechanical wiring specified by exact file:line + behavior. No TBDs.
- **Type consistency:** `score_predictions`/`run`/`compute`/`importance_multipliers`/`effective_min_*`/`refresh` names + signatures are used consistently across tasks.
- **Determinism/time:** every core takes `now`/`date` as a parameter (no `Date.now()`/`datetime.now()` in cores) for 3.9-safety and testability.
```
