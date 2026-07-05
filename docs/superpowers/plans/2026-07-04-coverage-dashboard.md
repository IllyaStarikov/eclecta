# Coverage Dashboard v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `/coverage/` as a static instrument-panel dashboard fed by an extended `stats.json` (90-day series, funnel, relevance histogram, observed model provenance, rhythm heatmap, editions calendar); `/stats/` merges in and redirects.

**Architecture:** The pipeline's `export_stats` (pipeline/signalpipe/publish.py) gains eight optional blocks; `src/lib/schema.ts` mirrors them as optional zod blocks so old and new exports both parse. A new pure-function module `src/lib/coverage.ts` turns stats into chart geometry; `src/pages/coverage.astro` renders ten rail-labelled bands as build-time inline SVG/CSS. No chart library, no client JS.

**Tech Stack:** Astro 5 (static, `build.format: 'directory'`), zod, vitest, Playwright, Python 3 stdlib (sqlite3) for the pipeline.

**Spec:** `docs/superpowers/specs/2026-07-04-coverage-dashboard-design.md`

## Global Constraints

- **Work in an isolated git worktree** branched from local `main` (create via superpowers:using-git-worktrees at execution start; suggested branch `feat/coverage-dashboard`). NEVER edit files in the main checkout — another Claude session is active there with uncommitted changes.
- Design language (docs/design-language.md): charts are CSS or inline SVG, never an image or chart library; sharp corners; no glyph separators (no `·` middots — spacing/register changes separate); orange (`var(--accent)`) on exactly ONE lead element per chart; all new CSS uses tokens, no raw hex; mono furniture uses only `--mono-xs/-sm/-lg` and `--track-meta/--track-cap`.
- Privacy (publish.py docstring): stats carry NO spend dollars, NO archive URLs, NO health/error text. Model names, backend kinds, call counts are allowed.
- All new `stats.json` fields are OPTIONAL in the zod schema; `coverage.astro` renders a band only when its block is present. `avg_relevance`-style nullables: keys are omitted or `null` per the schema below — never emit `undefined`.
- SQL time-window bounds are Python-side ISO strings via `_iso_days_ago()` passed as `?` parameters, NEVER `datetime('now')` in SQL (T-vs-space lexicographic hazard, publish.py:77-81).
- The one-off DB read uses `db.connect_ro` (`mode=ro` URI). No pipeline services are started, reloaded, or kicked.
- Em-dashes are banned in site copy (repo style rule 62) — use commas, colons, or periods in all new user-facing text.
- Internal links via `href('/path/')` with trailing slash.
- Commit prefixes: `feat:` / `test:` / `docs:` / `pipeline:`. Every commit ends with the Claude co-author trailer.

---

### Task 1: Worktree + green baseline

**Files:** none modified.

**Interfaces:**
- Produces: a worktree at `<WT>` (path chosen by the using-git-worktrees skill) on branch `feat/coverage-dashboard`, `npm ci` done, unit tests green. All later tasks run inside `<WT>`.

- [ ] **Step 1: Create the worktree** (superpowers:using-git-worktrees skill; branch from local `main`)

```bash
cd "/Users/starikov/Library/Mobile Documents/com~apple~CloudDocs/Documents/development/eclecta"
git worktree add "$TMPDIR/eclecta-coverage" -b feat/coverage-dashboard main
```

(If the skill picks a different location, use that path as `<WT>` throughout.)

- [ ] **Step 2: Install and baseline**

```bash
cd <WT> && npm ci && npm run check && npm run test:unit
```

Expected: `astro check` 0 errors; vitest all green (3 unit files). If baseline fails, STOP and report — do not fix unrelated breakage.

---

### Task 2: Zod schema extension for the v2 stats blocks

**Files:**
- Modify: `src/lib/schema.ts` (append inside `statsSchema`, plus new sub-schemas above it)
- Test: `tests/unit/schema.test.ts` (add describe block)

**Interfaces:**
- Consumes: existing `statsSchema`, `parseStats` (src/lib/schema.ts:72-116).
- Produces: `Stats` type now carries optional `series_daily`, `funnel`, `relevance_hist_30d`, `models_used_30d`, `fetch_30d`, `top_sources_30d`, `echo_dist`, `rhythm_7x24`. Exact shapes below — Tasks 6, 7, 9 rely on these key names verbatim.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/schema.test.ts`:

```ts
describe('stats v2 coverage blocks', () => {
  // Thin shape (current live pipeline export): must still parse.
  it('parses without any v2 block', () => {
    const thin = { ...statsRaw } as Record<string, unknown>;
    delete thin.series_daily; delete thin.funnel; delete thin.relevance_hist_30d;
    delete thin.models_used_30d; delete thin.fetch_30d; delete thin.top_sources_30d;
    delete thin.echo_dist; delete thin.rhythm_7x24;
    expect(() => parseStats(thin)).not.toThrow();
  });

  // Rich shape: a minimal fixture with every v2 block present.
  it('parses with all v2 blocks', () => {
    const rich = {
      ...(statsRaw as Record<string, unknown>),
      series_daily: [{ d: '2026-07-01', items: 3204, clusters: 2900, curated: 25 }],
      funnel: {
        all_time: { items: 123935, clusters: 111215, fetched: 4200, curated: 897, published: 850 },
        last_30d: { items: 90000, clusters: 82000, fetched: 900, curated: 600, published: 580 },
      },
      relevance_hist_30d: { kept: { '7': 40, '8': 12 }, skipped: { '3': 55 } },
      models_used_30d: [
        { scope: 'curation', model: 'qwen2.5:14b', backend: 'local', count: 412, avg_relevance: 7.1 },
        { scope: 'digest', model: 'claude-opus-4-8', backend: null, count: 9, avg_relevance: null },
      ],
      fetch_30d: { ok: 610, paywalled: 55, failed: 20, skipped: 210 },
      top_sources_30d: [{ name: 'Hacker News', items: 3100 }],
      echo_dist: { '1': 90000, '2': 12000, '3_5': 7000, '6_plus': 2200 },
      rhythm_7x24: Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 3)),
    };
    const parsed = parseStats(rich);
    expect(parsed.series_daily![0].items).toBe(3204);
    expect(parsed.models_used_30d![0].scope).toBe('curation');
  });

  it('rejects a malformed rhythm grid (wrong row length)', () => {
    const bad = {
      ...(statsRaw as Record<string, unknown>),
      rhythm_7x24: Array.from({ length: 7 }, () => [1, 2, 3]),
    };
    expect(() => parseStats(bad)).toThrow();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run tests/unit/schema.test.ts`
Expected: FAIL — `parses with all v2 blocks` throws (unknown keys are stripped by zod default, but `parsed.series_daily` is `undefined`, so `parsed.series_daily![0]` TypeErrors), and the malformed-grid test fails because nothing rejects it.

- [ ] **Step 3: Implement** — in `src/lib/schema.ts`, insert immediately above `export const statsSchema`:

```ts
/* ── stats.json v2 coverage blocks ─────────────────────────────────────────
 * Emitted by pipeline publish.py::export_stats v2. ALL optional: the site
 * must build against either export generation (the deployed pipeline lags
 * the repo until Illya syncs it). coverage.astro renders a band only when
 * its block is present. */

export const dayPointSchema = z.object({
  d: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  items: z.number(),
  clusters: z.number(),
  curated: z.number(),
});
export type DayPoint = z.infer<typeof dayPointSchema>;

const funnelCountsSchema = z.object({
  items: z.number(),
  clusters: z.number(),
  fetched: z.number(),
  curated: z.number(),
  published: z.number(),
});
export type FunnelCounts = z.infer<typeof funnelCountsSchema>;

const scoreBucketsSchema = z.record(z.string(), z.number());

export const modelUsageSchema = z.object({
  scope: z.enum(['curation', 'digest']),
  model: z.string().min(1),
  backend: z.string().nullable(),
  count: z.number(),
  avg_relevance: z.number().nullable(),
});
export type ModelUsage = z.infer<typeof modelUsageSchema>;
```

Then add to the `statsSchema` object, after `models: z.record(z.string(), z.string()),`:

```ts
  // v2 coverage blocks — optional during the export transition.
  series_daily: z.array(dayPointSchema).optional(),
  funnel: z
    .object({ all_time: funnelCountsSchema, last_30d: funnelCountsSchema })
    .optional(),
  relevance_hist_30d: z
    .object({ kept: scoreBucketsSchema, skipped: scoreBucketsSchema })
    .optional(),
  models_used_30d: z.array(modelUsageSchema).optional(),
  fetch_30d: z
    .object({
      ok: z.number(),
      paywalled: z.number(),
      failed: z.number(),
      skipped: z.number(),
    })
    .optional(),
  top_sources_30d: z
    .array(z.object({ name: z.string(), items: z.number() }))
    .optional(),
  echo_dist: z
    .object({
      '1': z.number(),
      '2': z.number(),
      '3_5': z.number(),
      '6_plus': z.number(),
    })
    .optional(),
  rhythm_7x24: z.array(z.array(z.number()).length(24)).length(7).optional(),
```

- [ ] **Step 4: Run to verify pass**

Run: `npx vitest run tests/unit/schema.test.ts`
Expected: PASS (all describe blocks).

- [ ] **Step 5: Commit**

```bash
git add src/lib/schema.ts tests/unit/schema.test.ts
git commit -m "feat: optional v2 coverage blocks in the stats contract

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: coverage.ts helpers — wire chart geometry

**Files:**
- Create: `src/lib/coverage.ts`
- Test: `tests/unit/coverage.test.ts` (new)

**Interfaces:**
- Consumes: `DayPoint` type from `src/lib/schema` (Task 2).
- Produces (Task 9 consumes verbatim):
  - `fmtDay(d: string): string` — `'2026-07-04'` → `'Jul 4'`
  - `columnChart(series: DayPoint[], width: number, height: number, gap?: number): { columns: { x: number; y: number; w: number; h: number; title: string }[]; max: number }`
  - `curatedStrip(series: DayPoint[], steps?: number): { level: number; title: string }[]` — level 0..steps-1
  - `seriesCaption(series: DayPoint[]): { busiest: DayPoint; quietest: DayPoint; mean: number }`

- [ ] **Step 1: Write the failing tests** — create `tests/unit/coverage.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import {
  columnChart,
  curatedStrip,
  fmtDay,
  seriesCaption,
} from '../../src/lib/coverage';

const day = (d: string, items: number, curated = 0) => ({
  d,
  items,
  clusters: 0,
  curated,
});

describe('fmtDay', () => {
  it('formats an ISO date as short month + day', () => {
    expect(fmtDay('2026-07-04')).toBe('Jul 4');
    expect(fmtDay('2026-12-31')).toBe('Dec 31');
  });
});

describe('columnChart', () => {
  const series = [day('2026-07-01', 100, 5), day('2026-07-02', 50), day('2026-07-03', 0)];
  const { columns, max } = columnChart(series, 300, 100, 2);

  it('scales the tallest column to full height', () => {
    expect(max).toBe(100);
    expect(columns[0].h).toBe(100);
    expect(columns[0].y).toBe(0);
  });

  it('gives zero days zero height at the baseline', () => {
    expect(columns[2].h).toBe(0);
    expect(columns[2].y).toBe(100);
  });

  it('fills the width: last column ends at width', () => {
    const last = columns[columns.length - 1];
    expect(last.x + last.w).toBeCloseTo(300, 6);
  });

  it('titles carry the day, items, and curated count', () => {
    expect(columns[0].title).toBe('Jul 1: 100 items, 5 curated');
  });

  it('never divides by zero on an all-zero series', () => {
    const flat = columnChart([day('2026-07-01', 0)], 300, 100);
    expect(flat.columns[0].h).toBe(0);
  });
});

describe('curatedStrip', () => {
  it('maps zero to level 0 and the max to the top level', () => {
    const strip = curatedStrip(
      [day('2026-07-01', 1, 0), day('2026-07-02', 1, 2), day('2026-07-03', 1, 8)],
      5
    );
    expect(strip[0].level).toBe(0);
    expect(strip[2].level).toBe(4);
    expect(strip[1].level).toBeGreaterThanOrEqual(1);
    expect(strip[1].level).toBeLessThan(4);
    expect(strip[2].title).toBe('Jul 3: 8 curated');
  });

  it('handles an all-zero curated series', () => {
    const strip = curatedStrip([day('2026-07-01', 5, 0)]);
    expect(strip[0].level).toBe(0);
  });
});

describe('seriesCaption', () => {
  it('finds busiest, quietest, and mean', () => {
    const cap = seriesCaption([
      day('2026-07-01', 100),
      day('2026-07-02', 40),
      day('2026-07-03', 10),
    ]);
    expect(cap.busiest.d).toBe('2026-07-01');
    expect(cap.quietest.d).toBe('2026-07-03');
    expect(cap.mean).toBe(50);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run tests/unit/coverage.test.ts`
Expected: FAIL — cannot resolve `../../src/lib/coverage`.

- [ ] **Step 3: Implement** — create `src/lib/coverage.ts`:

```ts
/**
 * Pure geometry/binning helpers behind /coverage/ — the transparency
 * dashboard. No Astro imports, no DOM: everything here is unit-tested and
 * the .astro page stays thin. Charts are build-time inline SVG/CSS per the
 * design language (no chart libraries).
 */
import type { DayPoint } from './schema';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** '2026-07-04' -> 'Jul 4' (UTC, no locale surprises at build time). */
export function fmtDay(d: string): string {
  const m = Number(d.slice(5, 7));
  const day = Number(d.slice(8, 10));
  return `${MONTHS[m - 1]} ${day}`;
}

export interface Column {
  x: number;
  y: number;
  w: number;
  h: number;
  title: string;
}

/**
 * Column geometry for the 90-day wire chart. Origin top-left (SVG): a
 * column of height h sits at y = height - h. Zero days render h=0 (no
 * 1px lie for a genuinely empty day).
 */
export function columnChart(
  series: DayPoint[],
  width: number,
  height: number,
  gap = 1
): { columns: Column[]; max: number } {
  const max = Math.max(...series.map((p) => p.items), 1);
  const w = (width - gap * (series.length - 1)) / series.length;
  const columns = series.map((p, i) => {
    const h = p.items === 0 ? 0 : Math.max(1, (p.items / max) * height);
    return {
      x: i * (w + gap),
      y: height - h,
      w,
      h,
      title: `${fmtDay(p.d)}: ${p.items.toLocaleString('en-US')} items, ${p.curated} curated`,
    };
  });
  return { columns, max: Math.max(...series.map((p) => p.items), 0) };
}

/**
 * Curated-picks strip under the wire chart: one cell per day, opacity level
 * 0..steps-1 (0 = none). Own scale — curated volume is two orders below
 * ingest and would vanish on the shared axis.
 */
export function curatedStrip(
  series: DayPoint[],
  steps = 5
): { level: number; title: string }[] {
  const max = Math.max(...series.map((p) => p.curated), 0);
  return series.map((p) => ({
    level: p.curated === 0 || max === 0 ? 0 : Math.max(1, Math.ceil((p.curated / max) * (steps - 1))),
    title: `${fmtDay(p.d)}: ${p.curated} curated`,
  }));
}

export function seriesCaption(series: DayPoint[]): {
  busiest: DayPoint;
  quietest: DayPoint;
  mean: number;
} {
  let busiest = series[0];
  let quietest = series[0];
  let total = 0;
  for (const p of series) {
    if (p.items > busiest.items) busiest = p;
    if (p.items < quietest.items) quietest = p;
    total += p.items;
  }
  return { busiest, quietest, mean: Math.round(total / series.length) };
}
```

- [ ] **Step 4: Run to verify pass**

Run: `npx vitest run tests/unit/coverage.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lib/coverage.ts tests/unit/coverage.test.ts
git commit -m "feat: wire-chart geometry helpers for the coverage dashboard

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: coverage.ts helpers — funnel, strips, histogram, heatmap

**Files:**
- Modify: `src/lib/coverage.ts` (append)
- Test: `tests/unit/coverage.test.ts` (append)

**Interfaces:**
- Consumes: `FunnelCounts` from `src/lib/schema` (Task 2).
- Produces (Task 9 consumes verbatim):
  - `funnelRows(counts: FunnelCounts): { key: string; label: string; count: number; pct: number | null; widthPct: number }[]`
  - `stripSegments(parts: { label: string; value: number }[]): { label: string; value: number; pct: number }[]`
  - `histogramRows(hist: { kept: Record<string, number>; skipped: Record<string, number> }): { rows: { score: number; kept: number; skipped: number }[]; max: number }`
  - `heatLevels(grid: number[][], steps?: number): number[][]`

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/coverage.test.ts`:

```ts
import {
  funnelRows,
  heatLevels,
  histogramRows,
  stripSegments,
} from '../../src/lib/coverage';

describe('funnelRows', () => {
  const rows = funnelRows({
    items: 100000,
    clusters: 90000,
    fetched: 4000,
    curated: 900,
    published: 850,
  });

  it('orders the five stages and keeps counts', () => {
    expect(rows.map((r) => r.key)).toEqual([
      'items',
      'clusters',
      'fetched',
      'curated',
      'published',
    ]);
    expect(rows[0].count).toBe(100000);
  });

  it('computes conversion vs the previous stage (first is null)', () => {
    expect(rows[0].pct).toBeNull();
    expect(rows[1].pct).toBeCloseTo(90, 1);
    expect(rows[4].pct).toBeCloseTo((850 / 900) * 100, 1);
  });

  it('log-scales widths: first is 100, all positive counts >= 2', () => {
    expect(rows[0].widthPct).toBe(100);
    for (const r of rows) expect(r.widthPct).toBeGreaterThanOrEqual(2);
    expect(rows[3].widthPct).toBeLessThan(rows[1].widthPct);
  });

  it('survives zero counts', () => {
    const z = funnelRows({ items: 0, clusters: 0, fetched: 0, curated: 0, published: 0 });
    expect(z[0].widthPct).toBe(2);
    expect(z[1].pct).toBeNull();
  });
});

describe('stripSegments', () => {
  it('drops zero segments and yields percentages summing to ~100', () => {
    const segs = stripSegments([
      { label: 'ok', value: 75 },
      { label: 'paywalled', value: 25 },
      { label: 'failed', value: 0 },
    ]);
    expect(segs.map((s) => s.label)).toEqual(['ok', 'paywalled']);
    expect(segs[0].pct).toBeCloseTo(75, 5);
    expect(segs.reduce((a, s) => a + s.pct, 0)).toBeCloseTo(100, 5);
  });

  it('returns [] when everything is zero', () => {
    expect(stripSegments([{ label: 'x', value: 0 }])).toEqual([]);
  });
});

describe('histogramRows', () => {
  it('fills 0..10, coercing missing buckets to zero', () => {
    const { rows, max } = histogramRows({ kept: { '7': 40 }, skipped: { '3': 55 } });
    expect(rows).toHaveLength(11);
    expect(rows[7]).toEqual({ score: 7, kept: 40, skipped: 0 });
    expect(rows[3]).toEqual({ score: 3, kept: 0, skipped: 55 });
    expect(max).toBe(55);
  });
});

describe('heatLevels', () => {
  it('buckets 0 to level 0 and max to the top level', () => {
    const levels = heatLevels([
      [0, 5, 10],
      [1, 0, 2],
    ]);
    expect(levels[0][0]).toBe(0);
    expect(levels[0][2]).toBe(4);
    expect(levels[1][1]).toBe(0);
    expect(levels[1][0]).toBeGreaterThanOrEqual(1);
  });

  it('handles an all-zero grid', () => {
    expect(heatLevels([[0, 0]])).toEqual([[0, 0]]);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run tests/unit/coverage.test.ts`
Expected: FAIL — `funnelRows` etc. not exported.

- [ ] **Step 3: Implement** — append to `src/lib/coverage.ts`:

```ts
import type { FunnelCounts } from './schema';

const FUNNEL_LABELS: [keyof FunnelCounts, string][] = [
  ['items', 'items ingested'],
  ['clusters', 'stories clustered'],
  ['fetched', 'articles read'],
  ['curated', 'picks curated'],
  ['published', 'stories published'],
];

/**
 * Funnel rows with log-scaled widths. Linear widths would render every
 * stage after "items" invisible (897 of 123,935 is 0.7% of the track); the
 * page caption states the scale.
 */
export function funnelRows(counts: FunnelCounts): {
  key: string;
  label: string;
  count: number;
  pct: number | null;
  widthPct: number;
}[] {
  const maxLog = Math.log10(Math.max(counts.items, 1) + 1);
  let prev: number | null = null;
  return FUNNEL_LABELS.map(([key, label]) => {
    const count = counts[key];
    const pct = prev !== null && prev > 0 ? (count / prev) * 100 : null;
    prev = count;
    const widthPct =
      maxLog === 0 ? 2 : Math.max(2, (Math.log10(count + 1) / maxLog) * 100);
    return { key, label, count, pct, widthPct };
  });
}

/** Proportional strip segments (tier split, fetch outcomes, model mix). */
export function stripSegments(
  parts: { label: string; value: number }[]
): { label: string; value: number; pct: number }[] {
  const total = parts.reduce((a, p) => a + p.value, 0);
  if (total === 0) return [];
  return parts
    .filter((p) => p.value > 0)
    .map((p) => ({ ...p, pct: (p.value / total) * 100 }));
}

/** Relevance histogram: dense 0..10 rows from sparse buckets. */
export function histogramRows(hist: {
  kept: Record<string, number>;
  skipped: Record<string, number>;
}): { rows: { score: number; kept: number; skipped: number }[]; max: number } {
  const rows = Array.from({ length: 11 }, (_, score) => ({
    score,
    kept: hist.kept[String(score)] ?? 0,
    skipped: hist.skipped[String(score)] ?? 0,
  }));
  const max = Math.max(...rows.map((r) => Math.max(r.kept, r.skipped)), 1);
  return { rows, max: rows.every((r) => r.kept === 0 && r.skipped === 0) ? 0 : max };
}

/** Heatmap ink levels: 0 stays 0; positive values bucket into 1..steps-1. */
export function heatLevels(grid: number[][], steps = 5): number[][] {
  const max = Math.max(...grid.flat(), 0);
  return grid.map((row) =>
    row.map((v) =>
      v === 0 || max === 0 ? 0 : Math.max(1, Math.ceil((v / max) * (steps - 1)))
    )
  );
}
```

Note: `import type` lines must sit at the top of the file with the existing import — move it there (single `import type { DayPoint, FunnelCounts } from './schema';`).

- [ ] **Step 4: Run to verify pass**

Run: `npx vitest run tests/unit/coverage.test.ts`
Expected: PASS. `histogramRows` max: note the test expects `max` 55 with data present; the all-zero case returns `max: 0` (the page hides the band anyway).

- [ ] **Step 5: Commit**

```bash
git add src/lib/coverage.ts tests/unit/coverage.test.ts
git commit -m "feat: funnel, strip, histogram, heatmap helpers for coverage

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: coverage.ts helpers — editions calendar

**Files:**
- Modify: `src/lib/coverage.ts` (append)
- Test: `tests/unit/coverage.test.ts` (append)

**Interfaces:**
- Consumes: nothing new (plain args; the page maps collection entries in).
- Produces (Task 9 consumes verbatim):
  - `interface EditionEntry { kind: string; date: Date; id: string; title: string; period: string }`
  - `editionsCalendar(entries: EditionEntry[], today: Date): { weeks: { days: { date: string; entry: EditionEntry | null }[]; weekly: EditionEntry | null }[]; specials: EditionEntry[] }`
  - Weeks run Monday-first from the week of the earliest entry to the week of `today` (UTC). `days[i].entry` is the daily digest for that date or null; `weekly` is that week's weekly digest; `specials` = monthly/quarterly/yearly sorted ascending by date.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/coverage.test.ts`:

```ts
import { editionsCalendar } from '../../src/lib/coverage';

const entry = (kind: string, iso: string, id: string) => ({
  kind,
  date: new Date(`${iso}T00:00:00Z`),
  id,
  title: id,
  period: iso,
});

describe('editionsCalendar', () => {
  const entries = [
    entry('daily', '2026-05-14', 'daily/2026-05-14'), // a Thursday
    entry('daily', '2026-05-15', 'daily/2026-05-15'),
    entry('weekly', '2026-05-11', 'weekly/2026-w20'), // Monday of that week
    entry('monthly', '2026-05-01', 'monthly/2026-05'),
  ];
  const cal = editionsCalendar(entries, new Date('2026-05-21T12:00:00Z'));

  it('spans from the earliest entry week to the today week', () => {
    expect(cal.weeks).toHaveLength(2); // week of May 11, week of May 18
    expect(cal.weeks[0].days[0].date).toBe('2026-05-11');
    expect(cal.weeks[0].days[6].date).toBe('2026-05-17');
  });

  it('places dailies on their day and weeklies on their week', () => {
    expect(cal.weeks[0].days[3].entry?.id).toBe('daily/2026-05-14');
    expect(cal.weeks[0].days[0].entry).toBeNull(); // weekly is not a day fill
    expect(cal.weeks[0].weekly?.id).toBe('weekly/2026-w20');
    expect(cal.weeks[1].weekly).toBeNull();
  });

  it('routes monthly/quarterly/yearly to specials, ascending', () => {
    expect(cal.specials.map((s) => s.id)).toEqual(['monthly/2026-05']);
  });

  it('returns empty structures for no entries', () => {
    const empty = editionsCalendar([], new Date('2026-05-21T00:00:00Z'));
    expect(empty.weeks).toEqual([]);
    expect(empty.specials).toEqual([]);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run tests/unit/coverage.test.ts`
Expected: FAIL — `editionsCalendar` not exported.

- [ ] **Step 3: Implement** — append to `src/lib/coverage.ts`:

```ts
export interface EditionEntry {
  kind: string;
  date: Date;
  id: string;
  title: string;
  period: string;
}

const DAY_MS = 86_400_000;

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** UTC Monday 00:00 of the week containing d. */
function mondayOf(d: Date): Date {
  const utc = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const dow = new Date(utc).getUTCDay(); // 0 = Sun
  return new Date(utc - ((dow + 6) % 7) * DAY_MS);
}

/**
 * Editions calendar: GitHub-style weeks (Mon-first) from the earliest
 * edition to today. Dailies fill day cells; the weekly digest marks its
 * week; monthly/quarterly/yearly are a separate labelled row.
 */
export function editionsCalendar(
  entries: EditionEntry[],
  today: Date
): {
  weeks: { days: { date: string; entry: EditionEntry | null }[]; weekly: EditionEntry | null }[];
  specials: EditionEntry[];
} {
  if (entries.length === 0) return { weeks: [], specials: [] };

  const dailies = new Map<string, EditionEntry>();
  const weeklies = new Map<string, EditionEntry>(); // key: monday ISO
  const specials: EditionEntry[] = [];
  for (const e of entries) {
    if (e.kind === 'daily') dailies.set(isoDate(e.date), e);
    else if (e.kind === 'weekly') weeklies.set(isoDate(mondayOf(e.date)), e);
    else specials.push(e);
  }
  specials.sort((a, b) => a.date.valueOf() - b.date.valueOf());

  const first = mondayOf(new Date(Math.min(...entries.map((e) => e.date.valueOf()))));
  const last = mondayOf(today);
  const weeks = [];
  for (let t = first.valueOf(); t <= last.valueOf(); t += 7 * DAY_MS) {
    const monday = new Date(t);
    weeks.push({
      days: Array.from({ length: 7 }, (_, i) => {
        const date = isoDate(new Date(t + i * DAY_MS));
        return { date, entry: dailies.get(date) ?? null };
      }),
      weekly: weeklies.get(isoDate(monday)) ?? null,
    });
  }
  return { weeks, specials };
}
```

- [ ] **Step 4: Run to verify pass**

Run: `npx vitest run tests/unit/coverage.test.ts`
Expected: PASS (all coverage.test.ts describes).

- [ ] **Step 5: Commit**

```bash
git add src/lib/coverage.ts tests/unit/coverage.test.ts
git commit -m "feat: editions calendar model for coverage

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Pipeline export — eight v2 blocks in export_stats

**Files:**
- Modify: `pipeline/signalpipe/publish.py` (insert helpers above `export_stats`; extend its return dict)

There is no Python test infrastructure in this repo (deliberate; verified). Verification for this task is Task 7's regeneration + the vitest contract test parsing the real output. Match the file's existing style: SQL strings built by adjacent-literal concatenation, window bounds via `_iso_days_ago`, `r["col"]` row access.

**Interfaces:**
- Consumes: `_iso_days_ago`, `_now_iso` (publish.py:73-85), `sqlite3.Row` connections.
- Produces: `export_stats(conn, cfg)` returns the existing dict PLUS keys `series_daily`, `funnel`, `relevance_hist_30d`, `models_used_30d`, `fetch_30d`, `top_sources_30d`, `echo_dist`, `rhythm_7x24` — shapes exactly matching Task 2's zod blocks.

- [ ] **Step 1: Add the v2 helper functions** — insert into `pipeline/signalpipe/publish.py` directly ABOVE `def export_stats` (currently line 186):

```python
def _daily_series(conn: sqlite3.Connection, days: int = 90) -> List[Dict[str, Any]]:
    """Contiguous per-day counts (zero-filled) for the wire chart."""
    since = _iso_days_ago(days)

    def per_day(sql: str) -> Dict[str, int]:
        return {r["d"]: r["n"] for r in conn.execute(sql, (since,)).fetchall()}

    items = per_day(
        "SELECT substr(ingested_at,1,10) AS d, COUNT(*) AS n FROM items "
        "WHERE ingested_at >= ? GROUP BY d"
    )
    clusters = per_day(
        "SELECT substr(first_seen,1,10) AS d, COUNT(*) AS n FROM clusters "
        "WHERE first_seen >= ? GROUP BY d"
    )
    curated = per_day(
        "SELECT substr(curated_at,1,10) AS d, COUNT(*) AS n FROM curations "
        "WHERE status='done' AND skip=0 AND curated_at >= ? GROUP BY d"
    )
    today = datetime.datetime.now(datetime.timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        out.append({
            "d": d,
            "items": items.get(d, 0),
            "clusters": clusters.get(d, 0),
            "curated": curated.get(d, 0),
        })
    return out


def _funnel_counts(conn: sqlite3.Connection,
                   since: Optional[str] = None) -> Dict[str, int]:
    """items -> clusters -> fetched -> curated -> published. published =
    distinct story_id in the ledger (a story published to several editions
    counts once)."""
    def one(sql, args=()):
        return conn.execute(sql, args).fetchone()[0]

    if since is None:
        return {
            "items": one("SELECT COUNT(*) FROM items"),
            "clusters": one("SELECT COUNT(*) FROM clusters"),
            "fetched": one(
                "SELECT COUNT(*) FROM articles WHERE fetch_status='ok'"),
            "curated": one(
                "SELECT COUNT(*) FROM curations WHERE status='done' AND skip=0"),
            "published": one(
                "SELECT COUNT(DISTINCT story_id) FROM published_ledger"),
        }
    return {
        "items": one(
            "SELECT COUNT(*) FROM items WHERE ingested_at >= ?", (since,)),
        "clusters": one(
            "SELECT COUNT(*) FROM clusters WHERE first_seen >= ?", (since,)),
        "fetched": one(
            "SELECT COUNT(*) FROM articles "
            "WHERE fetch_status='ok' AND extracted_at >= ?", (since,)),
        "curated": one(
            "SELECT COUNT(*) FROM curations "
            "WHERE status='done' AND skip=0 AND curated_at >= ?", (since,)),
        "published": one(
            "SELECT COUNT(DISTINCT story_id) FROM published_ledger "
            "WHERE first_at >= ?", (since,)),
    }


def _relevance_hist(conn: sqlite3.Connection, since: str) -> Dict[str, Dict[str, int]]:
    """Score histogram 0..10, kept (done, not skipped) vs skipped (everything
    else that got a score). Scores clamp into 0..10 defensively."""
    hist = {"kept": {}, "skipped": {}}
    for r in conn.execute(
        "SELECT (status='done' AND skip=0) AS kept, relevance_score AS s, "
        "COUNT(*) AS n FROM curations "
        "WHERE relevance_score IS NOT NULL AND curated_at >= ? "
        "GROUP BY kept, s",
        (since,),
    ).fetchall():
        bucket = "kept" if r["kept"] else "skipped"
        s = str(max(0, min(10, int(r["s"]))))
        hist[bucket][s] = hist[bucket].get(s, 0) + r["n"]
    return hist


def _models_used(conn: sqlite3.Connection, since: str) -> List[Dict[str, Any]]:
    """Observed provenance: which model actually ran, per scope. The DB holds
    one model_used per curation and per digest; sub-stage splits don't exist,
    so neither does a fake breakdown."""
    rows: List[Dict[str, Any]] = []
    for r in conn.execute(
        "SELECT model_used AS model, backend_used AS backend, COUNT(*) AS n, "
        "AVG(relevance_score) AS avg_rel FROM curations "
        "WHERE model_used IS NOT NULL AND curated_at >= ? "
        "GROUP BY model_used, backend_used ORDER BY n DESC",
        (since,),
    ).fetchall():
        rows.append({
            "scope": "curation",
            "model": r["model"],
            "backend": r["backend"],
            "count": r["n"],
            "avg_relevance":
                round(r["avg_rel"], 2) if r["avg_rel"] is not None else None,
        })
    for r in conn.execute(
        "SELECT model_used AS model, COUNT(*) AS n FROM digests "
        "WHERE model_used IS NOT NULL AND generated_at >= ? "
        "GROUP BY model_used ORDER BY n DESC",
        (since,),
    ).fetchall():
        rows.append({
            "scope": "digest",
            "model": r["model"],
            "backend": None,
            "count": r["n"],
            "avg_relevance": None,
        })
    return rows


def _rhythm_7x24(conn: sqlite3.Connection, since: str) -> List[List[int]]:
    """Items ingested by UTC weekday (Mon-first) x hour."""
    grid = [[0] * 24 for _ in range(7)]
    for r in conn.execute(
        "SELECT substr(ingested_at,1,13) AS dh, COUNT(*) AS n FROM items "
        "WHERE ingested_at >= ? GROUP BY dh",
        (since,),
    ).fetchall():
        try:
            wd = datetime.date.fromisoformat(r["dh"][:10]).weekday()
            hh = int(r["dh"][11:13])
        except (ValueError, IndexError):
            continue
        if 0 <= hh <= 23:
            grid[wd][hh] += r["n"]
    return grid
```

- [ ] **Step 2: Wire the blocks into `export_stats`** — inside `export_stats`, add after the `week_since = _iso_days_ago(7)` line:

```python
    month_since = _iso_days_ago(30)
```

Add before the `return {` statement:

```python
    fetch_30d = {"ok": 0, "paywalled": 0, "failed": 0, "skipped": 0}
    for r in conn.execute(
        "SELECT fetch_status AS s, COUNT(*) AS n FROM articles "
        "WHERE extracted_at >= ? GROUP BY s",
        (month_since,),
    ).fetchall():
        if r["s"] in fetch_30d:
            fetch_30d[r["s"]] = r["n"]

    top_sources = [
        {"name": r["name"], "items": r["n"]}
        for r in conn.execute(
            "SELECT src.name AS name, COUNT(*) AS n FROM items i "
            "JOIN sources src ON src.id=i.source_id "
            "WHERE i.ingested_at >= ? "
            "GROUP BY src.name ORDER BY n DESC LIMIT 15",
            (month_since,),
        ).fetchall()
    ]

    echo = {"1": 0, "2": 0, "3_5": 0, "6_plus": 0}
    for r in conn.execute(
        "SELECT surface_count AS c, COUNT(*) AS n FROM clusters GROUP BY c"
    ).fetchall():
        c = r["c"] or 0
        if c <= 1:
            echo["1"] += r["n"]
        elif c == 2:
            echo["2"] += r["n"]
        elif c <= 5:
            echo["3_5"] += r["n"]
        else:
            echo["6_plus"] += r["n"]
```

And extend the returned dict — add after the `"models": ...` entry:

```python
        # v2 coverage blocks (optional in the site schema; same privacy
        # rules: counts and model names only, no dollars, no error text).
        "series_daily": _daily_series(conn),
        "funnel": {
            "all_time": _funnel_counts(conn),
            "last_30d": _funnel_counts(conn, month_since),
        },
        "relevance_hist_30d": _relevance_hist(conn, month_since),
        "models_used_30d": _models_used(conn, month_since),
        "fetch_30d": fetch_30d,
        "top_sources_30d": top_sources,
        "echo_dist": echo,
        "rhythm_7x24": _rhythm_7x24(conn, month_since),
```

- [ ] **Step 3: Syntax check**

```bash
cd <WT>/pipeline && python3 -m py_compile signalpipe/publish.py && echo OK
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add pipeline/signalpipe/publish.py
git commit -m "pipeline: export v2 coverage blocks from export_stats

Series, funnel, relevance histogram, observed model provenance, fetch
outcomes, top sources, echo distribution, ingest rhythm. Counts and
model names only; the no-dollars/no-errors privacy contract holds.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Regenerate stats.json from the live DB (read-only)

**Files:**
- Create: `<scratchpad>/regen-stats.py` (NOT committed — scratchpad only)
- Modify (regenerated): `src/data/stats.json`

**Interfaces:**
- Consumes: Task 6's `export_stats`; live DB at `~/.local/state/signal/signal.db`; runtime config at `~/.local/state/signal/app/config/signal.json`.
- Produces: rich `src/data/stats.json` committed on the branch. Tasks 8-9 build against it.

- [ ] **Step 1: Write the scratch script** — `<scratchpad>/regen-stats.py`:

```python
#!/usr/bin/env python3
"""One-off: regenerate src/data/stats.json from the live signal DB,
READ-ONLY. No services touched; no pipeline state written.

Usage: python3 regen-stats.py /path/to/worktree
"""
import importlib
import json
import pathlib
import sys
import types

WT = pathlib.Path(sys.argv[1]).resolve()
PKG_DIR = WT / "pipeline" / "signalpipe"

# Import signalpipe submodules WITHOUT executing signalpipe/__init__.py
# (avoids any heavy/side-effect imports the package root may have).
pkg = types.ModuleType("signalpipe")
pkg.__path__ = [str(PKG_DIR)]
sys.modules["signalpipe"] = pkg

config = importlib.import_module("signalpipe.config")
db = importlib.import_module("signalpipe.db")
publish = importlib.import_module("signalpipe.publish")

RUNTIME_CFG = pathlib.Path(
    "~/.local/state/signal/app/config/signal.json").expanduser()
cfg = config.load(RUNTIME_CFG)

conn = db.connect_ro(cfg.db_path)   # file:...?mode=ro — cannot write
try:
    stats = publish.export_stats(conn, cfg)
finally:
    conn.close()

out = WT / "src" / "data" / "stats.json"
out.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n")

# Spot-check summary for the operator.
print("wrote", out)
print("series_daily days:", len(stats["series_daily"]))
print("funnel all_time:", stats["funnel"]["all_time"])
print("models_used_30d:", [
    (m["scope"], m["model"], m["count"]) for m in stats["models_used_30d"]])
print("fetch_30d:", stats["fetch_30d"])
print("top_sources_30d[0:3]:", stats["top_sources_30d"][:3])
print("echo_dist:", stats["echo_dist"])
print("rhythm total:", sum(sum(r) for r in stats["rhythm_7x24"]))
```

- [ ] **Step 2: Run it**

```bash
python3 <scratchpad>/regen-stats.py <WT>
```

Expected: `wrote .../src/data/stats.json`, 90 series days, non-zero funnel counts (items ~124k), curation model rows including `qwen2.5:14b`, digest rows including a Claude model. If `config.load` raises (validation), fall back: replace the `cfg = config.load(...)` line with a duck-typed config:

```python
raw = json.loads(RUNTIME_CFG.read_text())
def _model_for(t):
    b = raw["backend"]["selector"]
    if b == "local":
        b = "subscription"
    return raw["tiers"][t][b]
cfg = types.SimpleNamespace(
    site=raw.get("site", {}), funnel=raw["funnel"],
    channels=list(raw["channels"]), model_for=_model_for,
    db_path=pathlib.Path(raw.get(
        "db_path", "~/.local/state/signal/signal.db")).expanduser(),
)
```

- [ ] **Step 3: Sanity-check the output against the schema**

```bash
cd <WT> && npx vitest run tests/unit/schema.test.ts tests/unit/sources.test.ts
```

Expected: PASS — the real regenerated stats.json parses with all v2 blocks, and the sources soft-consistency tests still hold. Also spot-check two numbers against the printed summary:
`python3 -c "import json;s=json.load(open('<WT>/src/data/stats.json'));print(s['pipeline']['items_total'], s['funnel']['all_time']['items'])"` — the two must be equal.

- [ ] **Step 4: Commit**

```bash
git add src/data/stats.json
git commit -m "signal: refresh stats with v2 coverage blocks (read-only export)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Chart CSS primitives

**Files:**
- Modify: `src/styles/global.css` — insert a new section AFTER the bar-charts block (after the `.bar` mobile `@media` closes, before the `/* ── footer` header)

**Interfaces:**
- Produces class names Task 9 uses verbatim: `.wire-chart`, `.wire-chart__strip`, `.wire-chart__cell`, `.wire-chart__cell--l1..l4` (level 0 is the bare cell), `.wire-chart__caption`, `.funnel`, `.funnel__row`, `.funnel__label`, `.funnel__track`, `.funnel__fill`, `.funnel__val`, `.funnel__row--lead`, `.strip`, `.strip__seg`, `.strip__seg--lead`, `.strip-legend`, `.hist`, `.hist__col`, `.hist__col--lead`, `.hist__kept`, `.hist__skipped`, `.hist__x`, `.heatmap`, `.heatmap__row`, `.heatmap__day`, `.heatmap__cell`, `.heatmap__cell--l1..l4`, `.editions`, `.editions__week`, `.editions__day`, `.editions__day--filled`, `.editions__weekly`, `.editions__weekly--filled`, `.editions__specials`, `.stat__delta`, `.chart-note`.

- [ ] **Step 1: Insert the CSS block** (tokens only, sharp corners are global, accent discipline: one lead per chart):

```css
/* ── coverage charts — build-time SVG/CSS, no libraries ─────────────────── */
/* The wire chart: SVG columns (ink-faint), curated strip below on its own
   scale. Orange marks exactly one lead per band: the curated strip, the
   funnel's final stage, the histogram's modal kept bucket. */
.wire-chart { margin: var(--space-4) 0 0; }
.wire-chart svg { display: block; width: 100%; height: 9rem; }
.wire-chart svg rect { fill: var(--ink-faint); }
.wire-chart__strip {
  display: grid; grid-auto-flow: column; grid-auto-columns: 1fr;
  gap: 1px; margin-top: 2px;
}
.wire-chart__cell { height: 0.55rem; background: var(--ground-2); }
.wire-chart__cell--l1 { background: color-mix(in srgb, var(--accent) 30%, var(--ground-2)); }
.wire-chart__cell--l2 { background: color-mix(in srgb, var(--accent) 55%, var(--ground-2)); }
.wire-chart__cell--l3 { background: color-mix(in srgb, var(--accent) 80%, var(--ground-2)); }
.wire-chart__cell--l4 { background: var(--accent); }
.wire-chart__caption {
  display: flex; flex-wrap: wrap; gap: 0.4rem 1.6rem; margin-top: 0.6rem;
  font-family: var(--mono); font-size: var(--mono-xs);
  letter-spacing: var(--track-meta); color: var(--ink-faint);
  font-variant-numeric: tabular-nums;
}

/* funnel — log-scaled tracks; caption states the scale */
.funnel { list-style: none; margin: var(--space-4) 0 0; padding: 0; display: grid; gap: 0.5rem; }
.funnel__row {
  display: grid; grid-template-columns: minmax(9rem, max-content) 1fr 8rem;
  gap: 0 0.9rem; align-items: center;
}
.funnel__label {
  font-family: var(--mono); font-size: var(--mono-xs); letter-spacing: var(--track-meta);
  text-transform: uppercase; color: var(--ink-soft); white-space: nowrap;
}
.funnel__track { height: 0.7rem; background: var(--ground-2); border: 1px solid var(--hairline); position: relative; }
.funnel__fill { position: absolute; inset: 0 auto 0 0; min-width: 2px; background: var(--ink-faint); }
.funnel__row--lead .funnel__fill { background: var(--accent); }
.funnel__val {
  font-family: var(--mono); font-size: var(--mono-xs); color: var(--ink-faint);
  text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap;
}

/* proportional strips (tier split, fetch outcomes, model mix) */
.strip { display: flex; height: 0.9rem; border: 1px solid var(--hairline); background: var(--ground-2); margin-top: var(--space-3); }
.strip__seg { background: var(--ink-faint); }
.strip__seg + .strip__seg { border-left: 1px solid var(--ground); }
.strip__seg--lead { background: var(--accent); }
.strip-legend {
  display: flex; flex-wrap: wrap; gap: 0.3rem 1.4rem; margin: 0.5rem 0 0; padding: 0;
  list-style: none; font-family: var(--mono); font-size: var(--mono-xs);
  letter-spacing: var(--track-meta); color: var(--ink-soft);
  font-variant-numeric: tabular-nums;
}

/* relevance histogram — kept vs skipped, side by side per score */
.hist {
  display: grid; grid-template-columns: repeat(11, 1fr); gap: 0.35rem;
  align-items: end; height: 8rem; margin-top: var(--space-4);
  border-bottom: 1px solid var(--hairline-bold); padding-bottom: 0;
}
.hist__col { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; align-items: end; height: 100%; }
.hist__kept { background: var(--ink-soft); min-height: 0; }
.hist__skipped { background: var(--hairline-bold); min-height: 0; }
.hist__col--lead .hist__kept { background: var(--accent); }
.hist__x {
  display: grid; grid-template-columns: repeat(11, 1fr); gap: 0.35rem;
  margin-top: 0.35rem; font-family: var(--mono); font-size: var(--mono-xs);
  color: var(--ink-faint); text-align: center; font-variant-numeric: tabular-nums;
}

/* rhythm heatmap — 7 x 24, ink levels, never orange */
.heatmap { display: grid; gap: 2px; margin-top: var(--space-4); }
.heatmap__row { display: grid; grid-template-columns: 2.4rem repeat(24, 1fr); gap: 2px; align-items: center; }
.heatmap__day {
  font-family: var(--mono); font-size: var(--mono-xs); letter-spacing: var(--track-meta);
  text-transform: uppercase; color: var(--ink-faint);
}
.heatmap__cell { aspect-ratio: 1; background: var(--ground-2); }
.heatmap__cell--l1 { background: color-mix(in srgb, var(--ink) 18%, var(--ground-2)); }
.heatmap__cell--l2 { background: color-mix(in srgb, var(--ink) 38%, var(--ground-2)); }
.heatmap__cell--l3 { background: color-mix(in srgb, var(--ink) 62%, var(--ground-2)); }
.heatmap__cell--l4 { background: var(--ink); }

/* editions calendar — weeks as columns, Mon-first */
.editions { display: flex; gap: 3px; margin-top: var(--space-4); overflow-x: auto; padding-bottom: 0.3rem; }
.editions__week { display: grid; gap: 3px; }
.editions__day, .editions__weekly {
  width: 0.8rem; height: 0.8rem; background: var(--ground-2);
  border: 1px solid var(--hairline); display: block;
}
.editions__day--filled { background: var(--ink-faint); border-color: var(--ink-faint); }
a.editions__day--filled:hover { background: var(--accent); border-color: var(--accent); }
.editions__weekly { margin-top: 0.35rem; }
.editions__weekly--filled { background: var(--accent); border-color: var(--accent); }
.editions__specials { display: flex; flex-wrap: wrap; gap: 0.4rem 1.4rem; margin-top: var(--space-3); }

/* stat-card 7d delta + band footnotes */
.stat__delta {
  display: block; margin-top: 0.15rem; font-family: var(--mono);
  font-size: var(--mono-xs); letter-spacing: var(--track-meta);
  color: var(--ink-faint); font-variant-numeric: tabular-nums;
}
.chart-note {
  margin: var(--space-3) 0 0; max-width: var(--measure-prose);
  color: var(--ink-soft); font-size: var(--small); text-wrap: pretty;
}
@media (max-width: 40rem) {
  .funnel__row { grid-template-columns: 1fr auto; grid-template-areas: "label label" "track val"; row-gap: 0.25rem; }
  .funnel__label { grid-area: label; }
  .funnel__track { grid-area: track; }
  .funnel__val { grid-area: val; }
  .heatmap__row { grid-template-columns: 1.6rem repeat(24, 1fr); }
}
```

Note on `color-mix`: baseline-available since 2023 and consistent with the token system (mixes follow theme + accent re-inking automatically). Degradation: an old browser paints `--ground-2` cells flat, which is quiet, not broken.

- [ ] **Step 2: Build check**

```bash
cd <WT> && npm run check && npm run build
```

Expected: 0 errors (CSS is inert until Task 9 uses it).

- [ ] **Step 3: Commit**

```bash
git add src/styles/global.css
git commit -m "feat: chart primitives for the coverage dashboard

Wire chart, funnel, strips, histogram, heatmap, editions calendar.
Tokens only; one accent lead per chart; 40rem mobile re-grids.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Rebuild coverage.astro as the ten-band dashboard

**Files:**
- Modify: `src/pages/coverage.astro` (full rewrite)

**Interfaces:**
- Consumes: everything from Tasks 2-8: `parseStats` + v2 optional blocks; all `src/lib/coverage` helpers; the CSS classes of Task 8; digests collection; `sources.json` for the paywalled share.
- Produces: the production `/coverage/` page. Every v2 band is wrapped in a presence check.

- [ ] **Step 1: Rewrite `src/pages/coverage.astro`** with exactly this content:

```astro
---
import { getCollection } from 'astro:content';
import Base from '../layouts/Base.astro';
import { site, href, KIND_LABEL, type DigestKind } from '../site';
import { parseStats } from '../lib/schema';
import rawStats from '../data/stats.json';
import sourcesData from '../data/sources.json';
import {
  columnChart, curatedStrip, editionsCalendar, fmtDay, funnelRows,
  heatLevels, histogramRows, seriesCaption, stripSegments,
} from '../lib/coverage';

const stats = parseStats(rawStats);
const n = (x: number) => x.toLocaleString('en-US');
const pct1 = (x: number) => (Math.round(x * 10) / 10).toString();
const generated = stats.generated_at.slice(0, 19).replace('T', ' ') + ' UTC';

const SRC_CAT_LABEL: Record<string, string> = {
  aggregators: 'Aggregators',
  ai_companies: 'AI companies',
  devtools: 'Dev tools',
  expert_blogs: 'Expert blogs',
  hardware_science: 'Hardware & science',
  news: 'News',
  newsletters: 'Newsletters',
  physics: 'Physics',
  research: 'Research',
  science: 'Science',
  tech_news: 'Tech news',
  security: 'Security',
};
const label = (k: string) => SRC_CAT_LABEL[k] ?? k.replaceAll('_', ' ');
const barPct = (v: number, max: number) => Math.max(2, Math.round((v / max) * 100));

/* ── band 3: ninety days on the wire ── */
const series = stats.series_daily && stats.series_daily.length > 0 ? stats.series_daily : null;
const chart = series ? columnChart(series, 900, 140) : null;
const strip = series ? curatedStrip(series) : null;
const cap = series ? seriesCaption(series) : null;
const clusters7d = series ? series.slice(-7).reduce((a, p) => a + p.clusters, 0) : null;

/* ── band 4: the funnel ── */
const funnel = stats.funnel ?? null;
const funnelAll = funnel ? funnelRows(funnel.all_time) : null;
const keptShare = funnel && funnel.all_time.items > 0
  ? (funnel.all_time.published / funnel.all_time.items) * 100
  : null;

/* ── band 5: who feeds the wire ── */
const byCat = Object.entries(stats.sources.by_category)
  .filter(([, c]) => c > 0)
  .sort((a, b) => b[1] - a[1]);
const maxCat = Math.max(...byCat.map(([, c]) => c), 1);
const TIER_LABEL: Record<string, string> = { '1': 'flagship', '2': 'core', '3': 'niche' };
const tierStrip = stripSegments(
  Object.entries(stats.sources.by_tier)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([t, c]) => ({ label: `tier ${t} ${TIER_LABEL[t] ?? ''}`.trim(), value: c }))
);
const paywalledCount = (sourcesData as { paywalled: boolean }[]).filter((s) => s.paywalled).length;
const paywalledPct = (paywalledCount / (sourcesData as unknown[]).length) * 100;
const topSources = stats.top_sources_30d && stats.top_sources_30d.length > 0 ? stats.top_sources_30d : null;
const maxTopSource = topSources ? Math.max(...topSources.map((s) => s.items), 1) : 1;
const surfaces = stats.top_surfaces_7d.slice(0, 10);
const maxSurface = Math.max(...surfaces.map((s) => s.clusters), 1);
const echo = stats.echo_dist ?? null;
const echoStrip = echo
  ? stripSegments([
      { label: 'surfaces once', value: echo['1'] },
      { label: 'twice', value: echo['2'] },
      { label: '3 to 5 times', value: echo['3_5'] },
      { label: '6+ times', value: echo['6_plus'] },
    ])
  : null;

/* ── band 6: the models ── */
const STAGE_LABEL: Record<string, string> = { triage: 'Triage', deep: 'Deep read', digest: 'Digest' };
const configured = ['triage', 'deep', 'digest']
  .filter((t) => stats.models[t])
  .map((t) => ({ stage: STAGE_LABEL[t], model: stats.models[t] }));
const used = stats.models_used_30d && stats.models_used_30d.length > 0 ? stats.models_used_30d : null;
const usedCuration = used ? used.filter((m) => m.scope === 'curation') : [];
const usedDigest = used ? used.filter((m) => m.scope === 'digest') : [];
const curationStrip = stripSegments(usedCuration.map((m) => ({ label: m.model, value: m.count })));
const digestStrip = stripSegments(usedDigest.map((m) => ({ label: m.model, value: m.count })));

/* ── band 7: the bar ── */
const hist = stats.relevance_hist_30d ? histogramRows(stats.relevance_hist_30d) : null;
const histLead = hist && hist.max > 0
  ? hist.rows.reduce((best, r) => (r.kept > best.kept ? r : best), hist.rows[0]).score
  : -1;
const fetchStrip = stats.fetch_30d
  ? stripSegments([
      { label: 'read', value: stats.fetch_30d.ok },
      { label: 'paywalled', value: stats.fetch_30d.paywalled },
      { label: 'failed', value: stats.fetch_30d.failed },
      { label: 'skipped', value: stats.fetch_30d.skipped },
    ])
  : null;

/* ── band 8: rhythm ── */
const heat = stats.rhythm_7x24 ? heatLevels(stats.rhythm_7x24) : null;
const heatTotal = stats.rhythm_7x24
  ? stats.rhythm_7x24.reduce((a, row) => a + row.reduce((b, v) => b + v, 0), 0)
  : 0;
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/* ── band 9: editions ── */
const digests = await getCollection('digests');
const calendar = editionsCalendar(
  digests.map((d) => ({
    kind: d.data.kind,
    date: d.data.date,
    id: d.id,
    title: d.data.title,
    period: d.data.period,
  })),
  new Date(stats.generated_at)
);
const latest = stats.digests.latest ?? null;
---
<Base title={`Coverage | ${site.name}`} description="The wire, instrumented: ninety days of ingest, the funnel from firehose to picks, the sources, the models that did the reading, and every edition filed. Generated by the pipeline, not by hand.">
  <div class="section-head" style="padding-top:2.4rem;">
    <h1>Coverage</h1>
    <span class="count">as of {generated}</span>
  </div>
  <p class="cat-blurb">
    What the wire reads, and what it does with it. Every figure on this page
    is generated by the pipeline on each publish; nothing is hand-entered.
    The full feed roll is on the <a href={href('/sources/')}>sources</a> page.
  </p>

  <div class="rail">
    <span class="rail__label">The wire</span>
    <span class="rail__rule"></span>
    <span class="rail__count">running totals</span>
  </div>
  <div class="stat-grid">
    <div class="stat"><b>{n(stats.sources.verified)}</b><span>verified sources</span></div>
    <div class="stat">
      <b>{n(stats.pipeline.items_total)}</b><span>items ingested</span>
      {stats.pipeline.items_7d != null && <span class="stat__delta">{n(stats.pipeline.items_7d)} this week</span>}
    </div>
    <div class="stat">
      <b>{n(stats.pipeline.clusters_total)}</b><span>stories clustered</span>
      {clusters7d != null && <span class="stat__delta">{n(clusters7d)} this week</span>}
    </div>
    <div class="stat">
      <b>{n(stats.pipeline.curations_done)}</b><span>curated picks</span>
      {stats.pipeline.curated_7d != null && <span class="stat__delta">{n(stats.pipeline.curated_7d)} this week</span>}
    </div>
    <div class="stat">
      <b>{n(stats.digests.total)}</b><span>digests published</span>
      {latest && <span class="stat__delta">latest {latest.date}</span>}
    </div>
    {stats.pipeline.avg_relevance_7d != null && (
      <div class="stat"><b>{stats.pipeline.avg_relevance_7d.toFixed(1)}</b><span>avg relevance, 7d</span></div>
    )}
  </div>

  {series && chart && strip && cap && (
    <>
      <div class="rail">
        <span class="rail__label">Ninety days on the wire</span>
        <span class="rail__rule"></span>
        <span class="rail__count">items per day</span>
      </div>
      <figure class="wire-chart">
        <svg viewBox="0 0 900 140" preserveAspectRatio="none" role="img" aria-label={`Items ingested per day over the last ${series.length} days. Busiest day ${fmtDay(cap.busiest.d)} with ${n(cap.busiest.items)} items.`}>
          {chart.columns.map((c) => (
            <rect x={c.x} y={c.y} width={c.w} height={c.h}><title>{c.title}</title></rect>
          ))}
        </svg>
        <div class="wire-chart__strip" aria-label="Curated picks per day, own scale">
          {strip.map((s) => (
            <span class={`wire-chart__cell wire-chart__cell--l${s.level}`} title={s.title}></span>
          ))}
        </div>
        <figcaption class="wire-chart__caption">
          <span>busiest {fmtDay(cap.busiest.d)} {n(cap.busiest.items)}</span>
          <span>quietest {fmtDay(cap.quietest.d)} {n(cap.quietest.items)}</span>
          <span>mean {n(cap.mean)} per day</span>
          <span>orange strip: picks curated that day</span>
        </figcaption>
      </figure>
    </>
  )}

  {funnel && funnelAll && (
    <>
      <div class="rail">
        <span class="rail__label">The funnel</span>
        <span class="rail__rule"></span>
        <span class="rail__count">all time</span>
      </div>
      <ul class="funnel">
        {funnelAll.map((r, i) => (
          <li class={i === funnelAll.length - 1 ? 'funnel__row funnel__row--lead' : 'funnel__row'}>
            <span class="funnel__label">{r.label}</span>
            <span class="funnel__track"><span class="funnel__fill" style={`width:${r.widthPct}%`}></span></span>
            <span class="funnel__val">{n(r.count)}{r.pct != null && ` (${pct1(r.pct)}%)`}</span>
          </li>
        ))}
      </ul>
      {keptShare != null && (
        <p class="chart-note">
          Read everything, keep almost nothing: of {n(funnel.all_time.items)} items
          ingested, {n(funnel.all_time.published)} stories were published,
          {' '}{keptShare < 10 ? keptShare.toFixed(1) : Math.round(keptShare)}% of the
          firehose. Track widths are log scale; on a linear track every stage
          after the first would be invisible. Percentages are conversion from
          the previous stage.
        </p>
      )}
    </>
  )}

  <div class="rail">
    <span class="rail__label">Who feeds the wire</span>
    <span class="rail__rule"></span>
    <span class="rail__count">{n(stats.sources.verified)} sources</span>
  </div>
  <ul class="bars">
    {byCat.map(([cat, c], i) => (
      <li class={i === 0 ? 'bar bar--lead' : 'bar'}>
        <span class="bar__label">{label(cat)}</span>
        <span class="bar__track"><span class="bar__fill" style={`width:${barPct(c, maxCat)}%`}></span></span>
        <span class="bar__val">{n(c)}</span>
      </li>
    ))}
  </ul>
  {tierStrip.length > 0 && (
    <>
      <div class="strip" role="img" aria-label="Sources by tier, proportional">
        {tierStrip.map((s, i) => (
          <span
            class={i === 0 ? 'strip__seg strip__seg--lead' : 'strip__seg'}
            style={`width:${s.pct}%`}
            title={`${s.label}: ${n(s.value)}`}
          ></span>
        ))}
      </div>
      <ul class="strip-legend">
        {tierStrip.map((s) => (<li>{s.label} {n(s.value)}</li>))}
        <li>paywalled {pct1(paywalledPct)}% of the roll</li>
      </ul>
    </>
  )}

  {topSources && (
    <>
      <div class="rail">
        <span class="rail__label">Loudest feeds, 30 days</span>
        <span class="rail__rule"></span>
        <span class="rail__count">by items ingested</span>
      </div>
      <ul class="bars">
        {topSources.map((s, i) => (
          <li class={i === 0 ? 'bar bar--lead' : 'bar'}>
            <span class="bar__label" title={s.name}>{s.name}</span>
            <span class="bar__track"><span class="bar__fill" style={`width:${barPct(s.items, maxTopSource)}%`}></span></span>
            <span class="bar__val">{n(s.items)}</span>
          </li>
        ))}
      </ul>
    </>
  )}

  <div class="rail">
    <span class="rail__label">Where stories echo, 7 days</span>
    <span class="rail__rule"></span>
    <span class="rail__count">by clusters</span>
  </div>
  <ul class="bars">
    {surfaces.map((s, i) => (
      <li class={i === 0 ? 'bar bar--lead' : 'bar'}>
        <span class="bar__label" title={s.name}>{s.name}</span>
        <span class="bar__track"><span class="bar__fill" style={`width:${barPct(s.clusters, maxSurface)}%`}></span></span>
        <span class="bar__val">{n(s.clusters)}</span>
      </li>
    ))}
  </ul>
  {echoStrip && echoStrip.length > 0 && (
    <>
      <div class="strip" role="img" aria-label="How many surfaces each story appears on">
        {echoStrip.map((s, i) => (
          <span
            class={i === echoStrip.length - 1 ? 'strip__seg strip__seg--lead' : 'strip__seg'}
            style={`width:${s.pct}%`}
            title={`${s.label}: ${n(s.value)} stories`}
          ></span>
        ))}
      </div>
      <ul class="strip-legend">
        {echoStrip.map((s) => (<li>{s.label} {n(s.value)}</li>))}
      </ul>
      <p class="chart-note">
        A surface is a place a story shows up: Hacker News, an arXiv section,
        a subreddit. Most stories surface once and stay there. The ones that
        echo across six or more surfaces are the news, and the pipeline
        clusters every echo into one pick so nothing is counted twice.
      </p>
    </>
  )}

  <div class="rail">
    <span class="rail__label">The models</span>
    <span class="rail__rule"></span>
    <span class="rail__count">configured, then observed</span>
  </div>
  <div class="stat-grid">
    {configured.map((m) => (
      <div class="stat"><b style="font-size:1.1rem; font-family:var(--mono); font-weight:500;">{m.model}</b><span>{m.stage}</span></div>
    ))}
  </div>
  {used && (
    <>
      {curationStrip.length > 0 && (
        <>
          <div class="strip" role="img" aria-label="Curation calls by model, last 30 days">
            {curationStrip.map((s, i) => (
              <span
                class={i === 0 ? 'strip__seg strip__seg--lead' : 'strip__seg'}
                style={`width:${s.pct}%`}
                title={`${s.label}: ${n(s.value)} curations`}
              ></span>
            ))}
          </div>
          <ul class="strip-legend">
            {usedCuration.map((m) => (
              <li>
                {m.model}{m.backend ? ` on ${m.backend}` : ''} {n(m.count)} curations
                {m.avg_relevance != null ? `, avg relevance ${m.avg_relevance.toFixed(1)}` : ''}
              </li>
            ))}
          </ul>
        </>
      )}
      {digestStrip.length > 0 && (
        <>
          <div class="strip" role="img" aria-label="Digest runs by model, last 30 days">
            {digestStrip.map((s, i) => (
              <span
                class={i === 0 ? 'strip__seg strip__seg--lead' : 'strip__seg'}
                style={`width:${s.pct}%`}
                title={`${s.label}: ${n(s.value)} digests`}
              ></span>
            ))}
          </div>
          <ul class="strip-legend">
            {usedDigest.map((m) => (<li>{m.model} {n(m.count)} digests</li>))}
          </ul>
        </>
      )}
      <p class="chart-note">
        The cards above are the configured routing; the strips are what
        actually ran in the last 30 days, straight from the curation log.
        Triage runs on a local model on a MacBook; judgment is rented by the
        token.
      </p>
    </>
  )}

  {hist && hist.max > 0 && (
    <>
      <div class="rail">
        <span class="rail__label">The bar</span>
        <span class="rail__rule"></span>
        <span class="rail__count">relevance 0 to 10, 30 days</span>
      </div>
      <div class="hist" role="img" aria-label="Relevance score distribution, kept versus skipped, last 30 days">
        {hist.rows.map((r) => (
          <div class={r.score === histLead ? 'hist__col hist__col--lead' : 'hist__col'}>
            <span class="hist__kept" style={`height:${(r.kept / hist.max) * 100}%`} title={`score ${r.score}: ${n(r.kept)} kept`}></span>
            <span class="hist__skipped" style={`height:${(r.skipped / hist.max) * 100}%`} title={`score ${r.score}: ${n(r.skipped)} skipped`}></span>
          </div>
        ))}
      </div>
      <div class="hist__x">
        {hist.rows.map((r) => (<span>{r.score}</span>))}
      </div>
      <p class="chart-note">
        Dark columns were kept, pale columns were judged and skipped. You are
        looking at the cut line: where the pale mass ends and the dark mass
        begins is the bar a story has to clear.
      </p>
    </>
  )}
  {fetchStrip && fetchStrip.length > 0 && (
    <>
      <div class="strip" role="img" aria-label="Article fetch outcomes, last 30 days">
        {fetchStrip.map((s, i) => (
          <span
            class={i === 0 ? 'strip__seg strip__seg--lead' : 'strip__seg'}
            style={`width:${s.pct}%`}
            title={`${s.label}: ${n(s.value)} articles`}
          ></span>
        ))}
      </div>
      <ul class="strip-legend">
        {fetchStrip.map((s) => (<li>{s.label} {n(s.value)}</li>))}
      </ul>
    </>
  )}

  {heat && heatTotal > 0 && (
    <>
      <div class="rail">
        <span class="rail__label">Rhythm</span>
        <span class="rail__rule"></span>
        <span class="rail__count">ingest by hour, UTC, 30 days</span>
      </div>
      <div class="heatmap" role="img" aria-label="Items ingested by weekday and hour, UTC, last 30 days">
        {heat.map((row, d) => (
          <div class="heatmap__row">
            <span class="heatmap__day">{DAYS[d]}</span>
            {row.map((level, h) => (
              <span
                class={`heatmap__cell heatmap__cell--l${level}`}
                title={`${DAYS[d]} ${String(h).padStart(2, '0')}:00 UTC: ${n(stats.rhythm_7x24![d][h])} items`}
              ></span>
            ))}
          </div>
        ))}
      </div>
    </>
  )}

  {calendar.weeks.length > 0 && (
    <>
      <div class="rail">
        <span class="rail__label">Editions</span>
        <span class="rail__rule"></span>
        <span class="rail__count">{n(stats.digests.total)} filed</span>
      </div>
      <div class="editions">
        {calendar.weeks.map((w) => (
          <div class="editions__week">
            {w.days.map((day) =>
              day.entry ? (
                <a
                  class="editions__day editions__day--filled"
                  href={href(`/digests/${day.entry.id}/`)}
                  title={`${KIND_LABEL[day.entry.kind as DigestKind] ?? day.entry.kind} ${day.entry.period}`}
                ></a>
              ) : (
                <span class="editions__day" title={day.date}></span>
              )
            )}
            {w.weekly ? (
              <a
                class="editions__weekly editions__weekly--filled"
                href={href(`/digests/${w.weekly.id}/`)}
                title={`${KIND_LABEL['weekly']} ${w.weekly.period}`}
              ></a>
            ) : (
              <span class="editions__weekly"></span>
            )}
          </div>
        ))}
      </div>
      {calendar.specials.length > 0 && (
        <ul class="strip-legend editions__specials">
          {calendar.specials.map((s) => (
            <li><a href={href(`/digests/${s.id}/`)}>{KIND_LABEL[s.kind as DigestKind] ?? s.kind} {s.period}</a></li>
          ))}
        </ul>
      )}
      {latest && (
        <p class="chart-note">
          Latest edition: <a href={href(`/digests/${latest.kind}/${latest.period.toLowerCase()}/`)}>{latest.title}</a> ({KIND_LABEL[latest.kind as DigestKind] ?? latest.kind}, {latest.date}).
          Squares are daily briefs; the row beneath each week marks its weekly
          digest. Orange on hover means it links.
        </p>
      )}
    </>
  )}

  <p class="coverage-foot">
    Method, briefly: the pipeline reads every source on a cadence, clusters
    the same story across surfaces, fetches and reads the strongest link,
    scores relevance on a 0 to 10 rubric, and keeps almost nothing. Every
    number on this page regenerates on each publish. The full feed roll is
    on the <a href={href('/sources/')}>sources</a> page.
  </p>
</Base>
```

- [ ] **Step 2: Type-check and build**

```bash
cd <WT> && npm run check && npm run build
```

Expected: 0 errors, `/coverage/index.html` in `dist/`. If `KIND_LABEL[latest.kind as DigestKind]` trips on an unknown kind, the `?? latest.kind` fallback covers it.

- [ ] **Step 3: Eyeball in preview**

```bash
npx astro preview & sleep 2 && open http://localhost:4321/coverage/
```

Check: all ten bands render; exactly one orange lead per chart; dark mode via the nav toggle; mobile at 390px (funnel and bars re-grid, heatmap stays legible, editions strip scrolls horizontally).

- [ ] **Step 4: Run the full unit suite**

Run: `npm run test:unit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pages/coverage.astro
git commit -m "feat: rebuild /coverage/ as the ten-band instrument panel

Wire chart, funnel, sources, models observed vs configured, relevance
histogram, fetch outcomes, rhythm heatmap, editions calendar. Every v2
band renders only when its stats block is present, so the page degrades
cleanly against a thin export.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Retire /stats/ — delete, redirect, update references

**Files:**
- Delete: `src/pages/stats.astro`
- Modify: `astro.config.mjs`, `src/pages/about.astro:67`, `tests/e2e/pages.spec.ts:29`, `scripts/capture.mjs:48`
- Test: `tests/e2e/pages.spec.ts` (redirect test)

**Interfaces:**
- Consumes: Task 9's finished page (the redirect target must exist first).
- Produces: `/stats/` 200s as a meta-refresh page landing on `/coverage/`.

- [ ] **Step 1: Delete the page and add the redirect**

```bash
git rm src/pages/stats.astro
```

In `astro.config.mjs`, extend `defineConfig`:

```js
export default defineConfig({
  site: SITE,
  base: BASE,
  integrations: [sitemap()],
  build: { format: 'directory' },
  // /stats/ merged into /coverage/ (spec 2026-07-04); static meta-refresh.
  redirects: { '/stats': '/coverage/' },
});
```

- [ ] **Step 2: Update the two content references.** In `src/pages/about.astro`, the prose sentence linking `/stats/` (line ~67; grep `href('/stats/')` for the live position). Replace the phrase

`the <a href={href('/stats/')}>stats page</a> publishes the pipeline's numbers, down to which model runs each stage`

with

`the <a href={href('/coverage/')}>coverage page</a> publishes the pipeline's numbers, down to which model ran each stage`

(keep the rest of the sentence intact; if it already links `/coverage/` elsewhere in the same sentence, drop the duplicate link and keep plain text). Also update the comment at `src/pages/about.astro:10` (`exact counts live on /stats/ and` → `/coverage/`), and `src/lib/schema.ts:116`'s comment mentioning `/stats/`.

- [ ] **Step 3: Update test + capture route lists.** In `tests/e2e/pages.spec.ts` remove the `'/stats/',` line from `routes`, and append two tests at the end of the file:

```ts
test('/stats/ redirects to /coverage/', async ({ page }) => {
  await page.goto(u('/stats/'));
  await page.waitForURL('**/coverage/');
  expect(page.url()).toContain('/coverage/');
});

test('/coverage/ renders the v2 bands', async ({ page }) => {
  await page.goto(u('/coverage/'));
  await expect(page.getByText('Ninety days on the wire')).toBeVisible();
  await expect(page.getByText('The funnel')).toBeVisible();
  await expect(page.getByText('The models')).toBeVisible();
  await expect(page.getByText('Rhythm')).toBeVisible();
});
```

(The bands test rides on the rich stats.json committed in Task 7; if the data file ever goes thin again the bands hide by design and this test flags it, which is exactly the alarm we want on the site repo.)

In `scripts/capture.mjs` remove the `'/stats/',` line.

- [ ] **Step 4: Verify**

```bash
cd <WT> && npm run check && npm run build && npm run test:e2e
```

Expected: check 0 errors; build emits `dist/stats/index.html` containing `http-equiv="refresh"`; Playwright all green including the new redirect test.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: retire /stats/ into /coverage/ with a static redirect

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Docs — design-language.md reflects the new coverage page

**Files:**
- Modify: `docs/design-language.md:87-89` (the Coverage bullet in Structural system)

- [ ] **Step 1: Replace the bullet** currently reading:

```
- **Coverage** (`/coverage/`): the transparency dashboard — headline stat cards
  and CSS bar charts (`.bars`/`.bar`) of sources by category/tier and the
  loudest surfaces. **Sources** (`/sources/`): the full feed roll, grouped.
```

with:

```
- **Coverage** (`/coverage/`): the transparency dashboard, a stack of
  rail-labelled bands rendered as build-time SVG/CSS (never a chart
  library): stat cards with 7-day deltas, the 90-day wire chart with its
  curated strip, the log-scale funnel, source bars and tier/echo strips,
  configured-vs-observed model provenance, the relevance histogram, the
  7x24 ingest heatmap, and the editions calendar. Chart primitives live in
  global.css (`.wire-chart`, `.funnel`, `.strip`, `.hist`, `.heatmap`,
  `.editions`); geometry in `src/lib/coverage.ts`. One accent lead per
  chart. `/stats/` redirects here. **Sources** (`/sources/`): the full
  feed roll, grouped.
```

- [ ] **Step 2: Commit**

```bash
git add docs/design-language.md
git commit -m "docs: design-language coverage entry for the v2 dashboard

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Full verification + handoff

**Files:** none new.

- [ ] **Step 1: Full gate** (build before e2e — Playwright's preview server serves `dist/`)

```bash
cd <WT> && npm run check && npm run test:unit && npm run build && npm run test:e2e
```

Expected: everything green.

- [ ] **Step 2: Capture and eyeball**

```bash
npm run capture
```

Open the newest `screenshots/<stamp>/` and eyeball `coverage` in light + dark at all three viewports: one orange lead per chart, no horizontal page scroll at 390px (the editions strip scrolls inside its own container), heatmap legible, print preview of /coverage/ sane (charts are plain divs/SVG, they print).

- [ ] **Step 3: Report and stop.** Do NOT merge to main and do NOT push. Report to Illya:
  - branch `feat/coverage-dashboard` ready in the worktree, all gates green;
  - merge is his call (the main checkout has another active session; if `src/data/stats.json` conflicts at merge time, re-run the Task 7 regen script on the merged tree);
  - after merge, his one action: sync the deployed pipeline copy at `~/.local/state/signal/app/signalpipe` (his explicit go) so future publishes keep emitting the v2 blocks; until then the next publish rewrites stats.json thin and the v2 bands quietly hide.
