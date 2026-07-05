# Coverage dashboard v2 — design spec

Date: 2026-07-04
Status: approved pending user review
Owner: Illya Starikov

## Summary

Rebuild `/coverage/` as Eclecta's production transparency dashboard: a fully
static instrument panel that shows what the wire reads, what it keeps, and
which models did the work. `/stats/` merges into it and redirects. The page is
fed by an extended `stats.json` emitted by the pipeline; all new fields are
optional so old and new exports both build.

Decisions locked with the user:

1. **Scope: pipeline + site.** Extend `publish.py::export_stats` and rebuild
   the page on the richer export.
2. **Merge `/stats/` into `/coverage/`.** One dashboard URL; `/stats/`
   redirects.
3. **Approach A: static instrument panel.** Build-time inline SVG + CSS, no
   chart library, no client JS. Tooltips are native `title` attributes.

## Constraints

- Design language holds (docs/design-language.md): charts are CSS or inline
  SVG, never an image or a chart library; mono furniture; orange is a spice —
  one lead datum per band; sharp corners; light, dark, and print first-class.
- Privacy policy of the export holds (publish.py): **no spend dollars, no
  archive URLs, no health/error text** on the public site. Model names,
  backend kinds, and call counts are fine.
- The live worker runs the deployed pipeline copy at
  `~/.local/state/signal/app/signalpipe`, not the repo copy. Repo-side
  `publish.py` changes take effect only after the user syncs the deployed
  copy (his explicit go, per standing rule). Until then the next publish
  rewrites `stats.json` in the old thin shape; the page must degrade
  gracefully (bands hide, page never breaks).
- No services are started, reloaded, or kicked. The one-off data regeneration
  reads `~/.local/state/signal/signal.db` over a read-only (`mode=ro`) SQLite
  connection.
- The working tree carries another session's uncommitted title-separator
  edits (`—` → `|`) across 10 files. Do not revert or absorb them; stage only
  this project's hunks. `stats.astro` is deleted by this project, which
  retires its pending one-line edit.

## Data contract: `stats.json` extensions

All new blocks are **optional** in `src/lib/schema.ts::statsSchema`. The page
renders a band only when its block is present. Existing fields are unchanged.

| block | shape | source (DB) |
|---|---|---|
| `series_daily` | `[{d: "YYYY-MM-DD", items, clusters, curated}]`, last 90 days, contiguous (zero-fill missing days) | `items.ingested_at`, `clusters.first_seen`, `curations.curated_at` grouped by day |
| `funnel` | `{all_time: {items, clusters, fetched, curated, published}, last_30d: {…}}`; `published` = distinct `story_id` in `published_ledger` (windowed on `first_at`; story_id is NOT NULL where cluster_id may not be) | counts over `items`, `clusters`, `articles(fetch_status='ok')`, `curations(status='done', skip=0)`, `published_ledger` |
| `relevance_hist_30d` | `{kept: {"0"…"10": n}, skipped: {"0"…"10": n}}` | `curations.relevance_score` split on `skip` |
| `models_used_30d` | `[{scope: "curation"\|"digest", model, backend, count, avg_relevance}]`; `avg_relevance` and `backend` null where the DB doesn't record them (digests carry no backend column) | curations: `model_used/backend_used`, avg `relevance_score`; digests: `model_used` |
| `fetch_30d` | `{ok, paywalled, failed, skipped}`, windowed on `extracted_at` | `articles.fetch_status` |
| `top_sources_30d` | `[{name, items}]`, top 15 | `items.source_id → sources.name` |
| `echo_dist` | `{"1": n, "2": n, "3_5": n, "6_plus": n}` | `clusters.surface_count` |
| `rhythm_7x24` | `[[24 ints] × 7]`, Mon-first, UTC, last 30 days | `items.ingested_at` weekday × hour |

Notes:

- `top_sources_30d` (who feeds the wire, from `items`) is deliberately
  distinct from the existing `top_surfaces_7d` (where stories echo, from
  `surfaces`). Both appear on the page with distinct labels.
- `models_used_30d` is observed provenance; the existing `models` map stays
  as the *configured* routing. The page shows both. The DB records one
  `model_used` per curation and per digest (`tier_used` holds compound
  labels like `triage+judge+write`), so observed data is bucketed into two
  scopes — curation and digest — rather than pretending to a per-sub-stage
  split the DB doesn't hold. The three configured stages still render from
  the `models` map.
- The editions calendar needs no export: it builds from the `digests`
  content collection already in the repo.

### Writers

1. `pipeline/signalpipe/publish.py::export_stats` — extended in place; same
   privacy guarantees; queries lean on existing indexes
   (`idx_items_ingested`, `idx_cur_score`).
2. One-off scratchpad script (not committed) regenerates `stats.json` today
   from the live DB read-only, so the page ships with real data before the
   deployed pipeline is synced.

## Page design: `/coverage/`

A stack of rail-labelled bands (`▪ LABEL ── count`), narrative order. Every
chart is build-time inline SVG or the existing `.bars` CSS. One orange lead
datum per band. Bands render conditionally on data presence.

1. **Masthead + deck.** `Coverage` + as-of stamp; one-line deck. Unchanged
   masthead treatment (full masthead — this is a current-wire page).
2. **▪ THE WIRE.** Five stat cards (verified sources, items ingested,
   stories clustered, curated picks, digests published) with 7-day deltas
   beneath each where available (`items_7d`, `curated_7d`).
3. **▪ NINETY DAYS ON THE WIRE.** Full-width SVG column chart: one column
   per day of items ingested; curated picks per day as orange squares along
   the baseline. Native `title` per column. Mono caption: busiest day,
   quietest day, daily mean.
4. **▪ THE FUNNEL.** Five horizontal bars, items → clusters → fetched →
   curated → published, each with count + conversion percentage from the
   previous stage; final bar orange. Widths use a log scale (a linear scale
   renders every stage after items invisible); the log scale is stated in
   the caption. Caption lands the thesis: read everything, keep almost
   nothing (~0.7% all-time).
5. **▪ WHO FEEDS THE WIRE.** Category bars (kept from v1); tier split as one
   proportional stacked strip (flagship/core/niche); paywalled share of
   sources; `top_sources_30d` bars; `top_surfaces_7d` bars (kept, relabelled
   "where stories echo"); echo distribution strip (1 / 2 / 3–5 / 6+
   surfaces per story).
6. **▪ THE MODELS.** Configured routing as one card per stage (triage →
   deep read → digest, from `models`); beneath, the observed 30-day mix as
   proportional strips for the two recorded scopes (curation, digest) —
   model name, backend (local / subscription / api), call count, avg
   relevance for curations.
   Colophon line: curation runs on a MacBook; judgment is rented by the
   token.
7. **▪ THE BAR.** Relevance histogram 0–10, kept vs skipped in two tones
   (skipped = ground-2/faint, kept = ink, lead bucket orange), so the cut
   line is visible. Beside it, fetch outcomes strip (ok / paywalled /
   failed / skipped).
8. **▪ RHYTHM.** 7×24 UTC heatmap of ingest as a mono grid; 5 opacity steps
   of ink (never orange); weekday rows, hour columns; `title` per cell.
9. **▪ EDITIONS.** Calendar strip since launch from the digests collection:
   square per daily digest, wider marks for weekly/monthly/quarterly, each
   linking to its edition; latest edition called out with kind + title.
10. **Foot.** Methodology note (what a surface is, what clustering means,
    when figures regenerate), link to `/sources/`. The old "raw tables on
    /stats/" cross-reference is removed.

### `/stats/` retirement

- `src/pages/stats.astro` deleted.
- `redirects: { '/stats': '/coverage' }` in `astro.config.mjs` (static
  meta-refresh page).
- All internal `/stats/` references updated (grep across src/, tests/,
  docs/, scripts/).

## Architecture

- `src/lib/coverage.ts` (new): pure helpers, no Astro imports —
  `columnChart(series, …) → {columns: [{x, h, title}…], …}` geometry for the
  90-day SVG; funnel rows with log widths + percentages; histogram bins;
  heatmap opacity bucketing; editions-calendar model from collection
  entries; strip-chart segment math. Every helper unit-tested.
- `src/pages/coverage.astro`: thin — parse, call helpers, render markup.
- `src/styles/global.css`: new token-based classes (`.cols-chart`, `.strip`,
  `.heatmap`, `.editions-cal`, stat-card delta line) beside the kept
  `.bars`; light/dark/print covered; respects `prefers-reduced-motion`
  (no new motion introduced).
- `src/lib/schema.ts`: optional zod blocks exactly matching the table above.

## Error handling / degradation

- Missing optional block → band not rendered; page valid with the old thin
  `stats.json` (this is the state after the next live publish until the
  deployed pipeline is synced).
- Empty arrays / all-zero series → band hidden rather than rendering an
  empty chart.
- `parseStats` still throws on malformed data → build fails loudly (existing
  contract; unchanged).
- Divide-by-zero guards in funnel percentages and histogram scaling.

## Testing

- Unit: `coverage.ts` helpers (geometry, bins, buckets, calendar);
  `statsSchema` fixtures for BOTH old (thin) and new (rich) shapes; data
  files still validate.
- E2E: update anything referencing `/stats/`; coverage page smoke (bands
  present with rich fixture; page renders with thin fixture); redirect
  works in built output.
- Visual: `npm run capture`, eyeball light + dark + mobile; print check.
- Gate: `npm run check && npm run test:unit && npm run test:e2e` green.
- Pipeline: `export_stats` exercised against the read-only live DB during
  the one-off regeneration; output must round-trip `parseStats`.

## Rollout

1. Land site + pipeline changes in the repo (staged hunks only; do not
   touch the other session's pending edits).
2. Regenerate `stats.json` once from the live DB (read-only) so the page is
   rich immediately.
3. **User action:** sync the deployed pipeline copy at
   `~/.local/state/signal/app/signalpipe` so future publishes keep the rich
   shape (explicit go required; no service is touched before then).

## Out of scope

- Client-side JS on the page (approach B was declined).
- Spend/cost display of any kind (privacy policy).
- Per-call token counts and run durations (not persisted by the pipeline
  today; would need pipeline changes beyond `export_stats`).
- Nav changes: Coverage stays the footer's transparency link.
- The separate `/sources/` page (unchanged, still linked).
