/**
 * Pure geometry/binning helpers behind /coverage/ — the transparency
 * dashboard. No Astro imports, no DOM: everything here is unit-tested and
 * the .astro page stays thin. Charts are build-time inline SVG/CSS per the
 * design language (no chart libraries).
 */
import type { DayPoint, FunnelCounts } from './schema';

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
